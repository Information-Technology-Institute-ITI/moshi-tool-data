import { useEffect, useState } from "react";
import { api, jsonRequest, seconds } from "../api";
import type { Annotation, AnnotationApprovalTask } from "../types";

export default function ApprovalQueuePage({
  setError,
}: {
  setError: (message: string) => void;
}) {
  const [tasks, setTasks] = useState<AnnotationApprovalTask[]>([]);
  const [selected, setSelected] = useState<AnnotationApprovalTask | null>(null);
  const [annotation, setAnnotation] = useState<Annotation | null>(null);
  const [note, setNote] = useState("");
  const [working, setWorking] = useState(false);
  const [retention, setRetention] = useState<{
    affected_versions: number[];
    estimated_bytes: number;
  } | null>(null);

  async function load() {
    const value = await api<{ tasks: AnnotationApprovalTask[] }>(
      "/api/admin/annotation-approvals",
    );
    setTasks(value.tasks);
    if (selected) {
      setSelected(value.tasks.find((item) => item.id === selected.id) || null);
    }
  }

  useEffect(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, []);

  async function inspect(task: AnnotationApprovalTask) {
    setSelected(task);
    setNote(task.review_note || "");
    try {
      setAnnotation(await api<Annotation>(
        `/api/sources/${task.source_id}/annotations/${task.annotation_version}`,
      ));
      if (task.status === "approved") {
        setRetention(await api<{
          affected_versions: number[];
          estimated_bytes: number;
        }>(`/api/admin/sources/${task.source_id}/retention`));
      } else {
        setRetention(null);
      }
    } catch (reason) {
      setAnnotation(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function chooseRetention(mode: "keep_all" | "archive_older") {
    if (!selected || working) return;
    if (
      mode === "archive_older"
      && !window.confirm(
        "Create a verified database backup, then start the 10-day recovery window for older versions?",
      )
    ) return;
    setWorking(true);
    try {
      await api(
        `/api/admin/sources/${selected.source_id}/retention`,
        jsonRequest("POST", { mode }),
      );
      await inspect(selected);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
    }
  }

  async function decide(decision: "approved" | "rejected" | "returned") {
    if (!selected || working) return;
    setWorking(true);
    try {
      await api(
        `/api/admin/annotation-approvals/${selected.id}/decision`,
        jsonRequest("POST", { decision, note }),
      );
      setSelected(null);
      setAnnotation(null);
      setNote("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="page approval-page">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Administrator only</span>
          <h1>Transcript approval queue</h1>
          <p>Each task is frozen to one corrected version, internal generation, and fingerprint.</p>
        </div>
        <span>{tasks.filter((item) => item.status === "pending").length} pending</span>
      </div>
      <div className="approval-layout">
        <div className="approval-task-list card">
          {tasks.map((task) => (
            <button
              type="button"
              key={task.id}
              className={selected?.id === task.id ? "active" : ""}
              onClick={() => void inspect(task)}
            >
              <strong>{task.original_name}</strong>
              <span>{task.project_name} · V{task.corrected_version_ordinal} · internal save {task.annotation_version}</span>
              <small>{task.status} · {new Date(task.created_at).toLocaleString()}</small>
            </button>
          ))}
          {!tasks.length && <p>No approval tasks yet.</p>}
        </div>
        <div className="approval-inspector card">
          {!selected && <p>Select a submission to review its fixed transcript.</p>}
          {selected && (
            <>
              <header>
                <div>
                  <span className="eyebrow">Submitted V{selected.corrected_version_ordinal}</span>
                  <h2>{selected.original_name}</h2>
                </div>
                <span className={`pill ${selected.status === "approved" ? "good" : selected.status === "pending" ? "warn" : "bad"}`}>
                  {selected.status}
                </span>
              </header>
              <code title={selected.content_fingerprint}>{selected.content_fingerprint}</code>
              <div className="approval-transcript">
                {annotation?.transcript.map((segment) => (
                  <div key={segment.id}>
                    <strong>{segment.speaker || "?"}</strong>
                    <span dir="auto">{segment.text || "(empty)"}</span>
                    <small>{seconds(segment.start_sample)}–{seconds(segment.end_sample)}s</small>
                  </div>
                ))}
              </div>
              {selected.status === "pending" && (
                <>
                  <label>
                    Admin review note
                    <textarea value={note} onChange={(event) => setNote(event.target.value)} />
                  </label>
                  <div className="modal-actions">
                    <button type="button" disabled={working || !note.trim()} onClick={() => void decide("rejected")}>Reject</button>
                    <button type="button" disabled={working || !note.trim()} onClick={() => void decide("returned")}>Return for correction</button>
                    <button type="button" className="primary" disabled={working} onClick={() => void decide("approved")}>Approve final version</button>
                  </div>
                </>
              )}
              {selected.status === "approved" && retention && (
                <div className="retention-choice">
                  <strong>Approved-history retention</strong>
                  <p>
                    {retention.affected_versions.length} older version(s), approximately
                    {` ${(retention.estimated_bytes / 1024 / 1024).toFixed(1)} MB`}.
                  </p>
                  <button type="button" disabled={working} onClick={() => void chooseRetention("keep_all")}>
                    Keep all versions
                  </button>
                  <button type="button" className="primary" disabled={working} onClick={() => void chooseRetention("archive_older")}>
                    Back up, then archive older versions for 10 days
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
