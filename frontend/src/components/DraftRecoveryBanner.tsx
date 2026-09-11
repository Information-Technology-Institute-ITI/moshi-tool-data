import type { RecoveryDraft } from "../useDraftRecovery";

export default function DraftRecoveryBanner({
  draft,
  conflict,
  inspecting,
  onInspect,
  onRestore,
  onDiscard,
}: {
  draft: RecoveryDraft;
  conflict: boolean;
  inspecting: boolean;
  onInspect: () => void;
  onRestore: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className={`draft-recovery ${conflict ? "conflict" : ""}`} role="status">
      <div>
        <strong>{conflict ? "Older local draft found" : "Recover unsaved work"}</strong>
        <span>{conflict ? "Its base generation differs from the current server copy." : "Automatic crash protection is active on this browser. Nothing reaches the server until you press Save."}</span>
      </div>
      <div className="draft-recovery-actions">
        <button type="button" onClick={onInspect}>{inspecting ? "Hide details" : "Inspect"}</button>
        <button type="button" className="primary" disabled={conflict} onClick={onRestore}>Recover unsaved work</button>
        <button type="button" onClick={onDiscard}>Discard</button>
      </div>
      {inspecting && (
        <dl>
          <div><dt>Stored</dt><dd>{new Date(draft.savedAt).toLocaleString()}</dd></div>
          <div><dt>Base generation</dt><dd>{draft.baseRevision}</dd></div>
          <div><dt>Segments</dt><dd>{draft.annotation.transcript.length}</dd></div>
        </dl>
      )}
    </div>
  );
}
