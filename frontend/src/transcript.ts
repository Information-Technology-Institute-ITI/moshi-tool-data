import { sampleId } from "./api";
import type { Annotation, Speaker, TranscriptUtterance } from "./types";

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

export function updateSegment(
  annotation: Annotation,
  id: string,
  patch: Partial<TranscriptUtterance>,
): Annotation {
  return withTranscript(
    annotation,
    annotation.transcript.map((item) => (item.id === id ? { ...item, ...patch } : item)),
  );
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
    annotation: withTranscript(annotation, [...annotation.transcript, segment]),
    id: segment.id,
  };
}

/** Deleted ids are never reused; the caller keeps undo history for recovery. */
export function deleteSegment(annotation: Annotation, id: string): Annotation {
  return withTranscript(
    annotation,
    annotation.transcript.filter((item) => item.id !== id),
  );
}

export type SplitResult =
  | { ok: true; annotation: Annotation; ids: [string, string] }
  | { ok: false; reason: string };

/**
 * Splits one segment into two adjacent timestamp ranges against the original
 * media. Both halves receive new ids. This changes annotation only: no audio
 * file is created, cut, or concatenated.
 */
export function splitSegment(
  annotation: Annotation,
  id: string,
  atSample: number,
  textOffset?: number,
): SplitResult {
  const segment = annotation.transcript.find((item) => item.id === id);
  if (!segment) return { ok: false, reason: "That segment no longer exists." };
  if (atSample <= segment.start_sample || atSample >= segment.end_sample) {
    return { ok: false, reason: "The split point must fall inside the segment." };
  }
  const cut = textOffset === undefined
    ? segment.text.length
    : Math.max(0, Math.min(segment.text.length, textOffset));
  const first = newSegment(
    segment.start_sample,
    atSample,
    (segment.speaker || "A") as Speaker,
    segment.text.slice(0, cut).trim(),
  );
  const second = newSegment(
    atSample,
    segment.end_sample,
    (segment.speaker || "A") as Speaker,
    segment.text.slice(cut).trim(),
  );
  // Both halves keep the original flags; the reviewer resolves them per half.
  first.quality_flags = [...segment.quality_flags];
  second.quality_flags = [...segment.quality_flags];
  first.model_text = segment.model_text;
  first.model_speaker = segment.model_speaker ?? null;
  second.model_speaker = segment.model_speaker ?? null;
  return {
    ok: true,
    annotation: withTranscript(annotation, [
      ...annotation.transcript.filter((item) => item.id !== id),
      first,
      second,
    ]),
    ids: [first.id, second.id],
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
    annotation: withTranscript(annotation, [
      ...annotation.transcript.filter(
        (item) => item.id !== firstId && item.id !== secondId,
      ),
      merged,
    ]),
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
