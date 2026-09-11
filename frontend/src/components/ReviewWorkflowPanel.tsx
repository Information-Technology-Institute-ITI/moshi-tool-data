import type { Annotation, TranscriptUtterance } from "../types";
import {
  DEFAULT_REVIEW_FILTERS,
  qualityQueue,
  reviewStats,
  type ReviewFilters,
} from "../reviewWorkflow";

export default function ReviewWorkflowPanel({
  annotation,
  chapterSegments,
  filters,
  savedQueueCount,
  reviewStatus,
  reviewStale,
  disabled,
  onFilters,
  onOpenSegment,
  onCompleteChapter,
}: {
  annotation: Annotation;
  chapterSegments: TranscriptUtterance[];
  filters: ReviewFilters;
  savedQueueCount: number;
  reviewStatus: "not_started" | "in_progress" | "complete";
  reviewStale: boolean;
  disabled: boolean;
  onFilters: (filters: ReviewFilters) => void;
  onOpenSegment: (id: string) => void;
  onCompleteChapter: () => void;
}) {
  const source = reviewStats(annotation.transcript);
  const chapter = reviewStats(chapterSegments);
  const queue = qualityQueue(annotation);
  const flags = [...new Set(annotation.transcript.flatMap((item) => item.quality_flags))].sort();
  const set = <K extends keyof ReviewFilters>(key: K, value: ReviewFilters[K]) => (
    onFilters({ ...filters, [key]: value })
  );

  return (
    <section className="rail-section review-workflow" aria-labelledby="workflow-heading">
      <h3 id="workflow-heading">Progress &amp; quality</h3>
      <div className="review-progress-grid">
        <span><strong>{chapter.percentage}%</strong> current chapter</span>
        <span><strong>{source.percentage}%</strong> entire recording</span>
        <span><strong>{chapter.verified}/{chapter.eligible}</strong> verified in chapter</span>
        <span><strong>{chapter.blocked}</strong> need correction</span>
      </div>
      <div className={`chapter-review-state ${reviewStale ? "stale" : reviewStatus}`}>
        {reviewStale
          ? "Previous completion needs review after newer edits"
          : reviewStatus === "complete"
            ? "Completion recorded for this chapter"
            : reviewStatus === "in_progress"
              ? "Review in progress"
              : "Completion not recorded"}
      </div>
      <button
        type="button"
        className="primary"
        disabled={disabled || !chapter.completed}
        onClick={onCompleteChapter}
        title={chapter.completed ? "Record completion for this saved revision" : "Verify every valid segment first"}
      >
        Mark chapter complete
      </button>

      <details className="review-filters" open>
        <summary>Segment filters</summary>
        <input
          aria-label="Search transcript text"
          placeholder="Transcript text"
          value={filters.text}
          onChange={(event) => set("text", event.target.value)}
        />
        <div className="review-filter-grid">
          <select aria-label="Filter speaker" value={filters.speaker} onChange={(event) => set("speaker", event.target.value as ReviewFilters["speaker"])}>
            <option value="all">All speakers</option><option value="A">Speaker A</option><option value="B">Speaker B</option>
          </select>
          <select aria-label="Filter verification" value={filters.verification} onChange={(event) => set("verification", event.target.value as ReviewFilters["verification"])}>
            <option value="all">Any verification</option><option value="verified">Verified</option><option value="unverified">Unverified</option>
          </select>
          <select aria-label="Filter flag" value={filters.flag} onChange={(event) => set("flag", event.target.value)}>
            <option value="">Any flag</option>{flags.map((flag) => <option key={flag} value={flag}>{flag}</option>)}
          </select>
          <select aria-label="Filter alignment" value={filters.alignment} onChange={(event) => set("alignment", event.target.value as ReviewFilters["alignment"])}>
            <option value="all">Any alignment</option><option value="aligned">Aligned</option><option value="low_confidence">Low confidence</option><option value="unaligned">Unaligned</option><option value="not_run">Not run</option>
          </select>
          <select aria-label="Filter overlap" value={filters.overlap} onChange={(event) => set("overlap", event.target.value as ReviewFilters["overlap"])}>
            <option value="all">Any overlap</option><option value="yes">Has overlap</option><option value="no">No overlap</option>
          </select>
          <select aria-label="Filter empty text" value={filters.empty} onChange={(event) => set("empty", event.target.value as ReviewFilters["empty"])}>
            <option value="all">Any text</option><option value="yes">Empty only</option><option value="no">Non-empty</option>
          </select>
          <select aria-label="Filter edited state" value={filters.edited} onChange={(event) => set("edited", event.target.value as ReviewFilters["edited"])}>
            <option value="all">Any edit state</option><option value="edited">Edited</option><option value="unchanged">Original</option>
          </select>
        </div>
        <button type="button" onClick={() => onFilters(DEFAULT_REVIEW_FILTERS)}>Reset filters</button>
      </details>

      <details className="quality-queue">
        <summary>Quality queue: {queue.length} working / {savedQueueCount} saved</summary>
        <div className="quality-queue-list">
          {queue.slice(0, 40).map(({ segment, priority }) => (
            <button type="button" key={segment.id} onClick={() => onOpenSegment(segment.id)}>
              <strong>P{priority}</strong>
              <span>{segment.text.trim() || "Empty transcript"}</span>
            </button>
          ))}
          {!queue.length && <small>No working-copy quality items.</small>}
        </div>
      </details>
    </section>
  );
}
