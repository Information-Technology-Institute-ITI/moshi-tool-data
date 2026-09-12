import { useMemo } from "react";
import {
  mergeOverlapWithNext,
  overlapWindows,
  removeOverlap,
  setOverlapBounds,
  splitOverlap,
  updateOverlapReview,
  type OverlapWindow,
} from "../overlapEditing";
import type { Annotation, OverlapReview } from "../types";

const SAMPLE_RATE = 24_000;

export default function OverlapReviewPanel({
  annotation,
  durationSamples,
  selectedOverlapId,
  playheadSample,
  frameRate,
  disabled,
  onChange,
  onPlay,
  onAudition,
  onSelectOverlap,
  onError,
}: {
  annotation: Annotation;
  durationSamples: number;
  selectedOverlapId: string | null;
  playheadSample: number;
  frameRate: number;
  disabled: boolean;
  onChange: (annotation: Annotation) => void;
  onPlay: (startSample: number, endSample: number) => void;
  onAudition: (mode: "mixed" | "speaker_a" | "speaker_b") => void;
  onSelectOverlap: (id: string) => void;
  onError: (message: string) => void;
}) {
  const windows = useMemo(() => overlapWindows(annotation), [annotation]);
  const selectedIndex = Math.max(0, windows.findIndex((item) => item.id === selectedOverlapId));
  const selected = windows[selectedIndex] || null;
  const frameSamples = Math.max(1, Math.round(SAMPLE_RATE / frameRate));
  const affectedSegments = selected ? annotation.transcript.filter((item) => (
    item.start_sample < selected.end_sample && item.end_sample > selected.start_sample
  )).length : 0;
  const wordBoundaries = annotation.aligned_words.flatMap((word) => (
    word.start === null || word.start === undefined || word.end === null || word.end === undefined
      ? []
      : [Math.round(word.start * SAMPLE_RATE), Math.round(word.end * SAMPLE_RATE)]
  ));
  const nearestBoundary = (sample: number) => wordBoundaries.length
    ? wordBoundaries.reduce((best, value) => (
        Math.abs(value - sample) < Math.abs(best - sample) ? value : best
      ))
    : sample;
  const crossesWord = selected ? annotation.aligned_words.some((word) => {
    if (word.start === null || word.start === undefined || word.end === null || word.end === undefined) return false;
    const start = word.start * SAMPLE_RATE;
    const end = word.end * SAMPLE_RATE;
    return (start < selected.start_sample && end > selected.start_sample)
      || (start < selected.end_sample && end > selected.end_sample);
  }) : false;

  function revise(patch: Partial<OverlapReview>) {
    if (!selected || disabled) return;
    onChange(updateOverlapReview(annotation, selected, patch));
  }

  function bounds(startSeconds: number, endSeconds: number) {
    if (!selected || disabled) return;
    if (!Number.isFinite(startSeconds) || !Number.isFinite(endSeconds)
      || startSeconds < 0 || endSeconds * SAMPLE_RATE > durationSamples) {
      onError("Overlap bounds must remain inside the recording.");
      return;
    }
    const next = setOverlapBounds(
      annotation,
      selected,
      Math.round(startSeconds * SAMPLE_RATE),
      Math.round(endSeconds * SAMPLE_RATE),
    );
    if (!next) onError("Overlap bounds must be ordered and remain inside the recording.");
    else onChange(next);
  }

  return (
    <section className="rail-section overlap-review" aria-labelledby="overlap-review-heading">
      <h3 id="overlap-review-heading">Overlap review</h3>
      <div className="overlap-summary">
        <strong>{windows.length}</strong> overlap{windows.length === 1 ? "" : "s"}
        <span>{windows.filter((item) => item.review.classification !== "unreviewed" && item.review.state === "current" && item.review.training_decision !== "needs_work").length} ready</span>
      </div>
      {!windows.length && <small>No simultaneous A/B activity in this chapter or source.</small>}
      {!!windows.length && (
        <>
          <select
            aria-label="Select overlap"
            value={selected?.id || ""}
            onChange={(event) => onSelectOverlap(event.target.value)}
          >
            {windows.map((window, index) => (
              <option key={window.id} value={window.id}>
                {index + 1}. {(window.start_sample / SAMPLE_RATE).toFixed(2)}–{(window.end_sample / SAMPLE_RATE).toFixed(2)}s
                {window.review.state === "stale" ? " · stale" : ""}
              </option>
            ))}
          </select>
          {selected && (
            <div className="overlap-editor-grid">
              <button type="button" onClick={() => onPlay(selected.start_sample, selected.end_sample)}>
                Play overlap
              </button>
              <div className="overlap-audition">
                <button type="button" onClick={() => { onAudition("mixed"); onPlay(selected.start_sample, selected.end_sample); }}>Mixed</button>
                <button type="button" disabled={annotation.speaker_channel_map.A === undefined} onClick={() => { onAudition("speaker_a"); onPlay(selected.start_sample, selected.end_sample); }}>Speaker A</button>
                <button type="button" disabled={annotation.speaker_channel_map.B === undefined} onClick={() => { onAudition("speaker_b"); onPlay(selected.start_sample, selected.end_sample); }}>Speaker B</button>
              </div>
              <label>
                Start (s)
                <input
                  type="number"
                  step="0.01"
                  value={(selected.start_sample / SAMPLE_RATE).toFixed(2)}
                  disabled={disabled}
                  onChange={(event) => bounds(Number(event.target.value), selected.end_sample / SAMPLE_RATE)}
                />
              </label>
              <label>
                End (s)
                <input
                  type="number"
                  step="0.01"
                  value={(selected.end_sample / SAMPLE_RATE).toFixed(2)}
                  disabled={disabled}
                  onChange={(event) => bounds(selected.start_sample / SAMPLE_RATE, Number(event.target.value))}
                />
              </label>
              <div className="overlap-nudges">
                <button type="button" disabled={disabled} onClick={() => bounds((selected.start_sample - frameSamples) / SAMPLE_RATE, selected.end_sample / SAMPLE_RATE)}>Start − frame</button>
                <button type="button" disabled={disabled} onClick={() => bounds((selected.start_sample + frameSamples) / SAMPLE_RATE, selected.end_sample / SAMPLE_RATE)}>Start + frame</button>
                <button type="button" disabled={disabled} onClick={() => bounds(selected.start_sample / SAMPLE_RATE, (selected.end_sample - frameSamples) / SAMPLE_RATE)}>End − frame</button>
                <button type="button" disabled={disabled} onClick={() => bounds(selected.start_sample / SAMPLE_RATE, (selected.end_sample + frameSamples) / SAMPLE_RATE)}>End + frame</button>
              </div>
              {!!wordBoundaries.length && (
                <div className="overlap-nudges">
                  <button type="button" disabled={disabled} onClick={() => bounds(nearestBoundary(selected.start_sample) / SAMPLE_RATE, selected.end_sample / SAMPLE_RATE)}>Snap start to word</button>
                  <button type="button" disabled={disabled} onClick={() => bounds(selected.start_sample / SAMPLE_RATE, nearestBoundary(selected.end_sample) / SAMPLE_RATE)}>Snap end to word</button>
                </div>
              )}
              <label>
                Classification
                <select
                  value={selected.review.classification}
                  disabled={disabled}
                  onChange={(event) => revise({ classification: event.target.value as OverlapReview["classification"] })}
                >
                  <option value="unreviewed">Unreviewed</option>
                  <option value="confirmed">Confirmed overlap</option>
                  <option value="false_positive">False positive</option>
                  <option value="third_speaker">Third speaker</option>
                  <option value="noise">Noise</option>
                  <option value="unintelligible">Unintelligible</option>
                </select>
              </label>
              <label>
                Training use
                <select
                  value={selected.review.training_decision}
                  disabled={disabled}
                  onChange={(event) => revise({ training_decision: event.target.value as OverlapReview["training_decision"] })}
                >
                  <option value="needs_work">Needs work</option>
                  <option value="raw">Use mixed audio</option>
                  <option value="separate">Use separated channels</option>
                  <option value="exclude">Exclude from training</option>
                </select>
              </label>
              <label>
                Review note
                <textarea
                  value={selected.review.note}
                  disabled={disabled}
                  onChange={(event) => revise({ note: event.target.value })}
                />
              </label>
              <div className="overlap-actions">
                <button type="button" disabled={disabled} onClick={() => onChange(removeOverlap(annotation, selected, "A"))}>Remove overlap · A only</button>
                <button type="button" disabled={disabled} onClick={() => onChange(removeOverlap(annotation, selected, "B"))}>Remove overlap · B only</button>
                <button type="button" disabled={disabled} onClick={() => onChange(removeOverlap(annotation, selected, "neither"))}>Neither usable</button>
                <button
                  type="button"
                  disabled={disabled || playheadSample <= selected.start_sample || playheadSample >= selected.end_sample}
                  onClick={() => {
                    const next = splitOverlap(annotation, selected, playheadSample);
                    if (next) onChange(next);
                  }}
                >
                  Split at playhead
                </button>
                <button
                  type="button"
                  disabled={disabled || !windows[selectedIndex + 1] || Math.abs(selected.end_sample - windows[selectedIndex + 1].start_sample) > 1}
                  onClick={() => {
                    const nextWindow = windows[selectedIndex + 1];
                    const next = nextWindow && mergeOverlapWithNext(annotation, selected, nextWindow);
                    if (next) onChange(next);
                  }}
                >
                  Merge with next
                </button>
              </div>
              <small className="overlap-warning">
                Boundary changes never delete transcript text. {affectedSegments} affected segment{affectedSegments === 1 ? "" : "s"} will become unverified and dependent review work becomes stale.
              </small>
              {crossesWord && <small className="overlap-warning">A boundary currently crosses an aligned word. Snap it to a word boundary before verification.</small>}
            </div>
          )}
        </>
      )}
    </section>
  );
}
