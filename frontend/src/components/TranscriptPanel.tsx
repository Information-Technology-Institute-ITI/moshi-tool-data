import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { seconds } from "../api";
import {
  boundsError,
  chronological,
  flagLabel,
  neighbourAfter,
  overlapsForAnnotation,
  overlapsForSegment,
  resolveSplitSample,
  updateSegment,
  wordsForSegment,
} from "../transcript";
import type { Annotation, Speaker, TranscriptUtterance } from "../types";

const SAMPLE_RATE = 24_000;

function toSeconds(sample: number): string {
  return (sample / SAMPLE_RATE).toFixed(2);
}

function fromSeconds(value: string): number {
  return Math.round(Number(value) * SAMPLE_RATE);
}

function editableSeconds(sample: number): string {
  return String(sample / SAMPLE_RATE);
}

export type TranscriptPanelHandle = {
  /** Returns the visible document after applying valid focused timing inputs. */
  prepareForSave: () => Annotation | null;
};

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
  /** Typing in the text box: recorded locally, not saved until committed. */
  onChangeText: (next: Annotation) => void;
  /** The reviewer has finished with the text box, so the edit can be sent. */
  onCommitText: () => void;
  onSplit: (id: string, atSample: number, textOffset: number) => void;
  onAddOverlap: (id: string) => void;
  onAddAllOverlaps: () => void;
  onJoin: (firstId: string, secondId: string) => void;
  onDelete: (id: string) => void;
  onAdd: () => void;
  onClearFilter: () => void;
  onPendingBoundsChange: (pending: boolean) => void;
  /** Optional left-rail target for the selected-segment inspector. */
  inspectorTarget?: HTMLElement | null;
};

const TranscriptPanel = forwardRef<TranscriptPanelHandle, Props>(function TranscriptPanel({
  annotation,
  durationSamples,
  selectedId,
  filteredIds,
  playheadSample,
  readOnly,
  onSelect,
  onPlay,
  onChange,
  onChangeText,
  onCommitText,
  onSplit,
  onAddOverlap,
  onAddAllOverlaps,
  onJoin,
  onDelete,
  onAdd,
  onClearFilter,
  onPendingBoundsChange,
  inspectorTarget,
}, ref) {
  const prepareSelected = useRef<null | (() => Annotation | null)>(null);
  useImperativeHandle(ref, () => ({
    prepareForSave: () => prepareSelected.current?.() ?? annotation,
  }), [annotation]);
  const ordered = useMemo(() => chronological(annotation.transcript), [annotation.transcript]);
  const visible = useMemo(
    () => (filteredIds ? ordered.filter((item) => filteredIds.includes(item.id)) : ordered),
    [ordered, filteredIds],
  );
  const selected = ordered.find((item) => item.id === selectedId) || null;
  // Stepping follows the visible list, so a time filter does not jump the user
  // to an entry that is not on screen.
  const position = selected ? visible.findIndex((item) => item.id === selected.id) : -1;
  const previousEntry = position > 0 ? visible[position - 1] : null;
  const nextEntry =
    position >= 0 && position + 1 < visible.length ? visible[position + 1] : null;
  // Stretches where the lanes show two speakers but the transcript has only one
  // segment. Each one is a segment the quieter speaker is still missing.
  const missingOverlaps = useMemo(
    () => overlapsForAnnotation(annotation),
    [annotation],
  );
  const missingOverlapCount = missingOverlaps.reduce(
    (total, entry) => total + entry.windows.length,
    0,
  );
  const selectedInspector = selected ? (
    <SegmentInspector
      key={selected.id}
      segment={selected}
      position={position}
      total={visible.length}
      previousEntry={previousEntry}
      nextEntry={nextEntry}
      onStep={(entry) => {
        onSelect(entry.id);
        onPlay(entry, false);
      }}
      annotation={annotation}
      durationSamples={durationSamples}
      playheadSample={playheadSample}
      readOnly={readOnly}
      onPlay={onPlay}
      onChange={onChange}
      onChangeText={onChangeText}
      onCommitText={onCommitText}
      onSplit={onSplit}
      onAddOverlap={onAddOverlap}
      onJoin={onJoin}
      onDelete={onDelete}
      onRegisterPrepare={(prepare) => {
        prepareSelected.current = prepare;
      }}
      onPendingBoundsChange={onPendingBoundsChange}
    />
  ) : null;

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
          {!readOnly && missingOverlapCount > 0 && (
            <button
              type="button"
              title={
                `The timeline shows both speakers over ${missingOverlapCount} stretch`
                + `${missingOverlapCount === 1 ? "" : "es"} that only one segment covers.`
                + " This gives the other speaker their own segment for each."
              }
              onClick={onAddAllOverlaps}
            >
              Add overlap segments ({missingOverlapCount})
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

      {inspectorTarget ? createPortal(selectedInspector, inspectorTarget) : selectedInspector}
    </section>
  );
});

export default TranscriptPanel;

function SegmentInspector({
  segment,
  position,
  total,
  previousEntry,
  nextEntry,
  onStep,
  annotation,
  durationSamples,
  playheadSample,
  readOnly,
  onPlay,
  onChange,
  onChangeText,
  onCommitText,
  onSplit,
  onAddOverlap,
  onJoin,
  onDelete,
  onRegisterPrepare,
  onPendingBoundsChange,
}: {
  segment: TranscriptUtterance;
  /** Zero-based place in the visible list, for the "3 of 12" readout. */
  position: number;
  total: number;
  previousEntry: TranscriptUtterance | null;
  nextEntry: TranscriptUtterance | null;
  onStep: (entry: TranscriptUtterance) => void;
  annotation: Annotation;
  durationSamples: number;
  playheadSample: number;
  readOnly: boolean;
  onPlay: (segment: TranscriptUtterance, loop: boolean) => void;
  onChange: (next: Annotation) => void;
  onChangeText: (next: Annotation) => void;
  onCommitText: () => void;
  onSplit: (id: string, atSample: number, textOffset: number) => void;
  onAddOverlap: (id: string) => void;
  onJoin: (firstId: string, secondId: string) => void;
  onDelete: (id: string) => void;
  onRegisterPrepare: (prepare: null | (() => Annotation | null)) => void;
  onPendingBoundsChange: (pending: boolean) => void;
}) {
  const [start, setStart] = useState(editableSeconds(segment.start_sample));
  const [end, setEnd] = useState(editableSeconds(segment.end_sample));
  const [boundsProblem, setBoundsProblem] = useState<string | null>(null);
  const prepareBoundsRef = useRef<() => Annotation | null>(() => annotation);
  const [caret, setCaret] = useState(segment.text.length);
  const next = neighbourAfter(annotation.transcript, segment.id);
  const canJoin = !!next;

  // The split point is its own value rather than the live playhead. Selecting a
  // segment parks the playhead exactly on its start, and clicking the waveform
  // lands on a speaker region, so on real material the playhead is almost never
  // strictly inside the segment being edited and Split stayed greyed out.
  // It still follows the playhead while that is inside the segment, so playing
  // to a point and splitting there works as before.
  const midpoint = Math.round((segment.start_sample + segment.end_sample) / 2);
  const [splitPoint, setSplitPoint] = useState(toSeconds(midpoint));
  const [splitEdited, setSplitEdited] = useState(false);
  const editedRef = useRef(splitEdited);
  editedRef.current = splitEdited;
  const playheadInside =
    playheadSample > segment.start_sample && playheadSample < segment.end_sample;
  useEffect(() => {
    if (playheadInside && !editedRef.current) setSplitPoint(toSeconds(playheadSample));
  }, [playheadSample, playheadInside]);

  const requestedSample = fromSeconds(splitPoint);
  const splitInside =
    Number.isFinite(requestedSample)
    && requestedSample > segment.start_sample
    && requestedSample < segment.end_sample;

  // Word timings decide where a split can land, so show the user the point the
  // split will actually use before they commit to it.
  const words = wordsForSegment(
    annotation.aligned_words,
    segment.start_sample,
    segment.end_sample,
  );
  const resolved = splitInside
    ? resolveSplitSample(
        annotation.aligned_words,
        segment.start_sample,
        segment.end_sample,
        requestedSample,
      )
    : null;
  const splitWouldEmpty =
    !!resolved
    && (resolved.sample <= segment.start_sample || resolved.sample >= segment.end_sample);
  const canSplit = splitInside && !splitWouldEmpty;

  // Where the other speaker talks over this segment without a segment of their own.
  const overlaps = overlapsForSegment(annotation, segment);
  const otherSpeaker = (segment.speaker || "A") === "A" ? "B" : "A";

  // Routed through updateSegment so a speaker or timing change also moves the
  // speaker lanes on the timeline.
  function patch(update: Partial<TranscriptUtterance>) {
    onChange(updateSegment(annotation, segment.id, update));
  }

  function patchText(text: string) {
    onChangeText(updateSegment(annotation, segment.id, { text }));
  }

  const startSaved = editableSeconds(segment.start_sample);
  const endSaved = editableSeconds(segment.end_sample);
  const pendingBounds = start !== startSaved || end !== endSaved;

  function prepareBounds(): Annotation | null {
    // Preserve the exact stored sample for either field the reviewer did not
    // touch; its decimal display may not round-trip cleanly at 24 kHz.
    const nextStart = start === startSaved ? segment.start_sample : fromSeconds(start);
    const nextEnd = end === endSaved ? segment.end_sample : fromSeconds(end);
    const problem = boundsError(nextStart, nextEnd, durationSamples);
    if (problem) {
      setBoundsProblem(problem);
      return null;
    }
    setBoundsProblem(null);
    if (nextStart === segment.start_sample && nextEnd === segment.end_sample) {
      setStart(startSaved);
      setEnd(endSaved);
      return annotation;
    }
    return updateSegment(annotation, segment.id, {
      start_sample: nextStart,
      end_sample: nextEnd,
    });
  }

  function commitBounds() {
    const prepared = prepareBounds();
    if (prepared && prepared !== annotation) onChange(prepared);
  }
  prepareBoundsRef.current = prepareBounds;

  useEffect(() => {
    setStart(editableSeconds(segment.start_sample));
    setEnd(editableSeconds(segment.end_sample));
    setBoundsProblem(null);
  }, [segment.id, segment.start_sample, segment.end_sample]);

  useEffect(() => {
    onPendingBoundsChange(pendingBounds);
  }, [onPendingBoundsChange, pendingBounds]);

  useEffect(() => {
    onRegisterPrepare(() => prepareBoundsRef.current());
    return () => {
      onRegisterPrepare(null);
      onPendingBoundsChange(false);
    };
  }, []);

  return (
    <div className="segment-inspector card">
      <header>
        <span className="eyebrow">
          Selected segment{position >= 0 ? ` · ${position + 1} of ${total}` : ""}
        </span>
        <div className="inspector-play">
          <button
            type="button"
            className="step"
            disabled={!previousEntry}
            title={
              previousEntry
                ? `Go to segment ${position} · ${toSeconds(previousEntry.start_sample)}s`
                : "This is the first segment"
            }
            onClick={() => previousEntry && onStep(previousEntry)}
          >
            ‹ Previous
          </button>
          <button
            type="button"
            className="step"
            disabled={!nextEntry}
            title={
              nextEntry
                ? `Go to segment ${position + 2} · ${toSeconds(nextEntry.start_sample)}s`
                : "This is the last segment"
            }
            onClick={() => nextEntry && onStep(nextEntry)}
          >
            Next ›
          </button>
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
            patchText(event.target.value);
          }}
          onBlur={onCommitText}
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
        <details className="model-text">
          <summary>Original model suggestion</summary>
          <p dir="auto">{segment.model_text}</p>
        </details>
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
            aria-invalid={boundsProblem ? "true" : undefined}
            onChange={(event) => {
              setStart(event.target.value);
              setBoundsProblem(null);
            }}
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
            aria-invalid={boundsProblem ? "true" : undefined}
            onChange={(event) => {
              setEnd(event.target.value);
              setBoundsProblem(null);
            }}
            onBlur={commitBounds}
          />
        </label>
        {!readOnly && (
          <label className="split-point">
            Split at (s)
            <input
              type="number"
              step="0.01"
              min={toSeconds(segment.start_sample)}
              max={toSeconds(segment.end_sample)}
              value={splitPoint}
              onChange={(event) => {
                setSplitEdited(true);
                setSplitPoint(event.target.value);
              }}
            />
          </label>
        )}
      </div>

      {boundsProblem && <p className="inspector-warning" role="alert">{boundsProblem}</p>}

      {!readOnly && (
        <div className="inspector-actions">
          <button
            type="button"
            disabled={!canSplit}
            title={
              !splitInside
                ? `The split point must fall between ${toSeconds(segment.start_sample)}s`
                  + ` and ${toSeconds(segment.end_sample)}s`
                : splitWouldEmpty
                  ? "The word here runs to the end of the segment, so nothing would follow it"
                  : resolved?.snappedFrom !== null && resolved
                    ? `Splits at ${toSeconds(resolved.sample)}s, after the word being spoken`
                    : `Splits at ${toSeconds(resolved!.sample)}s`
            }
            onClick={() => onSplit(segment.id, requestedSample, caret)}
          >
            Split here
          </button>
          {overlaps.length > 0 && (
            <button
              type="button"
              title={
                `Speaker ${otherSpeaker} also talks over this segment. This adds a`
                + ` segment for them across each overlapped stretch, and leaves this`
                + ` one exactly as it is.`
              }
              onClick={() => onAddOverlap(segment.id)}
            >
              Add speaker {otherSpeaker} overlap ({overlaps.length})
            </button>
          )}
          <button
            type="button"
            disabled={!canJoin}
            title={
              !next
                ? "There is no following segment"
                : next.speaker === segment.speaker
                  ? "Join with the next segment and run the two texts together"
                  : `Join with the next segment. The merged segment keeps speaker`
                    + ` ${segment.speaker || "A"} and takes speaker ${next.speaker}'s text`
                    + " as well."
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
      {!readOnly && resolved && resolved.snappedFrom !== null && (
        <p className="inspector-note split-hint">
          A word is still being spoken at {toSeconds(resolved.snappedFrom)}s, so the split
          moves to {toSeconds(resolved.sample)}s and keeps that word whole.
        </p>
      )}
      {!readOnly && overlaps.length > 0 && (
        <p className="inspector-note split-hint">
          The timeline shows speaker {otherSpeaker} talking over this segment at{" "}
          {overlaps
            .map((window) => `${toSeconds(window.start_sample)}–${toSeconds(window.end_sample)}s`)
            .join(", ")}
          . Adding their overlap copies the words spoken there into a segment of their
          own; this segment keeps all of its own text.
        </p>
      )}
      {!readOnly && next && next.speaker !== segment.speaker && (
        <p className="inspector-note">
          The next segment is speaker {next.speaker}. Joining keeps speaker{" "}
          {segment.speaker || "A"} for the merged segment and runs both texts together;
          undo puts them back.
        </p>
      )}
      <p className="inspector-note">
        {words.length
          ? `${words.length} aligned words decide how the text divides when you split.`
          : "This segment has no word timings, so a split divides the text at your cursor."}
        {" "}Splitting and joining change timestamps and text only. The original media is
        never cut or re-encoded. Duration {seconds(segment.end_sample - segment.start_sample)}s.
      </p>
    </div>
  );
}
