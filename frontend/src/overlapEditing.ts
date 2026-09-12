import { sampleId } from "./api";
import type { ActivityRegion, Annotation, ExclusionRegion, OverlapReview, Speaker } from "./types";

export type OverlapWindow = {
  id: string;
  speaker_a_activity_id: string;
  speaker_b_activity_id: string;
  start_sample: number;
  end_sample: number;
  review: OverlapReview;
};

function defaultReview(
  a: ActivityRegion,
  b: ActivityRegion,
  startSample: number,
  endSample: number,
): OverlapReview {
  return {
    id: sampleId("overlap_review"),
    speaker_a_activity_id: a.id,
    speaker_b_activity_id: b.id,
    start_sample: startSample,
    end_sample: endSample,
    classification: "unreviewed",
    training_decision: "needs_work",
    state: "current",
    note: "",
    recovery_artifact_ids: [],
  };
}

export function overlapWindows(annotation: Annotation): OverlapWindow[] {
  const activities = annotation.activities;
  const reviews = annotation.overlap_reviews || [];
  const aValues = activities.filter((item) => item.speaker === "A");
  const bValues = activities.filter((item) => item.speaker === "B");
  const windows: OverlapWindow[] = [];
  for (const a of aValues) {
    for (const b of bValues) {
      const start = Math.max(a.start_sample, b.start_sample);
      const end = Math.min(a.end_sample, b.end_sample);
      if (end <= start) continue;
      const existing = reviews.find((item) => (
        item.speaker_a_activity_id === a.id && item.speaker_b_activity_id === b.id
      ));
      const review = existing
        ? {
            ...existing,
            state: existing.start_sample === start && existing.end_sample === end
              ? existing.state
              : "stale" as const,
          }
        : defaultReview(a, b, start, end);
      windows.push({
        id: `${a.id}:${b.id}`,
        speaker_a_activity_id: a.id,
        speaker_b_activity_id: b.id,
        start_sample: start,
        end_sample: end,
        review,
      });
    }
  }
  return windows.sort((left, right) => left.start_sample - right.start_sample);
}

export function updateOverlapReview(
  annotation: Annotation,
  window: OverlapWindow,
  patch: Partial<OverlapReview>,
): Annotation {
  const reviews = annotation.overlap_reviews || [];
  const next = {
    ...window.review,
    ...patch,
    start_sample: window.start_sample,
    end_sample: window.end_sample,
    state: "current" as const,
  };
  return {
    ...annotation,
    overlap_reviews: [
      ...reviews.filter((item) => item.id !== next.id),
      next,
    ],
  };
}

function invalidateAffected(annotation: Annotation, start: number, end: number): Annotation {
  return {
    ...annotation,
    overlap_reviews: (annotation.overlap_reviews || []).map((review) => (
      review.end_sample > start && review.start_sample < end
        ? { ...review, state: "stale" as const, training_decision: "needs_work" as const }
        : review
    )),
    transcript: annotation.transcript.map((segment) => (
      segment.end_sample > start && segment.start_sample < end
        ? {
            ...segment,
            human_verified: false,
            quality_flags: [...new Set([...segment.quality_flags, "overlap_changed"])],
          }
        : segment
    )),
  };
}

export function setOverlapBounds(
  annotation: Annotation,
  window: OverlapWindow,
  startSample: number,
  endSample: number,
): Annotation | null {
  if (startSample < 0 || endSample <= startSample) return null;
  const a = annotation.activities.find((item) => item.id === window.speaker_a_activity_id);
  const b = annotation.activities.find((item) => item.id === window.speaker_b_activity_id);
  if (!a || !b) return null;
  const oldStart = window.start_sample;
  const oldEnd = window.end_sample;
  const activities = annotation.activities.map((item) => {
    if (item.id !== a.id && item.id !== b.id) return item;
    // Move the old limiting boundary. When extending beyond the other
    // contributor, extend that contributor too so the derived intersection is
    // exactly the requested range.
    const nextStart = item.start_sample === oldStart || startSample < item.start_sample
      ? startSample
      : item.start_sample;
    const nextEnd = item.end_sample === oldEnd || endSample > item.end_sample
      ? endSample
      : item.end_sample;
    if (nextEnd <= nextStart) return item;
    return { ...item, start_sample: nextStart, end_sample: nextEnd, origin: "manual" as const };
  });
  const invalidated = invalidateAffected(
    { ...annotation, activities },
    Math.min(oldStart, startSample),
    Math.max(oldEnd, endSample),
  );
  return invalidated;
}

function subtractRange(region: ActivityRegion, start: number, end: number): ActivityRegion[] {
  if (region.end_sample <= start || region.start_sample >= end) return [region];
  const values: ActivityRegion[] = [];
  if (region.start_sample < start) values.push({ ...region, end_sample: start, origin: "manual" });
  if (region.end_sample > end) values.push({
    ...region,
    id: values.length ? sampleId("activity") : region.id,
    start_sample: end,
    origin: "manual",
  });
  return values;
}

export function removeOverlap(
  annotation: Annotation,
  window: OverlapWindow,
  keep: Speaker | "neither",
): Annotation {
  const removeIds = new Set<string>();
  if (keep !== "A") removeIds.add(window.speaker_a_activity_id);
  if (keep !== "B") removeIds.add(window.speaker_b_activity_id);
  const activities = annotation.activities.flatMap((item) => (
    removeIds.has(item.id)
      ? subtractRange(item, window.start_sample, window.end_sample)
      : [item]
  ));
  const exclusions: ExclusionRegion[] = keep === "neither"
    ? [
        ...annotation.exclusions,
        {
          id: sampleId("exclude"),
          kind: "unusable",
          start_sample: window.start_sample,
          end_sample: window.end_sample,
          note: "Overlap marked as neither speaker usable",
        },
      ]
    : annotation.exclusions;
  return invalidateAffected({ ...annotation, activities, exclusions }, window.start_sample, window.end_sample);
}

export function splitOverlap(
  annotation: Annotation,
  window: OverlapWindow,
  atSample: number,
): Annotation | null {
  if (atSample <= window.start_sample || atSample >= window.end_sample) return null;
  const ids = new Set([window.speaker_a_activity_id, window.speaker_b_activity_id]);
  const activities = annotation.activities.flatMap((item) => {
    if (!ids.has(item.id)) return [item];
    return [
      { ...item, end_sample: atSample, origin: "manual" as const },
      { ...item, id: sampleId("activity"), start_sample: atSample, origin: "manual" as const },
    ];
  });
  return invalidateAffected({ ...annotation, activities }, window.start_sample, window.end_sample);
}

export function mergeOverlapWithNext(
  annotation: Annotation,
  first: OverlapWindow,
  second: OverlapWindow,
): Annotation | null {
  if (Math.abs(first.end_sample - second.start_sample) > 1) return null;
  const involved = new Set([
    first.speaker_a_activity_id,
    first.speaker_b_activity_id,
    second.speaker_a_activity_id,
    second.speaker_b_activity_id,
  ]);
  const selected = annotation.activities.filter((item) => involved.has(item.id));
  const merged: ActivityRegion[] = (["A", "B"] as Speaker[]).map((speaker) => {
    const values = selected.filter((item) => item.speaker === speaker);
    return {
      ...values[0],
      id: sampleId("activity"),
      start_sample: Math.min(...values.map((item) => item.start_sample)),
      end_sample: Math.max(...values.map((item) => item.end_sample)),
      origin: "manual" as const,
    };
  });
  const activities = [
    ...annotation.activities.filter((item) => !involved.has(item.id)),
    ...merged,
  ];
  return invalidateAffected({ ...annotation, activities }, first.start_sample, second.end_sample);
}
