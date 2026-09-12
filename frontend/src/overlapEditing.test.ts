import { describe, expect, it } from "vitest";
import {
  mergeOverlapWithNext,
  overlapWindows,
  removeOverlap,
  setOverlapBounds,
  splitOverlap,
  updateOverlapReview,
} from "./overlapEditing";
import type { Annotation } from "./types";

function annotation(): Annotation {
  return {
    source_id: "source",
    version: 1,
    assistant_speaker: "A",
    channel_routing_mode: "mono",
    channel_routing_verified: false,
    speaker_channel_map: {},
    activities_finalized: true,
    activities: [
      { id: "a", speaker: "A", start_sample: 0, end_sample: 100, origin: "model" },
      { id: "b", speaker: "B", start_sample: 40, end_sample: 140, origin: "model" },
    ],
    speaker_references: [],
    exclusions: [],
    transcript: [
      { id: "u", speaker: "A", start_sample: 0, end_sample: 140, text: "kept", model_text: "", quality_flags: [], alignment_status: "aligned", human_verified: true, review_candidates: [] },
    ],
    overlap_reviews: [],
    aligned_words: [],
    note: "",
  };
}

describe("overlap editing", () => {
  it("classifies locally without changing transcript text", () => {
    const source = annotation();
    const window = overlapWindows(source)[0];
    const next = updateOverlapReview(source, window, {
      classification: "confirmed",
      training_decision: "raw",
    });

    expect(next.transcript).toEqual(source.transcript);
    expect(next.overlap_reviews?.[0]).toMatchObject({
      classification: "confirmed",
      training_decision: "raw",
      state: "current",
    });
  });

  it("moves the limiting boundaries and unverifies affected text", () => {
    const source = annotation();
    const next = setOverlapBounds(source, overlapWindows(source)[0], 50, 90)!;

    expect(overlapWindows(next)[0]).toMatchObject({ start_sample: 50, end_sample: 90 });
    expect(next.transcript[0].text).toBe("kept");
    expect(next.transcript[0].human_verified).toBe(false);
  });

  it("extends both contributors when the overlap exceeds either one", () => {
    const source = annotation();
    const next = setOverlapBounds(source, overlapWindows(source)[0], 20, 120)!;

    expect(overlapWindows(next)[0]).toMatchObject({ start_sample: 20, end_sample: 120 });
    expect(next.transcript[0].text).toBe("kept");
  });

  it("semantic removal preserves text and can exclude neither", () => {
    const source = annotation();
    const next = removeOverlap(source, overlapWindows(source)[0], "neither");

    expect(overlapWindows(next)).toHaveLength(0);
    expect(next.transcript[0].text).toBe("kept");
    expect(next.exclusions[0]).toMatchObject({ start_sample: 40, end_sample: 100 });
  });

  it("splits and merges an overlap without changing transcript text", () => {
    const source = annotation();
    const split = splitOverlap(source, overlapWindows(source)[0], 70)!;
    const windows = overlapWindows(split);
    expect(windows).toHaveLength(2);

    const merged = mergeOverlapWithNext(split, windows[0], windows[1])!;
    expect(overlapWindows(merged)).toHaveLength(1);
    expect(merged.transcript[0].text).toBe("kept");
  });
});
