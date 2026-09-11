import { describe, expect, it } from "vitest";
import {
  DEFAULT_REVIEW_FILTERS,
  matchesReviewFilters,
  qualityQueue,
  reviewPriority,
  reviewStats,
  segmentsInChapter,
} from "./reviewWorkflow";
import type { Annotation, TranscriptUtterance } from "./types";

function segment(id: string, update: Partial<TranscriptUtterance> = {}): TranscriptUtterance {
  return {
    id,
    speaker: "A",
    start_sample: 0,
    end_sample: 24_000,
    text: "draft words",
    model_text: "draft words",
    model_speaker: "A",
    quality_flags: [],
    alignment_status: "aligned",
    human_verified: false,
    review_candidates: [],
    ...update,
  };
}

function document(transcript: TranscriptUtterance[]): Annotation {
  return {
    source_id: "source_1",
    version: 4,
    assistant_speaker: "B",
    channel_routing_mode: "mono",
    channel_routing_verified: false,
    speaker_channel_map: {},
    activities_finalized: false,
    activities: [
      { id: "b", speaker: "B", start_sample: 10_000, end_sample: 20_000, origin: "model" },
    ],
    speaker_references: [],
    exclusions: [],
    transcript,
    aligned_words: [],
    note: "",
  };
}

describe("review workflow", () => {
  it("uses the same quality priority weights as the saved server queue", () => {
    const risky = segment("risky", {
      speaker: "B",
      quality_flags: ["repeated_ngram", "decode_disagreement"],
      alignment_status: "low_confidence",
    });
    expect(reviewPriority(risky, "B")).toBe(195);
    expect(reviewPriority({ ...risky, human_verified: true }, "B")).toBe(-805);
  });

  it("sorts the working queue and removes verified items", () => {
    const annotation = document([
      segment("low", { quality_flags: ["low_confidence_alignment"] }),
      segment("high", { quality_flags: ["suspicious_character_sequence"] }),
      segment("done", { quality_flags: ["repeated_ngram"], human_verified: true }),
    ]);
    expect(qualityQueue(annotation).map((item) => item.segment.id)).toEqual(["high", "low"]);
  });

  it("combines text, speaker, overlap, verification, and edited filters", () => {
    const edited = segment("edited", { text: "Corrected Arabic", human_verified: true });
    const annotation = document([edited]);
    expect(matchesReviewFilters(annotation, edited, {
      ...DEFAULT_REVIEW_FILTERS,
      text: "arabic",
      speaker: "A",
      verification: "verified",
      overlap: "yes",
      edited: "edited",
    })).toBe(true);
  });

  it("reports verified progress and prevents invalid completion", () => {
    expect(reviewStats([
      segment("one", { human_verified: true }),
      segment("two", { text: "", human_verified: true }),
    ])).toEqual({ eligible: 2, verified: 2, blocked: 1, completed: false, percentage: 100 });
  });

  it("assigns a boundary-crossing segment to one chapter only", () => {
    const crossing = segment("crossing", { start_sample: 20_000, end_sample: 30_000 });
    const first = { id: "c1", ordinal: 1, start_sample: 0, end_sample: 24_000, boundary_reason: "manual" as const };
    const second = { id: "c2", ordinal: 2, start_sample: 24_000, end_sample: 48_000, boundary_reason: "manual" as const };
    expect(segmentsInChapter([crossing], first)).toEqual([crossing]);
    expect(segmentsInChapter([crossing], second)).toEqual([]);
  });
});
