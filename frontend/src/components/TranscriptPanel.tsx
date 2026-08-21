import { useMemo, useState } from "react";
import { seconds } from "../api";
import {
  SUPPORTED_QUALITY_FLAGS,
  boundsError,
  chronological,
  flagLabel,
  neighbourAfter,
} from "../transcript";
import type { Annotation, Speaker, TranscriptUtterance } from "../types";

const SAMPLE_RATE = 24_000;

function toSeconds(sample: number): string {
  return (sample / SAMPLE_RATE).toFixed(2);
}

function fromSeconds(value: string): number {
  return Math.round(Number(value) * SAMPLE_RATE);
}

type Props = {
  annotation: Annotation;
  durationSamples: number;
  selectedId: string | null;
  /** Entry ids intersecting the current timeline selection, if any. */
  filteredIds: string[] | null;
  playheadSample: number;
  readOnly: boolean;
  onSelect: (id: string) => void;
  onPlay: (segment: TranscriptUtterance, loop: boolean) => void;
  onChange: (next: Annotation) => void;
  onSplit: (id: string, atSample: number, textOffset: number) => void;
  onJoin: (firstId: string, secondId: string) => void;
  onDelete: (id: string) => void;
  onAdd: () => void;
  onClearFilter: () => void;
};

export default function TranscriptPanel({
  annotation,
  durationSamples,
  selectedId,
  filteredIds,
  playheadSample,
  readOnly,
  onSelect,
  onPlay,
  onChange,
  onSplit,
  onJoin,
  onDelete,
  onAdd,
  onClearFilter,
}: Props) {
  const ordered = useMemo(() => chronological(annotation.transcript), [annotation.transcript]);
  const visible = useMemo(
    () => (filteredIds ? ordered.filter((item) => filteredIds.includes(item.id)) : ordered),
    [ordered, filteredIds],
  );
  const selected = ordered.find((item) => item.id === selectedId) || null;

  return (
    <section className="transcript-panel">
      <header className="transcript-header">
        <div>
          <span className="eyebrow">Draft transcript</span>
          <h2>{ordered.length} segments</h2>
        </div>
        <div className="transcript-header-actions">
          {filteredIds && (
            <button type="button" onClick={onClearFilter}>
              Clear time filter ({visible.length})
            </button>
          )}
          {!readOnly && (
            <button type="button" onClick={onAdd}>Add segment</button>
          )}
        </div>
      </header>

      <ol className="transcript-list">
        {visible.map((item, index) => {
          const isPlaying =
            playheadSample >= item.start_sample && playheadSample < item.end_sample;
          return (
            <li key={item.id}>
              <button
                type="button"
                className={[
                  "transcript-entry",
                  item.id === selectedId ? "selected" : "",
                  isPlaying ? "playing" : "",
                ].filter(Boolean).join(" ")}
                aria-current={item.id === selectedId ? "true" : undefined}
                onClick={() => {
                  onSelect(item.id);
                  onPlay(item, false);
                }}
              >
                <span className="transcript-index">{index + 1}</span>
                <span className={`transcript-speaker speaker-${(item.speaker || "a").toLowerCase()}`}>
                  {item.speaker || "?"}
                </span>
                <span className="transcript-body">
                  <span className="transcript-text" dir="auto">
                    {item.text || <em>(empty)</em>}
                  </span>
                  <span className="transcript-meta">
                    {toSeconds(item.start_sample)}–{toSeconds(item.end_sample)}s
                    {item.quality_flags.map((flag) => (
                      <span className="flag-chip" key={flag}>{flagLabel(flag)}</span>
                    ))}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
        {!visible.length && (
          <li className="transcript-empty">
            {ordered.length
              ? "No segments fall inside the selected time range."
              : "This source has no transcript segments yet."}
          </li>
        )}
      </ol>

      {selected && (
        <SegmentInspector
          key={selected.id}
          segment={selected}
          annotation={annotation}
          durationSamples={durationSamples}
          playheadSample={playheadSample}
          readOnly={readOnly}
          onPlay={onPlay}
          onChange={onChange}
          onSplit={onSplit}
          onJoin={onJoin}
          onDelete={onDelete}
        />
      )}
    </section>
  );
}

function SegmentInspector({
  segment,
  annotation,
  durationSamples,
  playheadSample,
  readOnly,
  onPlay,
  onChange,
  onSplit,
  onJoin,
  onDelete,
}: {
  segment: TranscriptUtterance;
  annotation: Annotation;
  durationSamples: number;
  playheadSample: number;
  readOnly: boolean;
  onPlay: (segment: TranscriptUtterance, loop: boolean) => void;
  onChange: (next: Annotation) => void;
  onSplit: (id: string, atSample: number, textOffset: number) => void;
  onJoin: (firstId: string, secondId: string) => void;
  onDelete: (id: string) => void;
}) {
  const [start, setStart] = useState(toSeconds(segment.start_sample));
  const [end, setEnd] = useState(toSeconds(segment.end_sample));
  const [caret, setCaret] = useState(segment.text.length);
  const next = neighbourAfter(annotation.transcript, segment.id);
  const canJoin = !!next && next.speaker === segment.speaker;
  const splitInside =
    playheadSample > segment.start_sample && playheadSample < segment.end_sample;

  function patch(update: Partial<TranscriptUtterance>) {
    onChange({
      ...annotation,
      transcript: annotation.transcript.map((item) =>
        item.id === segment.id ? { ...item, ...update } : item,
      ),
    });
  }

  function commitBounds() {
    const nextStart = fromSeconds(start);
    const nextEnd = fromSeconds(end);
    const problem = boundsError(nextStart, nextEnd, durationSamples);
    if (problem) {
      setStart(toSeconds(segment.start_sample));
      setEnd(toSeconds(segment.end_sample));
      return;
    }
    patch({ start_sample: nextStart, end_sample: nextEnd });
  }

  return (
    <div className="segment-inspector card">
      <header>
        <span className="eyebrow">Selected segment</span>
        <div className="inspector-play">
          <button type="button" onClick={() => onPlay(segment, false)}>Play</button>
          <button type="button" onClick={() => onPlay(segment, true)}>Loop</button>
        </div>
      </header>

      <label className="wide">
        Text
        <textarea
          dir="auto"
          rows={3}
          value={segment.text}
          readOnly={readOnly}
          onChange={(event) => {
            setCaret(event.target.selectionStart ?? event.target.value.length);
            patch({ text: event.target.value });
          }}
          onSelect={(event) =>
            setCaret((event.target as HTMLTextAreaElement).selectionStart ?? 0)
          }
        />
      </label>
      {!segment.text.trim() && (
        <p className="inspector-warning">
          This segment has no text. It can be saved, but review it before finishing.
        </p>
      )}
      {segment.model_text && segment.model_text !== segment.text && (
        <p className="model-text" dir="auto">
          <strong>Model suggested:</strong> {segment.model_text}
        </p>
      )}

      <div className="inspector-grid">
        <label>
          Speaker
          <select
            value={segment.speaker || ""}
            disabled={readOnly}
            onChange={(event) => patch({ speaker: event.target.value as Speaker })}
          >
            <option value="A">Speaker A</option>
            <option value="B">Speaker B</option>
          </select>
        </label>
        <label>
          Start (s)
          <input
            type="number"
            step="0.01"
            min="0"
            value={start}
            readOnly={readOnly}
            onChange={(event) => setStart(event.target.value)}
            onBlur={commitBounds}
          />
        </label>
        <label>
          End (s)
          <input
            type="number"
            step="0.01"
            min="0"
            value={end}
            readOnly={readOnly}
            onChange={(event) => setEnd(event.target.value)}
            onBlur={commitBounds}
          />
        </label>
      </div>

      <fieldset className="flag-editor">
        <legend>Quality flags</legend>
        {SUPPORTED_QUALITY_FLAGS.map((flag) => (
          <label className="checkbox" key={flag}>
            <input
              type="checkbox"
              checked={segment.quality_flags.includes(flag)}
              disabled={readOnly}
              onChange={() =>
                patch({
                  quality_flags: segment.quality_flags.includes(flag)
                    ? segment.quality_flags.filter((value) => value !== flag)
                    : [...segment.quality_flags, flag],
                })
              }
            />
            {flagLabel(flag)}
          </label>
        ))}
      </fieldset>

      {!readOnly && (
        <div className="inspector-actions">
          <button
            type="button"
            disabled={!splitInside}
            title={
              splitInside
                ? "Split at the playhead and the text cursor"
                : "Move the playhead inside this segment to split it"
            }
            onClick={() => onSplit(segment.id, playheadSample, caret)}
          >
            Split here
          </button>
          <button
            type="button"
            disabled={!canJoin}
            title={
              next
                ? canJoin
                  ? "Join with the next segment"
                  : "Only segments with the same speaker can be joined"
                : "There is no following segment"
            }
            onClick={() => next && onJoin(segment.id, next.id)}
          >
            Join with next
          </button>
          <button
            type="button"
            className="danger-soft"
            onClick={() => onDelete(segment.id)}
          >
            Delete segment
          </button>
        </div>
      )}
      <p className="inspector-note">
        Splitting and joining change timestamps and text only. The original media is
        never cut or re-encoded. Duration {seconds(segment.end_sample - segment.start_sample)}s.
      </p>
    </div>
  );
}
