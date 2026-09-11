import type { Annotation, Speaker, TranscriptUtterance } from "./types";
import type { ReviewChapter } from "./productContracts";

export type ReviewFilters = {
  text: string;
  speaker: "all" | Speaker;
  flag: string;
  verification: "all" | "verified" | "unverified";
  alignment: "all" | TranscriptUtterance["alignment_status"];
  overlap: "all" | "yes" | "no";
  empty: "all" | "yes" | "no";
  edited: "all" | "edited" | "unchanged";
};

export const DEFAULT_REVIEW_FILTERS: ReviewFilters = {
  text: "",
  speaker: "all",
  flag: "",
  verification: "all",
  alignment: "all",
  overlap: "all",
  empty: "all",
  edited: "all",
};

/** Assign by segment start so one segment can never be displayed in two chapters. */
export function segmentsInChapter(
  segments: TranscriptUtterance[],
  chapter: ReviewChapter,
): TranscriptUtterance[] {
  return segments.filter((segment) => (
    segment.start_sample >= chapter.start_sample
    && segment.start_sample < chapter.end_sample
  ));
}

export function reviewPriority(segment: TranscriptUtterance, assistantSpeaker?: Speaker | null): number {
  const weights: Record<string, number> = {
    suspicious_character_sequence: 100,
    repeated_ngram: 90,
    overlapping_speech: 70,
    decode_disagreement: 55,
    low_average_log_probability: 45,
    abnormally_high_word_rate: 40,
    unaligned_words: 35,
    low_confidence_alignment: 25,
  };
  let score = [...new Set(segment.quality_flags)]
    .reduce((total, flag) => total + (weights[flag] || 10), 0);
  if (segment.speaker === assistantSpeaker) score += 20;
  if (segment.alignment_status !== "aligned") score += 30;
  if (segment.human_verified) score -= 1_000;
  return score;
}

export function segmentHasOverlap(annotation: Annotation, segment: TranscriptUtterance): boolean {
  const ownSpeaker = segment.speaker || "A";
  const otherSpeaker = ownSpeaker === "A" ? "B" : "A";
  return annotation.activities.some((activity) => (
    activity.speaker === otherSpeaker
    && activity.start_sample < segment.end_sample
    && activity.end_sample > segment.start_sample
  ));
}

export function segmentWasEdited(segment: TranscriptUtterance): boolean {
  return segment.text !== segment.model_text
    || (!!segment.model_speaker && segment.speaker !== segment.model_speaker);
}

export function matchesReviewFilters(
  annotation: Annotation,
  segment: TranscriptUtterance,
  filters: ReviewFilters,
): boolean {
  const query = filters.text.trim().toLocaleLowerCase();
  if (query && !segment.text.toLocaleLowerCase().includes(query)) return false;
  if (filters.speaker !== "all" && segment.speaker !== filters.speaker) return false;
  if (filters.flag && !segment.quality_flags.includes(filters.flag)) return false;
  if (filters.verification === "verified" && !segment.human_verified) return false;
  if (filters.verification === "unverified" && segment.human_verified) return false;
  if (filters.alignment !== "all" && segment.alignment_status !== filters.alignment) return false;
  const overlap = segmentHasOverlap(annotation, segment);
  if (filters.overlap === "yes" && !overlap) return false;
  if (filters.overlap === "no" && overlap) return false;
  const empty = !segment.text.trim();
  if (filters.empty === "yes" && !empty) return false;
  if (filters.empty === "no" && empty) return false;
  const edited = segmentWasEdited(segment);
  if (filters.edited === "edited" && !edited) return false;
  if (filters.edited === "unchanged" && edited) return false;
  return true;
}

export function reviewStats(segments: TranscriptUtterance[]) {
  const verified = segments.filter((item) => item.human_verified).length;
  const blocked = segments.filter((item) => (
    !item.text.trim() || !item.speaker || item.alignment_status === "unaligned"
  )).length;
  return {
    eligible: segments.length,
    verified,
    blocked,
    completed: segments.length > 0 && verified === segments.length && blocked === 0,
    percentage: segments.length ? Math.round(verified * 100 / segments.length) : 0,
  };
}

export function qualityQueue(annotation: Annotation) {
  return annotation.transcript
    .map((segment) => ({
      segment,
      priority: reviewPriority(segment, annotation.assistant_speaker),
    }))
    .filter((item) => item.priority > 0)
    .sort((left, right) => (
      right.priority - left.priority || left.segment.start_sample - right.segment.start_sample
    ));
}
