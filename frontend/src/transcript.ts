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
 * Flags the GPU produces and the reviewer may add or remove. Flags are editable
 * metadata only: they never mute audio, start processing, or gate approval.
 */
export const SUPPORTED_QUALITY_FLAGS = [
  "abnormally_high_word_rate",
  "abnormally_low_word_rate",
  "decode_disagreement",
  "low_average_log_probability",
  "overlapping_speech",
  "repeated_ngram",
  "suspicious_character_sequence",
] as const;

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

export function toggleFlag(
  annotation: Annotation,
  id: string,
  flag: string,
): Annotation {
  return withTranscript(
    annotation,
    annotation.transcript.map((item) => {
      if (item.id !== id) return item;
      const flags = item.quality_flags.includes(flag)
        ? item.quality_flags.filter((value) => value !== flag)
        : [...item.quality_flags, flag];
      return { ...item, quality_flags: flags };
    }),
  );
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
  | { ok: true; annotation: Annotation; id: string }
  | { ok: false; reason: string };

/**
 * Joins two same-speaker segments using union bounds and chronological text
 * order. Cross-speaker joins fail safely and change nothing.
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
  if (left.speaker !== right.speaker) {
    return { ok: false, reason: "Only segments with the same speaker can be joined." };
  }
  const ordered = chronological([left, right]);
  const merged = newSegment(
    Math.min(left.start_sample, right.start_sample),
    Math.max(left.end_sample, right.end_sample),
    (left.speaker || "A") as Speaker,
    ordered.map((item) => item.text.trim()).filter(Boolean).join(" "),
  );
  merged.model_text = ordered
    .map((item) => item.model_text.trim())
    .filter(Boolean)
    .join(" ");
  merged.model_speaker = left.model_speaker ?? right.model_speaker ?? null;
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
