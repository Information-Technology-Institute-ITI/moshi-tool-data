import type { RefCallback } from "react";
import { seconds } from "../api";

type Revision = { version: number; created_at: string };

export default function ReviewToolRail({
  open,
  projectName,
  sourceName,
  durationSamples,
  savedVersion,
  segmentCount,
  saveLabel,
  saveTone,
  locked,
  dirty,
  saving,
  canUndo,
  canRedo,
  revisions,
  onClose,
  onBack,
  onUndo,
  onRedo,
  onSave,
  onRestore,
  onDelete,
  onOpenPalette,
  onOpenHelp,
  onPause,
  inspectorRef,
  timelineToolsRef,
}: {
  open: boolean;
  projectName: string;
  sourceName: string;
  durationSamples: number;
  savedVersion: number;
  segmentCount: number;
  saveLabel: string;
  saveTone: string;
  locked: boolean;
  dirty: boolean;
  saving: boolean;
  canUndo: boolean;
  canRedo: boolean;
  revisions: Revision[];
  onClose: () => void;
  onBack: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onSave: () => void;
  onRestore: (version: number) => void;
  onDelete: () => void;
  onOpenPalette: () => void;
  onOpenHelp: () => void;
  onPause: () => void;
  inspectorRef: RefCallback<HTMLDivElement>;
  timelineToolsRef: RefCallback<HTMLDivElement>;
}) {
  return (
    <>
      <button
        type="button"
        className={`review-drawer-backdrop ${open ? "open" : ""}`}
        aria-label="Close review tools"
        tabIndex={open ? 0 : -1}
        onClick={onClose}
      />
      <aside className={`studio-rail ${open ? "open" : ""}`} aria-label="Review tools">
        <header className="rail-heading">
          <button type="button" className="back" onClick={onBack}>← {projectName}</button>
          <button type="button" className="rail-close" aria-label="Close review tools" onClick={onClose}>×</button>
        </header>
        <span className="eyebrow">Source review</span>
        <h2>{sourceName}</h2>
        <div className="source-facts">
          <span><strong>{seconds(durationSamples)}s</strong> duration</span>
          <span><strong>v{savedVersion}</strong> saved revision</span>
          <span><strong>{segmentCount}</strong> segments</span>
        </div>

        <section className="rail-section" aria-labelledby="review-state-heading">
          <h3 id="review-state-heading">Review state</h3>
          <div className={`save-state ${saveTone}`} role="status" aria-live="polite">{saveLabel}</div>
          <div className="rail-actions">
            <button type="button" onClick={onUndo} disabled={locked || !canUndo}>Undo</button>
            <button type="button" onClick={onRedo} disabled={locked || !canRedo}>Redo</button>
            <button type="button" className="primary" onClick={onSave} disabled={locked || !dirty}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
          <details className="revision-history">
            <summary>{revisions.length} saved revisions</summary>
            {revisions.map((revision) => (
              <button type="button" key={revision.version} disabled={locked} onClick={() => onRestore(revision.version)}>
                v{revision.version} · {new Date(revision.created_at).toLocaleString()}
              </button>
            ))}
          </details>
        </section>

        <section className="rail-section" aria-labelledby="selected-tools-heading">
          <h3 id="selected-tools-heading">Selected segment</h3>
          <button type="button" className="rail-pause" onClick={onPause}>Pause audio</button>
          <div ref={inspectorRef} className="rail-portal" />
        </section>

        <section className="rail-section" aria-labelledby="timeline-tools-heading">
          <h3 id="timeline-tools-heading">Speaker &amp; exclusion tools</h3>
          <div ref={timelineToolsRef} className="rail-portal" />
        </section>

        <section className="rail-section rail-command-buttons" aria-labelledby="command-tools-heading">
          <h3 id="command-tools-heading">Commands</h3>
          <button type="button" onClick={onOpenPalette}>Command palette <kbd>Ctrl + K</kbd></button>
          <button type="button" onClick={onOpenHelp}>Shortcut help <kbd>?</kbd></button>
        </section>

        <section className="rail-section rail-danger">
          <button type="button" className="danger-soft" disabled={locked} onClick={onDelete}>Delete source</button>
        </section>
      </aside>
    </>
  );
}
