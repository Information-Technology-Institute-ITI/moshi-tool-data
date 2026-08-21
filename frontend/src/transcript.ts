import { sampleId } from "./api";
import type {
  ActivityRegion,
  AlignedWord,
  Annotation,
  Speaker,
  TranscriptUtterance,
} from "./types";

export const SAMPLE_RATE = 24_000;

/**
 * Renders a GPU quality flag for display. Flags are read-only information from
 * the pipeline: nothing in the review screen adds or removes them, and they
 * never mute audio, start processing, or gate approval.
 */
export function flagLabel(flag: string): string {
  return flag.replaceAll("_", " ");
}

export function chronological(list: TranscriptUtterance[]): TranscriptUtterance[] {
  return [...list].sort(
    (left, right) =>
      left.start_sample - right.start_sample || left.end_sample - right.end_sample,
  );
}

/** Entries whose sample range intersects [start, end). */
export function intersecting(
  list: TranscriptUtterance[],
  startSample: number,
  endSample: number,
): TranscriptUtterance[] {
  return chronological(
    list.filter((item) => item.start_sample < endSample && item.end_sample > startSample),
  );
}

function withTranscript(
  annotation: Annotation,
  transcript: TranscriptUtterance[],
): Annotation {
  return { ...annotation, transcript: chronological(transcript) };
}

function overlap(
  region: { start_sample: number; end_sample: number },
  segment: { start_sample: number; end_sample: number },
): number {
  return (
    Math.min(region.end_sample, segment.end_sample)
    - Math.max(region.start_sample, segment.start_sample)
  );
}

/**
 * The activity region that best corresponds to a transcript segment: same
 * speaker, and the largest time overlap. Exact bounds win outright.
 */
function matchingActivityIndex(
  activities: ActivityRegion[],
  segment: TranscriptUtterance,
): number {
  let best = -1;
  let bestOverlap = 0;
  activities.forEach((region, index) => {
    if (region.speaker !== segment.speaker) return;
    if (
      region.start_sample === segment.start_sample
      && region.end_sample === segment.end_sample
    ) {
      best = index;
      bestOverlap = Number.POSITIVE_INFINITY;
      return;
    }
    const shared = overlap(region, segment);
    if (shared > bestOverlap) {
      best = index;
      bestOverlap = shared;
    }
  });
  return bestOverlap > 0 ? best : -1;
}

function activityFor(segment: TranscriptUtterance): ActivityRegion {
  return {
    id: new_activity_id(),
    speaker: (segment.speaker || "A") as Speaker,
    start_sample: segment.start_sample,
    end_sample: segment.end_sample,
    origin: "manual",
    confidence: null,
  };
}

function new_activity_id(): string {
  return sampleId("activity");
}

/**
 * Mirrors a transcript change onto the speaker A/B lanes.
 *
 * The lanes show who spoke when, so adding, deleting, splitting or joining a
 * segment must move them too. Regions that do not correspond to the changed
 * segments are left alone, so hand-drawn activity regions survive.
 */
export function syncActivities(
  activities: ActivityRegion[],
  removed: TranscriptUtterance[],
  added: TranscriptUtterance[],
): ActivityRegion[] {
  const next = [...activities];
  for (const segment of removed) {
    const index = matchingActivityIndex(next, segment);
    if (index >= 0) next.splice(index, 1);
  }
  for (const segment of added) {
    next.push(activityFor(segment));
  }
  return next.sort(
    (left, right) =>
      left.start_sample - right.start_sample || left.end_sample - right.end_sample,
  );
}

/** Applies a transcript change and keeps the speaker lanes in step with it. */
function withSyncedTranscript(
  annotation: Annotation,
  transcript: TranscriptUtterance[],
  removed: TranscriptUtterance[],
  added: TranscriptUtterance[],
): Annotation {
  return {
    ...annotation,
    transcript: chronological(transcript),
    activities: syncActivities(annotation.activities, removed, added),
  };
}

export function updateSegment(
  annotation: Annotation,
  id: string,
  patch: Partial<TranscriptUtterance>,
): Annotation {
  const before = annotation.transcript.find((item) => item.id === id);
  if (!before) return annotation;
  const after = { ...before, ...patch };
  const transcript = annotation.transcript.map((item) =>
    item.id === id ? after : item,
  );
  // Only a speaker or timing change moves the lanes; editing text does not.
  const moved =
    after.speaker !== before.speaker
    || after.start_sample !== before.start_sample
    || after.end_sample !== before.end_sample;
  return moved
    ? withSyncedTranscript(annotation, transcript, [before], [after])
    : withTranscript(annotation, transcript);
}

export function newSegment(
  startSample: number,
  endSample: number,
  speaker: Speaker,
  text = "",
): TranscriptUtterance {
  return {
    id: sampleId("utterance"),
    speaker,
    start_sample: startSample,
    end_sample: endSample,
    text,
    model_text: "",
    model_speaker: null,
    quality_flags: [],
    alignment_status: "not_run",
    human_verified: false,
    review_candidates: [],
  };
}

export function addSegment(
  annotation: Annotation,
  startSample: number,
  endSample: number,
  speaker: Speaker,
  text = "",
): { annotation: Annotation; id: string } {
  const segment = newSegment(startSample, endSample, speaker, text);
  return {
    annotation: withSyncedTranscript(
      annotation,
      [...annotation.transcript, segment],
      [],
      [segment],
    ),
    id: segment.id,
  };
}

/** Deleted ids are never reused; the caller keeps undo history for recovery. */
export function deleteSegment(annotation: Annotation, id: string): Annotation {
  const removed = annotation.transcript.find((item) => item.id === id);
  if (!removed) return annotation;
  return withSyncedTranscript(
    annotation,
    annotation.transcript.filter((item) => item.id !== id),
    [removed],
    [],
  );
}

function wordStartSample(word: AlignedWord): number | null {
  return typeof word.start === "number" ? Math.round(word.start * SAMPLE_RATE) : null;
}

function wordEndSample(word: AlignedWord): number | null {
  return typeof word.end === "number" ? Math.round(word.end * SAMPLE_RATE) : null;
}

/**
 * The aligned words belonging to one segment, in spoken order. Words are matched
 * by overlap so a word is claimed by the segment it actually falls inside.
 */
export function wordsForSegment(
  alignedWords: AlignedWord[],
  startSample: number,
  endSample: number,
): AlignedWord[] {
  return alignedWords
    .filter((word) => {
      const start = wordStartSample(word);
      const end = wordEndSample(word);
      if (start === null || end === null) return false;
      return start < endSample && end > startSample;
    })
    .sort((left, right) => (wordStartSample(left)! - wordStartSample(right)!));
}

/**
 * Snaps a requested split point so it never lands inside a spoken word.
 *
 * A split exactly on a word boundary is kept. A split partway through a word
 * moves forward to that word's end, so the word stays whole and belongs to the
 * first half. Returns null when no aligned word covers the point.
 */
export function resolveSplitSample(
  alignedWords: AlignedWord[],
  startSample: number,
  endSample: number,
  requestedSample: number,
): { sample: number; snappedFrom: number | null } {
  const words = wordsForSegment(alignedWords, startSample, endSample);
  const straddling = words.find((word) => {
    const start = wordStartSample(word)!;
    const end = wordEndSample(word)!;
    return start < requestedSample && requestedSample < end;
  });
  if (!straddling) return { sample: requestedSample, snappedFrom: null };
  return { sample: wordEndSample(straddling)!, snappedFrom: requestedSample };
}

/**
 * Divides a segment's words at a sample point, keeping spoken order. Words
 * without timing follow the side their neighbours landed on, so text is never
 * dropped or reordered.
 */
function partitionWords(
  alignedWords: AlignedWord[],
  startSample: number,
  endSample: number,
  atSample: number,
): { first: AlignedWord[]; second: AlignedWord[] } {
  const words = wordsForSegment(alignedWords, startSample, endSample);
  const first: AlignedWord[] = [];
  const second: AlignedWord[] = [];
  let crossed = false;
  for (const word of words) {
    const end = wordEndSample(word);
    if (end !== null && end > atSample) crossed = true;
    (crossed ? second : first).push(word);
  }
  return { first, second };
}

function joinWords(words: AlignedWord[]): string {
  return words.map((word) => word.word.trim()).filter(Boolean).join(" ");
}

export type SplitResult =
  | {
      ok: true;
      annotation: Annotation;
      ids: [string, string];
      /** Where the split actually landed, after snapping to a word boundary. */
      atSample: number;
      /** Set when the requested point fell inside a word and was moved. */
      snappedFrom: number | null;
    }
  | { ok: false; reason: string };

/**
 * Splits one segment into two adjacent timestamp ranges against the original
 * media. Both halves receive new ids. This changes annotation only: no audio
 * file is created, cut, or concatenated.
 */
export function splitSegment(
  annotation: Annotation,
  id: string,
  requestedSample: number,
  textOffset?: number,
): SplitResult {
  const segment = annotation.transcript.find((item) => item.id === id);
  if (!segment) return { ok: false, reason: "That segment no longer exists." };
  if (requestedSample <= segment.start_sample || requestedSample >= segment.end_sample) {
    return { ok: false, reason: "The split point must fall inside the segment." };
  }

  const words = wordsForSegment(
    annotation.aligned_words,
    segment.start_sample,
    segment.end_sample,
  );
  const { sample: atSample, snappedFrom } = resolveSplitSample(
    annotation.aligned_words,
    segment.start_sample,
    segment.end_sample,
    requestedSample,
  );

  // Snapping past the final word would leave an empty second half.
  if (atSample <= segment.start_sample || atSample >= segment.end_sample) {
    return {
      ok: false,
      reason:
        "That word runs to the end of this segment, so splitting there would leave"
        + " nothing after it. Choose an earlier point.",
    };
  }

  let firstText: string;
  let secondText: string;
  if (words.length) {
    // Word timings decide the text on each side, so each half carries exactly
    // the words spoken inside its own range.
    const parts = partitionWords(
      annotation.aligned_words,
      segment.start_sample,
      segment.end_sample,
      atSample,
    );
    firstText = joinWords(parts.first);
    secondText = joinWords(parts.second);
  } else {
    // No alignment for this segment: fall back to the caret position.
    const cut = textOffset === undefined
      ? segment.text.length
      : Math.max(0, Math.min(segment.text.length, textOffset));
    firstText = segment.text.slice(0, cut).trim();
    secondText = segment.text.slice(cut).trim();
  }

  const first = newSegment(
    segment.start_sample,
    atSample,
    (segment.speaker || "A") as Speaker,
    firstText,
  );
  const second = newSegment(
    atSample,
    segment.end_sample,
    (segment.speaker || "A") as Speaker,
    secondText,
  );
  // Both halves keep the original flags; the reviewer resolves them per half.
  first.quality_flags = [...segment.quality_flags];
  second.quality_flags = [...segment.quality_flags];
  first.model_text = segment.model_text;
  first.model_speaker = segment.model_speaker ?? null;
  second.model_speaker = segment.model_speaker ?? null;
  return {
    ok: true,
    annotation: withSyncedTranscript(
      annotation,
      [...annotation.transcript.filter((item) => item.id !== id), first, second],
      [segment],
      [first, second],
    ),
    ids: [first.id, second.id],
    atSample,
    snappedFrom,
  };
}

export type JoinResult =
  | {
      ok: true;
      annotation: Annotation;
      id: string;
      /** Set when the two sides had different speakers and one had to win. */
      absorbedSpeaker: Speaker | null;
    }
  | { ok: false; reason: string };

/**
 * Joins two segments using union bounds and chronological text order.
 *
 * The earlier segment's speaker wins. Diarisation nearly always alternates
 * speakers between neighbours, so refusing a cross-speaker join would leave the
 * action permanently unavailable on real material; instead the caller is told
 * which speaker was absorbed so it can say so.
 */
export function joinSegments(
  annotation: Annotation,
  firstId: string,
  secondId: string,
): JoinResult {
  const left = annotation.transcript.find((item) => item.id === firstId);
  const right = annotation.transcript.find((item) => item.id === secondId);
  if (!left || !right) return { ok: false, reason: "Both segments must still exist." };
  if (left.id === right.id) return { ok: false, reason: "Choose two different segments." };
  const ordered = chronological([left, right]);
  const speaker = (ordered[0].speaker || "A") as Speaker;
  const absorbed = ordered[1].speaker && ordered[1].speaker !== speaker
    ? (ordered[1].speaker as Speaker)
    : null;
  const merged = newSegment(
    Math.min(left.start_sample, right.start_sample),
    Math.max(left.end_sample, right.end_sample),
    speaker,
    ordered.map((item) => item.text.trim()).filter(Boolean).join(" "),
  );
  merged.model_text = ordered
    .map((item) => item.model_text.trim())
    .filter(Boolean)
    .join(" ");
  merged.model_speaker = ordered[0].model_speaker ?? ordered[1].model_speaker ?? null;
  merged.quality_flags = Array.from(
    new Set([...left.quality_flags, ...right.quality_flags]),
  );
  merged.human_verified = left.human_verified && right.human_verified;
  return {
    ok: true,
    annotation: withSyncedTranscript(
      annotation,
      [
        ...annotation.transcript.filter(
          (item) => item.id !== firstId && item.id !== secondId,
        ),
        merged,
      ],
      [left, right],
      [merged],
    ),
    id: merged.id,
    absorbedSpeaker: absorbed,
  };
}

/** The next segment after `id` in chronological order, if any. */
export function neighbourAfter(
  list: TranscriptUtterance[],
  id: string,
): TranscriptUtterance | null {
  const ordered = chronological(list);
  const index = ordered.findIndex((item) => item.id === id);
  return index >= 0 && index + 1 < ordered.length ? ordered[index + 1] : null;
}

export function boundsError(
  startSample: number,
  endSample: number,
  durationSamples: number,
): string | null {
  if (!Number.isFinite(startSample) || !Number.isFinite(endSample)) {
    return "Timestamps must be numbers.";
  }
  if (startSample < 0) return "Start must not be negative.";
  if (endSample <= startSample) return "End must be after start.";
  if (durationSamples > 0 && endSample > durationSamples) {
    return "End must stay inside the source.";
  }
  return null;
}

/** One speaker-lane rectangle a segment covers, clipped to that segment. */
export type SegmentTurn = { start_sample: number; end_sample: number };

/**
 * The speaker-lane rectangles a segment covers, clipped to it and merged where
 * they overlap.
 *
 * The timeline can show one speaker talking across several rectangles while the
 * transcript still holds a single segment spanning all of them. These are the
 * turns that segment would divide into.
 */
export function turnsForSegment(
  activities: ActivityRegion[],
  segment: TranscriptUtterance,
): SegmentTurn[] {
  const speaker = segment.speaker || "A";
  const clipped = activities
    .filter((region) => region.speaker === speaker)
    .map((region) => ({
      start_sample: Math.max(region.start_sample, segment.start_sample),
      end_sample: Math.min(region.end_sample, segment.end_sample),
    }))
    .filter((region) => region.end_sample > region.start_sample)
    .sort(
      (left, right) =>
        left.start_sample - right.start_sample || left.end_sample - right.end_sample,
    );

  // Rectangles that overlap each other would claim the same words twice, so
  // they count as one turn. Rectangles that merely touch stay separate: the
  // timeline draws them as two, so they divide into two.
  const merged: SegmentTurn[] = [];
  for (const region of clipped) {
    const last = merged.at(-1);
    if (last && region.start_sample < last.end_sample) {
      last.end_sample = Math.max(last.end_sample, region.end_sample);
      continue;
    }
    merged.push({ ...region });
  }
  return merged;
}

/**
 * Places each aligned word on the turn it was spoken in, keeping spoken order.
 *
 * Overlap decides the owner. A word falling in the silence between two turns has
 * negative overlap with all of them, and the largest of those is the nearest
 * turn, so the word joins its closest neighbour instead of being dropped.
 */
function wordsByTurn(words: AlignedWord[], turns: SegmentTurn[]): AlignedWord[][] {
  const buckets: AlignedWord[][] = turns.map(() => []);
  for (const word of words) {
    const start = wordStartSample(word)!;
    const end = wordEndSample(word)!;
    let best = 0;
    let bestOverlap = Number.NEGATIVE_INFINITY;
    turns.forEach((turn, index) => {
      const shared =
        Math.min(turn.end_sample, end) - Math.max(turn.start_sample, start);
      if (shared > bestOverlap) {
        bestOverlap = shared;
        best = index;
      }
    });
    buckets[best].push(word);
  }
  return buckets;
}

export type TurnSplitResult =
  | { ok: true; annotation: Annotation; ids: string[] }
  | { ok: false; reason: string };

/**
 * Divides one segment into a segment per speaker-lane rectangle it covers, with
 * the word timings deciding which text lands on each turn.
 *
 * The lanes are the input here rather than the result, so they are left exactly
 * as they were: afterwards every rectangle carries its own transcript segment,
 * and the silence between rectangles carries none.
 */
export function splitSegmentByTurns(
  annotation: Annotation,
  id: string,
): TurnSplitResult {
  const segment = annotation.transcript.find((item) => item.id === id);
  if (!segment) return { ok: false, reason: "That segment no longer exists." };

  const turns = turnsForSegment(annotation.activities, segment);
  if (turns.length < 2) {
    return {
      ok: false,
      reason:
        "This segment sits on a single speaker turn, so there is nothing to divide"
        + " it at.",
    };
  }

  const words = wordsForSegment(
    annotation.aligned_words,
    segment.start_sample,
    segment.end_sample,
  );
  if (!words.length) {
    return {
      ok: false,
      reason:
        "This segment has no word timings, so its text cannot be shared out across"
        + " the turns. Split it by hand instead.",
    };
  }

  const buckets = wordsByTurn(words, turns);
  const speaker = (segment.speaker || "A") as Speaker;
  const parts = turns.map((turn, index) => {
    const part = newSegment(
      turn.start_sample,
      turn.end_sample,
      speaker,
      joinWords(buckets[index]),
    );
    // Every part keeps the original flags; the reviewer resolves them per turn.
    part.quality_flags = [...segment.quality_flags];
    part.model_speaker = segment.model_speaker ?? null;
    return part;
  });
  parts[0].model_text = segment.model_text;

  return {
    ok: true,
    annotation: withTranscript(annotation, [
      ...annotation.transcript.filter((item) => item.id !== id),
      ...parts,
    ]),
    ids: parts.map((part) => part.id),
  };
}

/** A stretch of a segment where the other speaker is also active. */
export type OverlapWindow = {
  start_sample: number;
  end_sample: number;
  /** The speaker talking over the segment, so the duplicate is assigned to them. */
  speaker: Speaker;
};

function mergeRanges(ranges: SegmentTurn[]): SegmentTurn[] {
  const sorted = [...ranges].sort(
    (left, right) =>
      left.start_sample - right.start_sample || left.end_sample - right.end_sample,
  );
  const merged: SegmentTurn[] = [];
  for (const range of sorted) {
    const last = merged.at(-1);
    if (last && range.start_sample <= last.end_sample) {
      last.end_sample = Math.max(last.end_sample, range.end_sample);
      continue;
    }
    merged.push({ ...range });
  }
  return merged;
}

/**
 * Stretches of a segment where the timeline also shows the other speaker.
 *
 * Diarisation gives an overlapped stretch to whichever speaker dominates, so the
 * transcript ends up with one segment for it while the lanes clearly show two
 * people talking. These are the windows the quieter speaker is missing.
 *
 * A window already carrying a segment for that speaker is left out, so asking
 * twice adds nothing the second time.
 */
export function overlapsForSegment(
  annotation: Annotation,
  segment: TranscriptUtterance,
): OverlapWindow[] {
  const speaker = segment.speaker || "A";
  const other: Speaker = speaker === "A" ? "B" : "A";
  const windows = mergeRanges(
    annotation.activities
      .filter((region) => region.speaker === other)
      .map((region) => ({
        start_sample: Math.max(region.start_sample, segment.start_sample),
        end_sample: Math.min(region.end_sample, segment.end_sample),
      }))
      .filter((region) => region.end_sample > region.start_sample),
  );
  return windows
    .filter(
      (window) =>
        !annotation.transcript.some(
          (item) =>
            (item.speaker || "A") === other
            && item.start_sample < window.end_sample
            && item.end_sample > window.start_sample,
        ),
    )
    .map((window) => ({ ...window, speaker: other }));
}

/** Every overlap window across the whole transcript, in time order. */
export function overlapsForAnnotation(
  annotation: Annotation,
): { segment: TranscriptUtterance; windows: OverlapWindow[] }[] {
  return chronological(annotation.transcript)
    .map((segment) => ({ segment, windows: overlapsForSegment(annotation, segment) }))
    .filter((entry) => entry.windows.length > 0);
}

function overlapSegment(
  annotation: Annotation,
  source: TranscriptUtterance,
  window: OverlapWindow,
): TranscriptUtterance {
  const words = wordsForSegment(
    annotation.aligned_words,
    window.start_sample,
    window.end_sample,
  );
  const text = joinWords(words);
  const part = newSegment(
    window.start_sample,
    window.end_sample,
    window.speaker,
    text,
  );
  part.model_text = text;
  // The model gave these words to the dominant speaker, so it never assigned
  // this segment: leaving model_speaker unset keeps that honest.
  part.model_speaker = null;
  part.alignment_status = words.length ? "aligned" : "not_run";
  part.quality_flags = Array.from(
    new Set([...source.quality_flags, "overlapping_speech"]),
  );
  return part;
}

/**
 * Gives the quieter speaker their own segment over each overlapped stretch.
 *
 * The original segment is left exactly as it is, words and all: the overlapped
 * words are copied, not moved, because both people really were talking. Saving
 * then keeps both — the full range under its original speaker, and the short
 * overlapped range under the other one.
 *
 * The lanes already show the overlap, so they are not touched.
 */
export function addOverlapSegments(
  annotation: Annotation,
  id: string,
): { annotation: Annotation; ids: string[] } {
  const segment = annotation.transcript.find((item) => item.id === id);
  if (!segment) return { annotation, ids: [] };
  const windows = overlapsForSegment(annotation, segment);
  if (!windows.length) return { annotation, ids: [] };
  const parts = windows.map((window) => overlapSegment(annotation, segment, window));
  return {
    annotation: withTranscript(annotation, [...annotation.transcript, ...parts]),
    ids: parts.map((part) => part.id),
  };
}

/** Runs {@link addOverlapSegments} over every segment that has an overlap. */
export function addAllOverlapSegments(
  annotation: Annotation,
): { annotation: Annotation; ids: string[] } {
  let current = annotation;
  const ids: string[] = [];
  // Recomputed per segment, so a window covered by a part just added is skipped.
  for (const segment of chronological(annotation.transcript)) {
    const result = addOverlapSegments(current, segment.id);
    current = result.annotation;
    ids.push(...result.ids);
  }
  return { annotation: current, ids };
}

/** A segment counts as this region's own when the region covers ~all of it. */
const OWNED_COVERAGE = 0.99;

/**
 * The transcript segments belonging to one speaker-lane rectangle.
 *
 * A rectangle owns a segment when they share a speaker and the rectangle covers
 * essentially the whole segment. A long segment spanning several rectangles is
 * owned by none of them, so removing one rectangle of an un-split run leaves the
 * transcript alone. The small tolerance absorbs a hand-resized region drifting a
 * few samples off the segment it was cut from.
 */
export function segmentsForActivity(
  transcript: TranscriptUtterance[],
  region: ActivityRegion,
): TranscriptUtterance[] {
  return chronological(
    transcript.filter((segment) => {
      if ((segment.speaker || "A") !== region.speaker) return false;
      const duration = segment.end_sample - segment.start_sample;
      if (duration <= 0) return false;
      const shared = overlap(region, segment);
      return shared / duration >= OWNED_COVERAGE;
    }),
  );
}

/**
 * Removes a speaker-lane rectangle, and with it any transcript segment that
 * rectangle owned. Segments spanning other rectangles are left in place.
 */
export function deleteActivity(
  annotation: Annotation,
  regionId: string,
): { annotation: Annotation; removedSegments: TranscriptUtterance[] } {
  const region = annotation.activities.find((item) => item.id === regionId);
  if (!region) return { annotation, removedSegments: [] };
  const owned = segmentsForActivity(annotation.transcript, region);
  const doomed = new Set(owned.map((segment) => segment.id));
  return {
    annotation: {
      ...annotation,
      activities: annotation.activities.filter((item) => item.id !== regionId),
      transcript: chronological(
        annotation.transcript.filter((segment) => !doomed.has(segment.id)),
      ),
    },
    removedSegments: owned,
  };
}

/**
 * Divides every segment that sits across more than one speaker-lane rectangle,
 * so each rectangle carries its own transcription.
 *
 * Applied once when a source is opened rather than on every edit: re-running it
 * on the result is a no-op, but doing it continuously would undo a join the
 * reviewer made on purpose. Segments with no word timings are left alone, since
 * there is nothing to divide their text by.
 */
export function splitAllByTurns(
  annotation: Annotation,
): { annotation: Annotation; dividedSegments: number; addedSegments: number } {
  let current = annotation;
  let dividedSegments = 0;
  let addedSegments = 0;
  // Iterating the original ids keeps the loop off the segments it creates.
  for (const segment of chronological(annotation.transcript)) {
    const result = splitSegmentByTurns(current, segment.id);
    if (!result.ok) continue;
    current = result.annotation;
    dividedSegments += 1;
    addedSegments += result.ids.length - 1;
  }
  return { annotation: current, dividedSegments, addedSegments };
}
