import { useEffect, useMemo, useState } from "react";
import type {
  ReviewChapterConfig,
  ReviewChapterSet,
  WaveformPeakWindow,
} from "../productContracts";
import type { TranscriptUtterance } from "../types";
import { api } from "../api";
import { segmentsInChapter } from "../reviewWorkflow";
import { chronological } from "../transcript";

const SAMPLE_RATE = 24_000;

function clock(sample: number): string {
  const total = Math.floor(sample / SAMPLE_RATE);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function segmentLabel(count: number): string {
  return `${count} segment${count === 1 ? "" : "s"}`;
}

export default function ReviewChapterNav({
  chapterSet,
  currentId,
  segments,
  disabled,
  configuring,
  showPeakWindow = false,
  onSelect,
  onSeek,
  onConfigure,
}: {
  chapterSet: ReviewChapterSet;
  currentId: string;
  segments: TranscriptUtterance[];
  disabled: boolean;
  configuring: boolean;
  showPeakWindow?: boolean;
  onSelect: (id: string) => void;
  onSeek?: (sample: number) => void;
  onConfigure: (config: ReviewChapterConfig) => void;
}) {
  const currentIndex = Math.max(
    0,
    chapterSet.chapters.findIndex((chapter) => chapter.id === currentId),
  );
  const current = chapterSet.chapters[currentIndex];
  const [mode, setMode] = useState<"max_duration" | "count">(
    chapterSet.config.mode === "count" ? "count" : "max_duration",
  );
  const [amount, setAmount] = useState(
    chapterSet.config.mode === "count"
      ? chapterSet.config.count || chapterSet.chapters.length
      : Math.round(chapterSet.config.max_duration_seconds / 60),
  );
  useEffect(() => {
    setMode(chapterSet.config.mode === "count" ? "count" : "max_duration");
    setAmount(
      chapterSet.config.mode === "count"
        ? chapterSet.config.count || chapterSet.chapters.length
        : Math.round(chapterSet.config.max_duration_seconds / 60),
    );
  }, [chapterSet]);
  const orderedSegments = useMemo(() => chronological(segments), [segments]);
  const chapterSegments = useMemo(
    () => segmentsInChapter(orderedSegments, current),
    [orderedSegments, current],
  );
  const chapterCounts = useMemo(() => new Map(
    chapterSet.chapters.map((chapter) => [
      chapter.id,
      segmentsInChapter(orderedSegments, chapter).length,
    ]),
  ), [chapterSet.chapters, orderedSegments]);
  const firstGlobalPosition = chapterSegments.length
    ? orderedSegments.findIndex((segment) => segment.id === chapterSegments[0].id) + 1
    : 0;
  const lastGlobalPosition = chapterSegments.length
    ? orderedSegments.findIndex((segment) => segment.id === chapterSegments.at(-1)?.id) + 1
    : 0;
  const recordingDuration = chapterSet.chapters.at(-1)?.end_sample || 0;
  const shortRecordingSplit = recordingDuration <= 30 * 60 * SAMPLE_RATE
    && chapterSet.chapters.length > 1;
  const configuredSize = chapterSet.config.mode === "count"
    ? `${chapterSet.chapters.length} requested chapters`
    : `maximum ${Math.round(chapterSet.config.max_duration_seconds / 60)} minutes`;

  function submit() {
    const value = Math.max(1, Math.floor(amount));
    onConfigure({
      contract_version: "studio.review/v1",
      mode,
      max_duration_seconds: mode === "max_duration" ? Math.max(60, value * 60) : 30 * 60,
      count: mode === "count" ? value : null,
      manual_boundaries_samples: [],
      boundary_search_seconds: chapterSet.config.boundary_search_seconds,
    });
  }

  return (
    <div className="chapter-navigator" aria-label="Review chapters">
      <div className="chapter-primary">
        <button
          type="button"
          aria-label="Previous chapter"
          disabled={disabled || currentIndex === 0}
          onClick={() => onSelect(chapterSet.chapters[currentIndex - 1].id)}
        >
          Previous
        </button>
        <label>
          <span>Review chapter</span>
          <select
            aria-label="Current review chapter"
            value={current.id}
            disabled={disabled}
            onChange={(event) => onSelect(event.target.value)}
          >
            {chapterSet.chapters.map((chapter) => (
              <option key={chapter.id} value={chapter.id}>
                Chapter {chapter.ordinal}: {clock(chapter.start_sample)} to {clock(chapter.end_sample)} ({segmentLabel(chapterCounts.get(chapter.id) || 0)})
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          aria-label="Next chapter"
          disabled={disabled || currentIndex === chapterSet.chapters.length - 1}
          onClick={() => onSelect(chapterSet.chapters[currentIndex + 1].id)}
        >
          Next
        </button>
        <div className="chapter-summary">
          <strong>{currentIndex + 1} of {chapterSet.chapters.length}</strong>
          <span>
            {segmentLabel(chapterSegments.length)}
            {chapterSegments.length > 0 && ` | global ${firstGlobalPosition}-${lastGlobalPosition} of ${orderedSegments.length}`}
            {` | ${clock(current.start_sample)} to ${clock(current.end_sample)}`}
          </span>
        </div>
      </div>
      {shortRecordingSplit && (
        <div className="short-chapter-notice" role="note">
          <span>This recording is under 30 minutes; one chapter is usually clearer.</span>
          <button type="button" disabled={configuring} onClick={() => onConfigure({
            contract_version: "studio.review/v1",
            mode: "max_duration",
            max_duration_seconds: 30 * 60,
            count: null,
            manual_boundaries_samples: [],
            boundary_search_seconds: chapterSet.config.boundary_search_seconds,
          })}>
            Use one chapter
          </button>
        </div>
      )}
      <details className="chapter-settings">
        <summary>Chapter setup: {configuredSize}</summary>
        <div>
          <label>
            <span>Divide recording by</span>
            <select
              aria-label="Chapter division mode"
              value={mode}
              disabled={configuring}
              onChange={(event) => setMode(event.target.value as typeof mode)}
            >
              <option value="max_duration">Maximum chapter length</option>
              <option value="count">Exact number of chapters</option>
            </select>
          </label>
          <label>
            <span>{mode === "count" ? "Number of chapters" : "Minutes per chapter"}</span>
            <input
              aria-label={mode === "count" ? "Chapter count" : "Maximum chapter minutes"}
              type="number"
              min="1"
              max={mode === "count" ? 500 : 120}
              value={amount}
              disabled={configuring}
              onChange={(event) => setAmount(Number(event.target.value))}
            />
          </label>
          <button type="button" disabled={configuring} onClick={submit}>
            {configuring ? "Applying..." : "Apply"}
          </button>
        </div>
      </details>
      {showPeakWindow && (
        <ChapterPeakStrip
          sourceId={chapterSet.source_id}
          startSample={current.start_sample}
          endSample={current.end_sample}
          onSeek={onSeek}
        />
      )}
    </div>
  );
}

function ChapterPeakStrip({ sourceId, startSample, endSample, onSeek }: {
  sourceId: string;
  startSample: number;
  endSample: number;
  onSeek?: (sample: number) => void;
}) {
  const [points, setPoints] = useState<[number, number][]>([]);
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({
      start_sample: String(startSample),
      end_sample: String(endSample),
      max_points: "900",
    });
    void api<WaveformPeakWindow>(
      `/api/sources/${sourceId}/waveform-peaks?${query}`,
      { signal: controller.signal, credentials: "same-origin" },
    ).then((value) => setPoints(value.points)).catch((reason) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) setPoints([]);
    });
    return () => controller.abort();
  }, [sourceId, startSample, endSample]);
  const area = useMemo(() => {
    if (!points.length) return "";
    const width = 1000;
    const x = (index: number) => index * width / Math.max(1, points.length - 1);
    const y = (amplitude: number) => 15 - amplitude * 13;
    const upper = points.map((point, index) => `${x(index)},${y(point[1])}`);
    const lower = points
      .map((point, index) => `${x(index)},${y(point[0])}`)
      .reverse();
    return `M${upper.join(" L")} L${lower.join(" L")} Z`;
  }, [points]);
  return (
    <button
      type="button"
      className="chapter-peak-strip"
      aria-label="Seek within current chapter waveform"
      disabled={!points.length}
      onClick={(event) => {
        if (!onSeek) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const ratio = rect.width ? (event.clientX - rect.left) / rect.width : 0;
        onSeek(Math.round(startSample + Math.max(0, Math.min(1, ratio)) * (endSample - startSample)));
      }}
    >
      <svg viewBox="0 0 1000 30" preserveAspectRatio="none" aria-hidden="true">
        <path d={area} />
      </svg>
    </button>
  );
}
