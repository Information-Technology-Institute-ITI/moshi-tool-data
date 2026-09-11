import { useRef, type ReactNode, type RefCallback } from "react";
import { seconds } from "../api";
import type {
  ApprovalEligibility,
  CorrectedVersion,
  MachineTranscriptVersion,
  RecoverableGeneration,
} from "../types";

export default function ReviewToolRail({
  open,
  width,
  projectName,
  sourceName,
  durationSamples,
  currentVersion,
  segmentCount,
  saveLabel,
  saveTone,
  locked,
  dirty,
  saving,
  canUndo,
  canRedo,
  versions,
  machineTranscripts,
  approvalEligibility,
  submittingApproval,
  recoverableGenerations,
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
  onSubmitApproval,
  onRecoverGeneration,
  onWidthChange,
  inspectorRef,
  timelineToolsRef,
  workflowTools,
}: {
  open: boolean;
  width: number;
  projectName: string;
  sourceName: string;
  durationSamples: number;
  currentVersion: CorrectedVersion | null;
  segmentCount: number;
  saveLabel: string;
  saveTone: string;
  locked: boolean;
  dirty: boolean;
  saving: boolean;
  canUndo: boolean;
  canRedo: boolean;
  versions: CorrectedVersion[];
  machineTranscripts: MachineTranscriptVersion[];
  approvalEligibility: ApprovalEligibility | null;
  submittingApproval: boolean;
  recoverableGenerations: RecoverableGeneration[];
  onClose: () => void;
  onBack: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onSave: (mode: "update" | "new_version") => void;
  onRestore: (ordinal: number) => void;
  onDelete: () => void;
  onOpenPalette: () => void;
  onOpenHelp: () => void;
  onPause: () => void;
  onSubmitApproval: () => void;
  onRecoverGeneration: (annotationVersion: number) => void;
  onWidthChange: (width: number) => void;
  inspectorRef: RefCallback<HTMLDivElement>;
  timelineToolsRef: RefCallback<HTMLDivElement>;
  workflowTools?: ReactNode;
}) {
  const resize = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

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
        <div
          className="rail-resize-handle"
          role="separator"
          aria-label="Resize review tools panel"
          aria-orientation="vertical"
          aria-valuemin={280}
          aria-valuemax={560}
          aria-valuenow={width}
          tabIndex={0}
          title="Drag to resize the tools panel"
          onPointerDown={(event) => {
            resize.current = {
              pointerId: event.pointerId,
              startX: event.clientX,
              startWidth: width,
            };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (resize.current?.pointerId !== event.pointerId) return;
            onWidthChange(resize.current.startWidth + event.clientX - resize.current.startX);
          }}
          onPointerUp={(event) => {
            if (resize.current?.pointerId !== event.pointerId) return;
            resize.current = null;
            event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onLostPointerCapture={() => { resize.current = null; }}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              onWidthChange(width - 16);
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              onWidthChange(width + 16);
            } else if (event.key === "Home") {
              event.preventDefault();
              onWidthChange(280);
            } else if (event.key === "End") {
              event.preventDefault();
              onWidthChange(560);
            }
          }}
        />
        <div className="studio-rail-scroll">
        <header className="rail-heading">
          <button type="button" className="back" onClick={onBack}>← {projectName}</button>
          <button type="button" className="rail-close" aria-label="Close review tools" onClick={onClose}>×</button>
        </header>
        <span className="eyebrow">Source review</span>
        <h2>{sourceName}</h2>
        <div className="source-facts">
          <span><strong>{seconds(durationSamples)}s</strong> duration</span>
          <span>
            <strong>{currentVersion ? `V${currentVersion.ordinal}` : "No version"}</strong>
            {currentVersion ? ` · generation ${currentVersion.generation}` : ""}
          </span>
          <span><strong>{segmentCount}</strong> total segments</span>
        </div>

        <section className="rail-section" aria-labelledby="review-state-heading">
          <h3 id="review-state-heading">Review state</h3>
          <div className={`save-state ${saveTone}`} role="status" aria-live="polite">{saveLabel}</div>
          <div className="rail-actions">
            <button type="button" onClick={onUndo} disabled={locked || !canUndo}>Undo</button>
            <button type="button" onClick={onRedo} disabled={locked || !canRedo}>Redo</button>
            <div className="split-save">
              <button type="button" className="primary" onClick={() => onSave("update")} disabled={locked || !dirty}>
                {saving ? "Saving…" : "Save"}
              </button>
              <details>
                <summary aria-label="More save choices">▾</summary>
                <button type="button" disabled={locked || !dirty} onClick={() => onSave("new_version")}>
                  Save as new version
                </button>
              </details>
            </div>
          </div>
          <details className="revision-history">
            <summary>Version history · {versions.length}</summary>
            {versions.map((version) => (
              <button type="button" key={version.id} disabled={locked} onClick={() => onRestore(version.ordinal)}>
                V{version.ordinal} · {version.status} · {new Date(version.updated_at).toLocaleString()}
              </button>
            ))}
          </details>
          <details className="revision-history machine-history">
            <summary>Machine transcripts · {machineTranscripts.length}</summary>
            {machineTranscripts.map((version) => (
              <span key={version.id}>
                <strong>M{version.ordinal}</strong> · {version.model_name} · {version.producer}
                {version.provenance_status === "historical_unknown" ? " · historical provenance" : ""}
              </span>
            ))}
          </details>
          {!!recoverableGenerations.length && (
            <details className="revision-history">
              <summary>Recover earlier saves · {recoverableGenerations.length}</summary>
              {recoverableGenerations.map((generation) => (
                <button
                  type="button"
                  key={generation.annotation_version}
                  disabled={locked}
                  onClick={() => onRecoverGeneration(generation.annotation_version)}
                >
                  Generation {generation.generation} · saved {new Date(generation.created_at).toLocaleString()}
                </button>
              ))}
            </details>
          )}
          <div className="approval-state">
            <strong>
              {currentVersion?.status === "approved"
                ? "Approved final version"
                : currentVersion?.status === "pending"
                  ? "Pending admin approval"
                  : "Whole-source approval"}
            </strong>
            {approvalEligibility && !approvalEligibility.eligible && currentVersion?.status !== "pending" && currentVersion?.status !== "approved" && (
              <small>{approvalEligibility.blockers[0]}</small>
            )}
            <button
              type="button"
              disabled={locked || dirty || submittingApproval || !approvalEligibility?.eligible}
              onClick={onSubmitApproval}
            >
              {submittingApproval ? "Submitting…" : "Submit latest version for approval"}
            </button>
          </div>
        </section>

        {workflowTools}

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
        </div>
      </aside>
    </>
  );
}
