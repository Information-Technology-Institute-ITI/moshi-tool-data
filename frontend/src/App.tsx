import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  deleteProject,
  getCurrentUser,
  jsonRequest,
  listAdminUsers,
  listProjects,
  seconds,
  signout,
  transferProjectOwner,
  watchJob,
} from "./api";
import AuthScreen from "./components/AuthScreen";
import GpuStatusPage from "./components/GpuStatusPage";
import IntroPage from "./components/IntroPage";
import JobProgress from "./components/JobProgress";
import StereoPlayer from "./components/StereoPlayer";
import WaveformEditor from "./components/WaveformEditor";
import type {
  AdminUser,
  Annotation,
  AuthUser,
  ClipArtifact,
  Job,
  Project,
  ProjectValidation,
  Source,
  SourceDetail,
  Speaker,
} from "./types";

type ProjectDetail = {
  project: Project;
  sources: Source[];
  jobs: Job[];
  exports: {
    id: string;
    version: number;
    name: string;
    status: string;
    path?: string;
  }[];
};

const highRiskTranscriptFlags = new Set([
  "abnormally_high_word_rate",
  "decode_disagreement",
  "low_average_log_probability",
  "overlapping_speech",
  "repeated_ngram",
  "suspicious_character_sequence",
]);

function alignmentInputSignature(annotation: Annotation): string {
  return JSON.stringify(
    annotation.transcript.map((value) => ({
      id: value.id,
      speaker: value.speaker,
      start_sample: value.start_sample,
      end_sample: value.end_sample,
      text: value.text,
    })),
  );
}

function cleanSpeakerTurns(annotation: Annotation, speaker: Speaker) {
  const other = annotation.activities.filter((value) => value.speaker !== speaker);
  return annotation.activities
    .filter((value) => value.speaker === speaker)
    .filter((value) => value.end_sample - value.start_sample >= 36_000)
    .filter((value) =>
      !other.some((second) =>
        second.end_sample > value.start_sample
        && second.start_sample < value.end_sample
      )
    )
    .sort(
      (first, second) =>
        (second.end_sample - second.start_sample)
        - (first.end_sample - first.start_sample),
    )
    .slice(0, 25);
}

function transcriptPriority(
  item: Annotation["transcript"][number],
  assistantSpeaker?: Speaker | null,
): number {
  const weights: Record<string, number> = {
    suspicious_character_sequence: 100,
    repeated_ngram: 90,
    overlapping_speech: 70,
    decode_disagreement: 55,
    low_average_log_probability: 45,
    abnormally_high_word_rate: 40,
    unaligned_words: 35,
    low_confidence_alignment: 25,
  };
  if (item.human_verified) return 0;
  return item.quality_flags.reduce(
    (total, flag) => total + (weights[flag] || 10),
    item.speaker === assistantSpeaker ? 20 : 0,
  ) + (item.alignment_status === "aligned" ? 0 : 30);
}

function App() {
  const activationLink = window.location.hash.startsWith("#/activate?");
  const [entryView, setEntryView] = useState<"intro" | "auth" | "workspace">(
    activationLink ? "auth" : "intro",
  );
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [authLoading, setAuthLoading] = useState(true);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [source, setSource] = useState<SourceDetail | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [page, setPage] = useState<"workspace" | "gpu">("workspace");
  const stopWatching = useRef<null | (() => void)>(null);
  const isAdmin = authUser?.role === "admin";

  useEffect(() => {
    let active = true;
    getCurrentUser()
      .then(({ user, required }) => {
        if (!active) return;
        setAuthRequired(required);
        setAuthUser(user);
        if (user || !required) void loadProjects();
      })
      .catch((value) => {
        if (active) {
          setError(value instanceof Error ? value.message : String(value));
        }
      })
      .finally(() => {
        if (active) setAuthLoading(false);
      });
    return () => stopWatching.current?.();
  }, []);

  /**
   * Closes anything the signed-in user may no longer reach and returns them to
   * the authorized library. Used for 404 responses, which the server also
   * returns for datasets owned by somebody else, so the UI must never imply the
   * object exists.
   */
  function returnToLibrary() {
    stopWatching.current?.();
    stopWatching.current = null;
    setProject(null);
    setSource(null);
    setJob(null);
    setPage("workspace");
  }

  function handleSessionExpired() {
    stopWatching.current?.();
    stopWatching.current = null;
    setAuthUser(null);
    setProjects([]);
    setProject(null);
    setSource(null);
    setJob(null);
    setPage("workspace");
    setEntryView("auth");
  }

  async function run<T>(action: () => Promise<T>): Promise<T | undefined> {
    setError("");
    try {
      return await action();
    } catch (value) {
      if (value instanceof ApiError) {
        if (value.status === 401) {
          handleSessionExpired();
          setError("Your session ended. Sign in to continue.");
          return undefined;
        }
        if (value.status === 403) {
          setError("Administrator access is required for that action.");
          return undefined;
        }
        if (value.status === 404) {
          // Never disclose whether the dataset exists under another owner.
          returnToLibrary();
          setError("That dataset is no longer available in your workspace.");
          void reloadProjects();
          return undefined;
        }
      }
      setError(value instanceof Error ? value.message : String(value));
      return undefined;
    }
  }

  /** Reloads without recursing through run()'s 404 handling. */
  async function reloadProjects() {
    try {
      setProjects((await listProjects()).projects);
    } catch {
      setProjects([]);
    }
  }

  async function loadProjects() {
    const value = await run(listProjects);
    if (value) setProjects(value.projects);
  }

  async function openProject(id: string) {
    const value = await run(() => api<ProjectDetail>(`/api/projects/${id}`));
    if (value) {
      setProject(value);
      setSource(null);
    }
  }

  async function openSource(id: string) {
    const value = await run(() => api<SourceDetail>(`/api/sources/${id}`));
    if (value) setSource(value);
  }

  function monitor(next: Job) {
    setJob(next);
    stopWatching.current?.();
    stopWatching.current = watchJob(next.id, async (value) => {
      setJob(value);
      if (value.status === "complete") {
        setNotice(`${value.kind.replaceAll("_", " ")} complete`);
        if (source) {
          await openSource(source.id);
          if (project) {
            const refreshed = await run(() =>
              api<ProjectDetail>(`/api/projects/${project.project.id}`),
            );
            if (refreshed) setProject(refreshed);
          }
        } else if (project) {
          await openProject(project.project.id);
        }
      }
    });
  }

  async function retryJob(failed: Job) {
    const next = await run(() =>
      api<Job>(`/api/jobs/${failed.id}/retry`, jsonRequest("POST")),
    );
    if (next) monitor(next);
  }

  async function handleSignout() {
    await run(signout);
    stopWatching.current?.();
    setAuthUser(null);
    setProjects([]);
    setProject(null);
    setSource(null);
    setJob(null);
    setPage("workspace");
    setEntryView("intro");
  }

  if (authLoading) {
    return (
      <main className="auth-loading" aria-live="polite">
        <span className="brand-mark">M</span>
        <strong>Loading your workspace...</strong>
      </main>
    );
  }

  if (entryView === "intro") {
    return (
      <IntroPage
        user={authUser}
        onSignIn={() => setEntryView("auth")}
        onEnter={() => setEntryView("workspace")}
      />
    );
  }

  if (entryView === "auth" || (authRequired && !authUser)) {
    return (
      <AuthScreen
        onBack={() => setEntryView("intro")}
        onAuthenticated={(user) => {
          setAuthUser(user);
          setEntryView("workspace");
          void loadProjects();
        }}
      />
    );
  }

  const view = page === "gpu" && isAdmin ? (
    <GpuStatusPage />
  ) : source ? (
    <Studio
      detail={source}
      project={project!}
      onBack={() => setSource(null)}
      onDeleted={() => openProject(project!.project.id)}
      onReload={() => openSource(source.id)}
      onJob={monitor}
      setNotice={setNotice}
      setError={setError}
    />
  ) : project ? (
    <ProjectWorkspace
      detail={project}
      onBack={() => setProject(null)}
      onOpenSource={openSource}
      onReload={() => openProject(project.project.id)}
      onJob={monitor}
      setError={setError}
    />
  ) : (
    <ProjectLibrary
      projects={projects}
      user={authUser}
      onOpen={openProject}
      onReload={loadProjects}
      setNotice={setNotice}
      setError={setError}
    />
  );

  return (
    <main>
      <header className="topbar">
        <button
          className="brand"
          onClick={() => {
            setPage("workspace");
            setProject(null);
            setSource(null);
          }}
        >
          <span className="brand-mark">M</span>
          <span>
            <strong>Moshi Dataset Studio</strong>
            <small>Human-guided full-duplex audio</small>
          </span>
        </button>
        <div className="top-actions">
          {isAdmin && (
            <button
              className={`system-nav ${page === "gpu" ? "active" : ""}`}
              type="button"
              aria-current={page === "gpu" ? "page" : undefined}
              onClick={() => setPage("gpu")}
            >
              GPU status
            </button>
          )}
          <div className="top-meta">
            <span className="status-dot" />
            {authUser ? (
              <span>
                <strong>{authUser.display_name}</strong>
                <small>{authUser.email}</small>
              </span>
            ) : (
              "Local workspace"
            )}
          </div>
          {authUser && (
            <button className="system-nav" type="button" onClick={handleSignout}>
              Sign out
            </button>
          )}
        </div>
      </header>
      {error && <div className="banner error" role="alert">{error}<button onClick={() => setError("")}>×</button></div>}
      {notice && <div className="banner success">{notice}<button onClick={() => setNotice("")}>×</button></div>}
      <JobProgress job={job} onRetry={retryJob} />
      {view}
    </main>
  );
}

function ownerLabel(project: Project): string {
  if (!project.owner) return "No owner (legacy dataset)";
  return project.owner.display_name || project.owner.email || project.owner.id;
}

function updatedLabel(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return "Unknown";
  return new Date(parsed).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function ProjectLibrary({
  projects,
  user,
  onOpen,
  onReload,
  setNotice,
  setError,
}: {
  projects: Project[];
  user: AuthUser | null;
  onOpen: (id: string) => void;
  onReload: () => void | Promise<void>;
  setNotice: (message: string) => void;
  setError: (message: string) => void;
}) {
  const isAdmin = user?.role === "admin";
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [search, setSearch] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [transferFor, setTransferFor] = useState<Project | null>(null);
  const [deleteFor, setDeleteFor] = useState<Project | null>(null);

  // Only administrators may list users, so never request it as a regular user.
  useEffect(() => {
    if (!isAdmin) {
      setAdminUsers([]);
      setScope("mine");
      setSearch("");
      setOwnerFilter("");
      return;
    }
    let active = true;
    listAdminUsers()
      .then((value) => {
        if (active) setAdminUsers(value.users);
      })
      .catch(() => {
        if (active) setAdminUsers([]);
      });
    return () => {
      active = false;
    };
  }, [isAdmin, user?.id]);

  const visible = useMemo(() => {
    // The server already scopes /api/projects by role: a regular user only ever
    // receives their own datasets, so "mine" is a presentation filter for
    // administrators and never a substitute for server authorization.
    let rows = projects;
    if (isAdmin && scope === "mine") {
      rows = rows.filter((item) => item.owner_user_id === user?.id);
    }
    if (isAdmin && ownerFilter) {
      rows = rows.filter((item) => item.owner_user_id === ownerFilter);
    }
    const term = search.trim().toLowerCase();
    if (isAdmin && term) {
      rows = rows.filter((item) => {
        const owner = item.owner;
        return (
          item.name.toLowerCase().includes(term) ||
          (owner?.display_name || "").toLowerCase().includes(term) ||
          (owner?.email || "").toLowerCase().includes(term)
        );
      });
    }
    return rows;
  }, [projects, isAdmin, scope, ownerFilter, search, user?.id]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    try {
      // Ownership is derived from the session; the server rejects owner/role here.
      await api("/api/projects", jsonRequest("POST", { name, language: "ar-EG" }));
      setName("");
      onReload();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }

  async function confirmTransfer(project: Project, ownerUserId: string) {
    try {
      await transferProjectOwner(project.id, ownerUserId);
      const next = adminUsers.find((item) => item.id === ownerUserId);
      setTransferFor(null);
      setNotice(
        `"${project.name}" now belongs to ${next?.display_name || next?.email || ownerUserId}.`,
      );
      await onReload();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }

  async function confirmDelete(project: Project) {
    try {
      await deleteProject(project.id);
      setDeleteFor(null);
      setNotice(`"${project.name}" was permanently deleted.`);
      await onReload();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }

  const heading = isAdmin && scope === "all" ? "All datasets" : "My datasets";

  return (
    <section className="page library-page">
      <div className="hero">
        <div>
          <span className="eyebrow">Egyptian Arabic · two speakers · 24 kHz</span>
          <h1>Build dialogue data you have actually heard.</h1>
          <p>
            Upload a podcast, correct who spoke when, fix the draft transcript against the
            original audio, and save your reviewed annotation.
          </p>
        </div>
        <form className="create-card" onSubmit={create}>
          <label>
            New dataset
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Cairo conversations"
              required
            />
          </label>
          <button className="primary" type="submit">Create dataset</button>
        </form>
      </div>

      {isAdmin && (
        <div className="library-controls">
          <div className="scope-toggle" role="tablist" aria-label="Dataset scope">
            {(["mine", "all"] as const).map((value) => (
              <button
                key={value}
                role="tab"
                type="button"
                aria-selected={scope === value}
                className={scope === value ? "active" : ""}
                onClick={() => setScope(value)}
              >
                {value === "mine" ? "My datasets" : "All datasets"}
              </button>
            ))}
          </div>
          {scope === "all" && (
            <div className="library-filters">
              <label>
                Search
                <input
                  type="search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Dataset or owner name / email"
                />
              </label>
              <label>
                Owner
                <select
                  value={ownerFilter}
                  onChange={(event) => setOwnerFilter(event.target.value)}
                >
                  <option value="">All owners</option>
                  {adminUsers.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name} · {item.email}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </div>
      )}

      <div className="section-heading">
        <div>
          <span className="eyebrow">Your workspace</span>
          <h2>{heading}</h2>
        </div>
        <span>{visible.length} datasets</span>
      </div>

      <div className="project-grid">
        {visible.map((item, index) => (
          <div className="project-card" key={item.id}>
            <button className="project-card-open" onClick={() => onOpen(item.id)}>
              <span className="project-index">{String(index + 1).padStart(2, "0")}</span>
              <h3>{item.name}</h3>
              {isAdmin && scope === "all" && (
                <p className="project-owner">
                  {ownerLabel(item)}
                  {item.owner?.email && <small>{item.owner.email}</small>}
                </p>
              )}
              <div className="project-stats">
                <span><strong>{item.source_count || 0}</strong> sources</span>
                <span><strong>{item.ready_sources || 0}</strong> ready</span>
                <span>Updated {updatedLabel(item.updated_at)}</span>
              </div>
            </button>
            <div className="project-card-actions">
              {isAdmin && (
                <button type="button" onClick={() => setTransferFor(item)}>
                  Transfer owner
                </button>
              )}
              <button
                type="button"
                className="danger-soft"
                onClick={() => setDeleteFor(item)}
              >
                Delete
              </button>
            </div>
          </div>
        ))}
        {!visible.length && (
          <div className="empty-card">
            {projects.length
              ? "No datasets match the current filters."
              : "Create the first dataset to begin."}
          </div>
        )}
      </div>

      {transferFor && (
        <TransferOwnerDialog
          project={transferFor}
          users={adminUsers}
          onCancel={() => setTransferFor(null)}
          onConfirm={(ownerUserId) => confirmTransfer(transferFor, ownerUserId)}
        />
      )}
      {deleteFor && (
        <DeleteDatasetDialog
          project={deleteFor}
          onCancel={() => setDeleteFor(null)}
          onConfirm={() => confirmDelete(deleteFor)}
        />
      )}
    </section>
  );
}

/** Closes a modal on Escape so it is dismissible without a pointer. */
function useEscapeToClose(onCancel: () => void) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);
}

function TransferOwnerDialog({
  project,
  users,
  onCancel,
  onConfirm,
}: {
  project: Project;
  users: AdminUser[];
  onCancel: () => void;
  onConfirm: (ownerUserId: string) => void | Promise<void>;
}) {
  const [selected, setSelected] = useState("");
  const candidates = users.filter((item) => item.id !== project.owner_user_id);
  const next = candidates.find((item) => item.id === selected);
  useEscapeToClose(onCancel);

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="transfer-title"
      >
        <h2 id="transfer-title">Transfer “{project.name}”</h2>
        <p>
          The current owner immediately loses access unless they are an administrator.
        </p>
        <dl className="transfer-summary">
          <div>
            <dt>Current owner</dt>
            <dd>{ownerLabel(project)}</dd>
          </div>
          <div>
            <dt>New owner</dt>
            <dd>{next ? `${next.display_name} · ${next.email}` : "Not chosen yet"}</dd>
          </div>
        </dl>
        <label>
          Choose a new owner
          <select value={selected} onChange={(event) => setSelected(event.target.value)}>
            <option value="">Select an active user…</option>
            {candidates.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name} · {item.email}
              </option>
            ))}
          </select>
        </label>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button
            type="button"
            className="primary"
            disabled={!selected}
            onClick={() => void onConfirm(selected)}
          >
            Transfer dataset
          </button>
        </div>
      </div>
    </div>
  );
}

function DeleteDatasetDialog({
  project,
  onCancel,
  onConfirm,
}: {
  project: Project;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const [typed, setTyped] = useState("");
  const matches = typed.trim() === project.name.trim();
  useEscapeToClose(onCancel);

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-title"
      >
        <h2 id="delete-title">Delete “{project.name}”</h2>
        <p className="danger-text">
          This permanently removes the dataset, its sources, uploaded media, and every saved
          annotation revision. It cannot be undone.
        </p>
        <label>
          Type the dataset name to confirm
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder={project.name}
            autoComplete="off"
          />
        </label>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button
            type="button"
            className="danger"
            disabled={!matches}
            onClick={() => void onConfirm()}
          >
            Delete forever
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Derives the one-pass source state from existing source and job fields.
 * A source has exactly one initialization attempt: it is waiting for the
 * Manual/Assisted choice, running it, ready for editing, or retryable after a
 * failure. Nothing here can start a second pass on a successful source.
 */
function sourceState(
  source: Source,
  jobs: Job[],
): { label: string; tone: string; description: string; failedJob: Job | null } {
  const initJobs = jobs.filter(
    (job) => job.kind === "initialize" && job.source_id === source.id,
  );
  const activeInit = initJobs.find(
    (job) => job.status === "queued" || job.status === "running",
  );
  const failedInit = initJobs.find((job) => job.status === "failed");

  if (source.status === "failed" || (failedInit && source.status === "uploaded")) {
    return {
      label: "Failed",
      tone: "bad",
      description: failedInit?.error || failedInit?.message || "Preparation did not finish.",
      failedJob: failedInit || null,
    };
  }
  if (source.status === "processing" || activeInit) {
    return {
      label: "Processing",
      tone: "warn",
      description: activeInit?.message || "Preparing audio, speakers, and draft transcript…",
      failedJob: null,
    };
  }
  if (source.status === "ready" || source.status === "clips_ready") {
    return {
      label: "Ready",
      tone: "good",
      description: "Ready to review and edit.",
      failedJob: null,
    };
  }
  return {
    label: "Not started",
    tone: "warn",
    description: "Open to choose Manual or Assisted preparation.",
    failedJob: null,
  };
}

function ProjectWorkspace({
  detail,
  onBack,
  onOpenSource,
  onReload,
  onJob,
  setError,
}: {
  detail: ProjectDetail;
  onBack: () => void;
  onOpenSource: (id: string) => void;
  onReload: () => void;
  onJob: (job: Job) => void;
  setError: (message: string) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [exportName, setExportName] = useState(detail.project.name);
  const [projectName, setProjectName] = useState(detail.project.name);
  const [language, setLanguage] = useState(detail.project.language);
  const [validation, setValidation] = useState<ProjectValidation | null>(null);
  const activeJob = detail.jobs.find((item) =>
    item.status === "queued" || item.status === "running",
  );

  useEffect(() => {
    setProjectName(detail.project.name);
    setLanguage(detail.project.language);
    setExportName(detail.project.name);
    setValidation(null);
  }, [detail.project.id, detail.project.name, detail.project.language]);

  useEffect(() => {
    if (activeJob) onJob(activeJob);
  }, [activeJob?.id]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setUploading(true);
    try {
      await api(`/api/projects/${detail.project.id}/sources`, {
        method: "POST",
        headers: { "content-type": file.type || "application/octet-stream", "x-filename": file.name },
        body: file,
      });
      setFile(null);
      onReload();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setUploading(false);
    }
  }

  async function createExport() {
    try {
      const checked = await api<ProjectValidation>(
        `/api/projects/${detail.project.id}/validate`,
      );
      setValidation(checked);
      if (!checked.valid) {
        setError(checked.blockers.join(" · "));
        return;
      }
      const value = await api<{ job: Job }>(
        `/api/projects/${detail.project.id}/exports`,
        jsonRequest("POST", { name: exportName }),
      );
      onJob(value.job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function saveSettings() {
    try {
      await api(
        `/api/projects/${detail.project.id}`,
        jsonRequest("PUT", { name: projectName, language }),
      );
      onReload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function validateExport() {
    try {
      setValidation(
        await api<ProjectValidation>(`/api/projects/${detail.project.id}/validate`),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function retry(job: Job) {
    try {
      onJob(
        await api<Job>(`/api/jobs/${job.id}/retry`, jsonRequest("POST")),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <section className="page">
      <button className="back" onClick={onBack}>← Datasets</button>
      <div className="workspace-heading">
        <div>
          <span className="eyebrow">Dataset project</span>
          <h1>{detail.project.name}</h1>
          <p>Each source stays together when the deterministic 90/10 train/eval split is created.</p>
          <div className="project-settings">
            <label>
              Dataset name
              <input value={projectName} onChange={(event) => setProjectName(event.target.value)} />
            </label>
            <label>
              Primary language
              <input value={language} onChange={(event) => setLanguage(event.target.value)} />
            </label>
            <button onClick={saveSettings}>Save settings</button>
          </div>
        </div>
        <form className="upload-card" onSubmit={upload}>
          <label className="file-drop">
            <span>{file ? file.name : "Choose podcast video or audio"}</span>
            <input
              type="file"
              accept=".mp4,.mkv,.webm,.mp3,.m4a,.wav,.flac"
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
          </label>
          <button className="primary" disabled={!file || uploading}>
            {uploading ? "Uploading…" : "Add source"}
          </button>
        </form>
      </div>
      <div className="source-list">
        {detail.sources.map((item, index) => {
          const state = sourceState(item, detail.jobs);
          return (
            <div className="source-row" key={item.id}>
              <button
                className="source-row-open"
                onClick={() => onOpenSource(item.id)}
              >
                <span className="source-number">{String(index + 1).padStart(2, "0")}</span>
                <span className="source-main">
                  <strong>{item.original_name}</strong>
                  <small>{state.description}</small>
                </span>
                <span className={`pill ${state.tone}`}>{state.label}</span>
                <span className="source-arrow">→</span>
              </button>
              {state.failedJob && (
                <button
                  type="button"
                  className="source-retry"
                  onClick={() => retry(state.failedJob!)}
                >
                  Retry
                </button>
              )}
            </div>
          );
        })}
        {!detail.sources.length && <div className="empty-card">Upload the first two-person podcast source.</div>}
      </div>
      {!!detail.jobs.length && (
        <section className="job-history card">
          <div>
            <span className="eyebrow">Progress</span>
            <h2>Recent activity</h2>
          </div>
          {detail.jobs.slice(0, 10).map((item) => (
            <div key={item.id}>
              <strong>{item.kind.replaceAll("_", " ")}</strong>
              <span className={`pill ${item.status === "complete" ? "good" : item.status === "failed" ? "bad" : "warn"}`}>
                {item.status}
              </span>
              <small>{item.error || item.message}</small>
              {/* Only a failed first pass may be retried; nothing here can start
                  a second pass on a source that already succeeded. */}
              {item.status === "failed" && item.kind === "initialize" && (
                <button onClick={() => retry(item)}>Retry</button>
              )}
            </div>
          ))}
        </section>
      )}
      <div className="export-panel card">
        <div>
          <span className="eyebrow">Immutable output</span>
          <h2>Dataset exports</h2>
          <p>Every generated clip needs a listen-and-decide result before export.</p>
        </div>
        <div className="export-action">
          <input value={exportName} onChange={(event) => setExportName(event.target.value)} />
          <button onClick={validateExport}>Validate dataset</button>
          <button className="primary" onClick={createExport}>Create version</button>
        </div>
        {validation && (
          <div className={`validation-box ${validation.valid ? "valid" : "invalid"}`}>
            <strong>
              {validation.valid
                ? `${validation.approved_clips} approved clips are export-ready`
                : `${validation.blockers.length} export blockers`}
            </strong>
            {[...validation.blockers, ...validation.warnings].map((message) => (
              <span key={message}>{message}</span>
            ))}
          </div>
        )}
        <div className="export-list">
          {detail.exports.map((item) => (
            <div key={item.id}>
              <strong>v{String(item.version).padStart(3, "0")} · {item.name}</strong>
              <span className={`pill ${item.status === "complete" ? "good" : "warn"}`}>{item.status}</span>
              <small>{item.path || "Waiting for reviewed clips"}</small>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Studio({
  detail,
  project,
  onBack,
  onDeleted,
  onReload,
  onJob,
  setNotice,
  setError,
}: {
  detail: SourceDetail;
  project: ProjectDetail;
  onBack: () => void;
  onDeleted: () => void;
  onReload: () => void;
  onJob: (job: Job) => void;
  setNotice: (message: string) => void;
  setError: (message: string) => void;
}) {
  const [tab, setTab] = useState<"source" | "transcript" | "overlap" | "clips">("source");
  const [annotation, setAnnotation] = useState<Annotation>(detail.annotation);
  const [history, setHistory] = useState<Annotation[]>([]);
  const [future, setFuture] = useState<Annotation[]>([]);
  const [saving, setSaving] = useState(false);
  const saveTimer = useRef<number | null>(null);
  const annotationRef = useRef<Annotation>(detail.annotation);
  const savingRef = useRef(false);
  const pendingSave = useRef<Annotation | null>(null);
  const realignAfterSave = useRef(false);
  const saveFailed = useRef(false);

  useEffect(() => {
    annotationRef.current = detail.annotation;
    saveFailed.current = false;
    setAnnotation(detail.annotation);
    setHistory([]);
    setFuture([]);
  }, [detail.id, detail.annotation.version]);

  useEffect(
    () => () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    },
    [],
  );

  function applyLocal(next: Annotation) {
    if (alignmentInputSignature(next) !== alignmentInputSignature(annotation)) {
      realignAfterSave.current = true;
    }
    annotationRef.current = next;
    setAnnotation(next);
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void save(next), 1200);
  }

  function edit(next: Annotation) {
    const activitiesChanged =
      JSON.stringify(next.activities) !== JSON.stringify(annotation.activities);
    const regionsChanged =
      activitiesChanged || next.assistant_speaker !== annotation.assistant_speaker;
    const prepared = regionsChanged
      ? {
          ...next,
          activities_finalized: false,
          speaker_references: activitiesChanged ? [] : next.speaker_references,
        }
      : next;
    setHistory((values) => [...values.slice(-49), annotation]);
    setFuture([]);
    applyLocal(prepared);
  }

  async function save(value = annotationRef.current) {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (savingRef.current) {
      pendingSave.current = value;
      return;
    }
    savingRef.current = true;
    setSaving(true);
    let completed = false;
    try {
      const saved = await api<Annotation>(
        `/api/sources/${detail.id}/annotations`,
        jsonRequest("PUT", { expected_version: value.version, annotation: value }),
      );
      if (annotationRef.current === value) {
        annotationRef.current = saved;
        pendingSave.current = null;
        setAnnotation(saved);
      } else {
        const rebased = { ...annotationRef.current, version: saved.version };
        annotationRef.current = rebased;
        pendingSave.current = rebased;
        setAnnotation(rebased);
      }
      completed = true;
      saveFailed.current = false;
      setNotice(`Annotation revision ${saved.version} saved`);
    } catch (reason) {
      saveFailed.current = true;
      pendingSave.current = null;
      if (reason instanceof ApiError && reason.status === 409) {
        realignAfterSave.current = false;
        setError(
          "The annotation changed in another job. The latest revision was reloaded; "
          + "please apply your last edit again.",
        );
        await onReload();
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      savingRef.current = false;
      setSaving(false);
      const pending = pendingSave.current;
      pendingSave.current = null;
      if (completed && pending) {
        void save(pending);
      } else if (completed && realignAfterSave.current) {
        realignAfterSave.current = false;
        void queue("realign");
      }
    }
  }

  function undo() {
    const previous = history.at(-1);
    if (!previous) return;
    setFuture((values) => [annotation, ...values]);
    setHistory((values) => values.slice(0, -1));
    applyLocal({ ...previous, version: annotationRef.current.version });
  }

  function redo() {
    const next = future[0];
    if (!next) return;
    setHistory((values) => [...values, annotation]);
    setFuture((values) => values.slice(1));
    applyLocal({ ...next, version: annotationRef.current.version });
  }

  async function deleteSource() {
    if (!window.confirm(`Permanently delete ${detail.original_name} from this workspace?`)) {
      return;
    }
    try {
      await api(`/api/sources/${detail.id}`, {
        method: "DELETE",
        headers: { "x-confirm-delete": detail.id },
      });
      onDeleted();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function restoreRevision(version: number) {
    try {
      const previous = await api<Annotation>(
        `/api/sources/${detail.id}/annotations/${version}`,
      );
      edit({ ...previous, version: annotationRef.current.version });
      setNotice(`Revision ${version} restored locally and queued as a new revision`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function flushAnnotation(): Promise<boolean> {
    if (saveTimer.current || savingRef.current || pendingSave.current) {
      await save(annotationRef.current);
    }
    while (savingRef.current || pendingSave.current) {
      await new Promise((resolve) => window.setTimeout(resolve, 50));
    }
    return !saveFailed.current;
  }

  async function queue(kind: string, body?: unknown) {
    try {
      if (!(await flushAnnotation())) return;
      const job = await api<Job>(
        `/api/sources/${detail.id}/${kind}`,
        jsonRequest("POST", body),
      );
      onJob(job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function queueOverlapTranscription(regionId: string) {
    try {
      if (!(await flushAnnotation())) return;
      const job = await api<Job>(
        `/api/sources/${detail.id}/overlaps/${regionId}/transcribe`,
        jsonRequest("POST"),
      );
      onJob(job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  function setSpeakerReference(speaker: Speaker, activityId: string) {
    const remaining = (annotation.speaker_references || []).filter(
      (value) => value.speaker !== speaker,
    );
    const activity = annotation.activities.find((value) => value.id === activityId);
    const speakerReferences = activity
      ? [
          ...remaining,
          {
            id: `speaker_reference_${crypto.randomUUID().replaceAll("-", "")}`,
            speaker,
            start_sample: activity.start_sample,
            end_sample: Math.min(
              activity.end_sample,
              activity.start_sample + 8 * 24_000,
            ),
            note: "Human-confirmed clean reference turn",
          },
        ]
      : remaining;
    edit({ ...annotation, speaker_references: speakerReferences });
  }

  if (detail.status === "uploaded" || detail.status === "processing") {
    return (
      <section className="page">
        <button className="back" onClick={onBack}>← {project.project.name}</button>
        <div className="onboarding card">
          <span className="eyebrow">New source</span>
          <h1>{detail.original_name}</h1>
          <p>
            Choose whether the studio should suggest speakers and text, or prepare only the
            synchronized media editor.
          </p>
          <div className="choice-grid">
            <button onClick={() => queue("initialize", { mode: "assisted" })}>
              <strong>Assisted start</strong>
              <span>Diarization, overlap, transcription, and word alignment suggestions.</span>
            </button>
            <button onClick={() => queue("initialize", { mode: "manual" })}>
              <strong>Manual start</strong>
              <span>Waveform and video proxy only. Add transcription when ready.</span>
            </button>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="studio-page">
      <aside className="studio-rail">
        <button className="back" onClick={onBack}>← {project.project.name}</button>
        <span className="eyebrow">Source studio</span>
        <h2>{detail.original_name}</h2>
        <div className="source-facts">
          <span><strong>{seconds(detail.duration_samples || 0)}s</strong> duration</span>
          <span><strong>v{annotation.version}</strong> annotation</span>
          <span><strong>{detail.overlaps.length}</strong> overlaps</span>
        </div>
        <nav>
          {[
            ["source", "1", "Source activity"],
            ["transcript", "2", "Transcript"],
            ["overlap", "3", "Overlap recovery"],
            ["clips", "4", "Clips and review"],
          ].map(([value, number, label]) => (
            <button key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value as typeof tab)}>
              <span>{number}</span>{label}
            </button>
          ))}
        </nav>
        <div className="rail-actions">
          <button onClick={undo} disabled={!history.length}>Undo</button>
          <button onClick={redo} disabled={!future.length}>Redo</button>
          <button className="primary" onClick={() => save()} disabled={saving}>{saving ? "Saving…" : "Save now"}</button>
          <details className="revision-history">
            <summary>{detail.annotation_revisions.length} saved revisions</summary>
            {detail.annotation_revisions.map((revision) => (
              <button key={revision.version} onClick={() => restoreRevision(revision.version)}>
                v{revision.version} · {new Date(revision.created_at).toLocaleString()}
              </button>
            ))}
          </details>
          <button className="danger-soft" onClick={deleteSource}>Delete source</button>
        </div>
      </aside>
      <div className="studio-main">
        {tab === "source" && (
          <>
            <section className="studio-heading">
              <div>
                <span className="eyebrow">Authoritative source annotation</span>
                <h1>Who spoke when?</h1>
                <p>Speaker lanes may overlap. Red regions are removed from both exported channels.</p>
              </div>
              <div className="assistant-settings">
                <label className="assistant-choice">
                  Moshi speaker
                  <select
                    value={annotation.assistant_speaker || ""}
                    onChange={(event) => edit({ ...annotation, assistant_speaker: event.target.value as Speaker })}
                  >
                    <option value="">Choose…</option>
                    <option value="A">Speaker A</option>
                    <option value="B">Speaker B</option>
                  </select>
                </label>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={annotation.activities_finalized}
                    disabled={!annotation.assistant_speaker || !annotation.activities.length}
                    onChange={(event) =>
                      edit({ ...annotation, activities_finalized: event.target.checked })
                    }
                  />
                  Human speaker regions are finalized
                </label>
              </div>
            </section>
            {detail.inspection?.channel_routing && (
              <section className="card identity-lock">
                <div>
                  <span className="eyebrow">Channel-first routing</span>
                  <h2>Preserve isolated source channels</h2>
                  <p>
                    The inspector recommends {detail.inspection.channel_routing.recommended_mode.replaceAll("_", " ")}.
                    Independent routing is never enabled without your confirmation.
                  </p>
                  <div className="reason-list">
                    <span>{detail.inspection.channel_routing.reason.replaceAll("_", " ")}</span>
                    {detail.inspection.channel_routing.absolute_correlation !== undefined && (
                      <span>correlation {detail.inspection.channel_routing.absolute_correlation.toFixed(3)}</span>
                    )}
                    {detail.inspection.channel_routing.estimated_lag_ms !== undefined && (
                      <span>lag {detail.inspection.channel_routing.estimated_lag_ms.toFixed(2)} ms</span>
                    )}
                    {detail.inspection.channel_routing.dual_mono && <span>dual mono</span>}
                  </div>
                </div>
                <label>
                  Routing mode
                  <select
                    value={annotation.channel_routing_mode}
                    onChange={(event) => {
                      const mode = event.target.value as Annotation["channel_routing_mode"];
                      edit({
                        ...annotation,
                        channel_routing_mode: mode,
                        channel_routing_verified: false,
                        speaker_channel_map: mode === "independent_stereo"
                          ? {
                              A: detail.inspection?.channel_routing
                                ?.suggested_speaker_channel_map?.A ?? 0,
                              B: detail.inspection?.channel_routing
                                ?.suggested_speaker_channel_map?.B ?? 1,
                            }
                          : {},
                      });
                    }}
                  >
                    <option value="mono">Mixed/mono analysis</option>
                    <option
                      value="independent_stereo"
                      disabled={
                        !detail.urls.canonical_channels
                        || detail.inspection?.channel_routing?.channel_count !== 2
                      }
                    >
                      Independent stereo channels
                    </option>
                  </select>
                </label>
                {annotation.channel_routing_mode === "independent_stereo" && (
                  <>
                    <label>
                      Speaker A channel
                      <select
                        value={annotation.speaker_channel_map.A ?? 0}
                        onChange={(event) => {
                          const channel = Number(event.target.value);
                          edit({
                            ...annotation,
                            channel_routing_verified: false,
                            speaker_channel_map: { A: channel, B: 1 - channel },
                          });
                        }}
                      >
                        <option value={0}>Channel 1 / left</option>
                        <option value={1}>Channel 2 / right</option>
                      </select>
                    </label>
                    <label>
                      Speaker B channel
                      <strong>
                        {annotation.speaker_channel_map.B === 0
                          ? "Channel 1 / left"
                          : "Channel 2 / right"}
                      </strong>
                    </label>
                    {detail.urls.canonical_channels && (
                      <label className="wide">
                        Preserved stereo preview
                        <audio controls preload="metadata" src={detail.urls.canonical_channels} />
                      </label>
                    )}
                    <label className="checkbox wide">
                      <input
                        type="checkbox"
                        checked={annotation.channel_routing_verified}
                        onChange={(event) =>
                          edit({
                            ...annotation,
                            channel_routing_verified: event.target.checked,
                          })
                        }
                      />
                      I confirmed that A and B are isolated on the selected channels.
                    </label>
                  </>
                )}
              </section>
            )}
            <section className="card identity-lock">
              <div>
                <span className="eyebrow">Stable speaker identity</span>
                <h2>Lock A and B to confirmed voices</h2>
                <p>
                  Choose one clean, non-overlapping turn for each person. Stable diarization
                  matches future detected labels to these voice references.
                </p>
              </div>
              {(["A", "B"] as Speaker[]).map((speaker) => {
                const reference = (annotation.speaker_references || []).find(
                  (value) => value.speaker === speaker,
                );
                const turns = cleanSpeakerTurns(annotation, speaker);
                const selected = turns.find(
                  (turn) =>
                    turn.start_sample === reference?.start_sample
                    && Math.min(
                      turn.end_sample,
                      turn.start_sample + 8 * 24_000,
                    ) === reference?.end_sample,
                );
                return (
                  <label key={speaker}>
                    Speaker {speaker} reference
                    <select
                      value={selected?.id || ""}
                      onChange={(event) =>
                        setSpeakerReference(speaker, event.target.value)
                      }
                    >
                      <option value="">Choose clean turn…</option>
                      {turns.map((turn) => (
                        <option key={turn.id} value={turn.id}>
                          {seconds(turn.start_sample)}–{seconds(turn.end_sample)}s ·{" "}
                          first {Math.min(
                            8,
                            Number(seconds(turn.end_sample - turn.start_sample)),
                          )}s
                        </option>
                      ))}
                    </select>
                  </label>
                );
              })}
              <button
                className="primary"
                disabled={
                  new Set((annotation.speaker_references || []).map((value) => value.speaker))
                    .size !== 2
                }
                onClick={() => queue("rediarize")}
              >
                Run stable diarization
              </button>
            </section>
            <QualityDashboard detail={detail} annotation={annotation} />
            <WaveformEditor
              audioUrl={detail.urls.canonical_audio}
              videoUrl={detail.urls.video_proxy}
              annotation={annotation}
              durationSamples={detail.duration_samples || 1}
              frameRate={detail.inspection?.video_frame_rate || 30}
              onChange={edit}
            />
            <Rights detail={detail} onReload={onReload} setError={setError} />
          </>
        )}
        {tab === "transcript" && (
          <Transcript
            detail={detail}
            annotation={annotation}
            onChange={edit}
            onTranscribe={() => queue("transcribe")}
            onRealign={() => queue("realign")}
            onReview={() => queue("review-transcript")}
          />
        )}
        {tab === "overlap" && (
          <Overlap
            detail={detail}
            annotation={annotation}
            onRecover={() => queue("recover-overlap")}
            onTranscribe={queueOverlapTranscription}
            onReload={onReload}
            setError={setError}
          />
        )}
        {tab === "clips" && (
          <Clips
            detail={detail}
            onGenerate={() => queue("generate")}
            onReload={onReload}
            setError={setError}
          />
        )}
      </div>
    </section>
  );
}

function QualityDashboard({
  detail,
  annotation,
}: {
  detail: SourceDetail;
  annotation: Annotation;
}) {
  const metrics = detail.quality_dashboard || {
    assistant_alignment_coverage: 0,
    golden_target: 20,
    model_character_error_rate: null,
    speaker_correction_rate: null,
  };
  const unresolved = annotation.transcript.filter(
    (value) =>
      value.quality_flags.some((flag) => highRiskTranscriptFlags.has(flag))
      && !value.human_verified,
  );
  const goldenExamples = annotation.transcript.filter(
    (value) => value.human_verified && value.text.trim(),
  ).length;
  const percent = (value?: number | null) =>
    value == null ? "Not measured" : `${(value * 100).toFixed(1)}%`;
  return (
    <section className="card quality-dashboard">
      <div>
        <span className="eyebrow">Accuracy dashboard</span>
        <h2>Review evidence, not just model confidence</h2>
      </div>
      <div className="metric-grid">
        <span>
          <small>Review queue</small>
          <strong>{unresolved.length}</strong>
        </span>
        <span>
          <small>Moshi unresolved</small>
          <strong>
            {unresolved.filter(
              (value) => value.speaker === annotation.assistant_speaker,
            ).length}
          </strong>
        </span>
        <span>
          <small>Moshi alignment</small>
          <strong>{percent(metrics.assistant_alignment_coverage)}</strong>
        </span>
        <span>
          <small>Golden examples</small>
          <strong>{goldenExamples}/{metrics.golden_target}</strong>
        </span>
        <span>
          <small>Model character error</small>
          <strong>{percent(metrics.model_character_error_rate)}</strong>
        </span>
        <span>
          <small>Speaker correction rate</small>
          <strong>{percent(metrics.speaker_correction_rate)}</strong>
        </span>
      </div>
      <p>
        Human-verified utterances form the exported golden regression set. Aim for at
        least {metrics.golden_target} representative Egyptian-Arabic and code-switching
        examples before comparing model or configuration changes.
      </p>
    </section>
  );
}

function Rights({ detail, onReload, setError }: { detail: SourceDetail; onReload: () => void; setError: (value: string) => void }) {
  const [origin, setOrigin] = useState(detail.origin);
  const [basis, setBasis] = useState(detail.rights_basis || "licensed");
  const [notes, setNotes] = useState(detail.rights_notes);
  const [confirmed, setConfirmed] = useState(detail.rights_confirmed);

  async function save() {
    const normalizedOrigin = origin.trim();
    if (!normalizedOrigin) {
      setError("Enter the recording origin or source URL before saving rights.");
      return;
    }
    try {
      await api(`/api/sources/${detail.id}/rights`, jsonRequest("PUT", {
        origin: normalizedOrigin,
        rights_basis: basis,
        rights_notes: notes,
        rights_confirmed: confirmed,
      }));
      onReload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <section className="card rights-card">
      <div><span className="eyebrow">Required before export</span><h2>Source rights and provenance</h2></div>
      <label>Origin or source URL<input value={origin} onChange={(event) => setOrigin(event.target.value)} placeholder="Owned recording or source URL" /></label>
      <label>Rights basis<select value={basis} onChange={(event) => setBasis(event.target.value)}><option value="owned">Owned</option><option value="consent">Participant consent</option><option value="licensed">Licensed</option><option value="public_domain">Public domain</option><option value="other">Other</option></select></label>
      <label className="wide">Notes<textarea value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
      <label className="checkbox wide"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> I confirm this source may be used for model training.</label>
      <button className="primary" onClick={save}>Save declaration</button>
    </section>
  );
}

function Transcript({
  detail,
  annotation,
  onChange,
  onTranscribe,
  onRealign,
  onReview,
}: {
  detail: SourceDetail;
  annotation: Annotation;
  onChange: (value: Annotation) => void;
  onTranscribe: () => void;
  onRealign: () => void;
  onReview: () => void;
}) {
  const [view, setView] = useState<"queue" | "all">("queue");
  const priorities = new Map(
    annotation.transcript
      .map((value) => [
        value.id,
        transcriptPriority(value, annotation.assistant_speaker),
      ] as const)
      .filter(([, priority]) => priority > 0),
  );
  const unresolvedFlagged = annotation.transcript.filter(
    (value) =>
      value.quality_flags.some((flag) => highRiskTranscriptFlags.has(flag))
      && !value.human_verified,
  ).length;
  const display = annotation.transcript
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => view === "all" || priorities.has(item.id))
    .sort((first, second) =>
      view === "queue"
        ? (priorities.get(second.item.id) || 0)
          - (priorities.get(first.item.id) || 0)
        : first.index - second.index
    );
  return (
    <section>
      <div className="studio-heading">
        <div><span className="eyebrow">Text then realign</span><h1>Correct the conversation.</h1><p>Only the selected Moshi speaker’s aligned words enter the official sidecar. Flagged Moshi utterances must be corrected, realigned, then verified against the audio before export.</p></div>
        <div className="heading-actions">
          <button onClick={onTranscribe}>Generate transcript</button>
          <button
            onClick={onReview}
            disabled={!unresolvedFlagged}
          >
            Generate second-pass candidates
          </button>
          <button className="primary" onClick={onRealign}>Realign corrected text</button>
        </div>
      </div>
      <div className="review-toolbar card">
        <div>
          <strong>{priorities.size} prioritized items</strong>
          <small>
            Character corruption, repeated text, overlap, decoder disagreement, and
            alignment failures appear first.
          </small>
        </div>
        <div className="heading-actions">
          <button className={view === "queue" ? "primary" : ""} onClick={() => setView("queue")}>
            Review queue
          </button>
          <button className={view === "all" ? "primary" : ""} onClick={() => setView("all")}>
            All utterances
          </button>
        </div>
      </div>
      <div className="transcript-list">
        {display.map(({ item, index }) => {
          const requiresVerification = item.quality_flags.some((flag) =>
            highRiskTranscriptFlags.has(flag)
          );
          return (
          <article className={`utterance ${requiresVerification ? "needs-verification" : ""}`} key={item.id}>
            <div className="utterance-meta">
              <span>{seconds(item.start_sample)}–{seconds(item.end_sample)}s</span>
              {priorities.has(item.id) && (
                <span className="pill warn">
                  Priority {priorities.get(item.id)}
                </span>
              )}
              <select
                value={item.speaker || ""}
                onChange={(event) => onChange({
                  ...annotation,
                  transcript: annotation.transcript.map((value, itemIndex) => itemIndex === index ? {
                    ...value,
                    speaker: event.target.value as Speaker,
                    alignment_status: "not_run",
                    human_verified: false,
                  } : value),
                })}
              >
                <option value="">Unknown</option><option value="A">Speaker A</option><option value="B">Speaker B</option>
              </select>
              <span className={`pill ${item.alignment_status === "aligned" ? "good" : "warn"}`}>{item.alignment_status.replaceAll("_", " ")}</span>
              {item.quality_flags.map((flag) => (
                <span className="quality-flag" key={flag}>{flag.replaceAll("_", " ")}</span>
              ))}
              <label className="checkbox transcript-verification">
                <input
                  type="checkbox"
                  checked={item.human_verified}
                  disabled={item.alignment_status !== "aligned"}
                  onChange={(event) => onChange({
                    ...annotation,
                    transcript: annotation.transcript.map((value, itemIndex) =>
                      itemIndex === index
                        ? { ...value, human_verified: event.target.checked }
                        : value
                    ),
                  })}
                />
                {requiresVerification ? "Verified against audio" : "Add to golden set"}
              </label>
            </div>
            <div className="utterance-editor">
              <audio
                controls
                preload="metadata"
                src={`${detail.urls.canonical_audio}#t=${seconds(item.start_sample)},${seconds(item.end_sample)}`}
              />
              <textarea
                dir="rtl"
                lang="ar"
                value={item.text}
                onChange={(event) => onChange({
                  ...annotation,
                  transcript: annotation.transcript.map((value, itemIndex) => itemIndex === index ? {
                    ...value,
                    text: event.target.value,
                    alignment_status: "not_run",
                    human_verified: false,
                  } : value),
                })}
              />
              {!!item.review_candidates?.length && (
                <div className="candidate-list">
                  {item.review_candidates.map((candidate, candidateIndex) => (
                    <div className="candidate-card" key={`${candidate.source}-${candidateIndex}`}>
                      <div>
                        <strong>{candidate.source.replaceAll("_", " ")}</strong>
                        <small>{candidate.model}</small>
                      </div>
                      <p dir="rtl" lang="ar">{candidate.text || "No speech decoded"}</p>
                      <div className="reason-list">
                        {candidate.quality_flags.map((flag) => (
                          <span key={flag}>{flag.replaceAll("_", " ")}</span>
                        ))}
                      </div>
                      <button
                        disabled={!candidate.text.trim()}
                        onClick={() => onChange({
                          ...annotation,
                          transcript: annotation.transcript.map((value, itemIndex) =>
                            itemIndex === index
                              ? {
                                  ...value,
                                  text: candidate.text,
                                  alignment_status: "not_run",
                                  human_verified: false,
                                }
                              : value
                          ),
                        })}
                      >
                        Use this candidate
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </article>
          );
        })}
        {!display.length && (
          <div className="empty-card">
            {annotation.transcript.length
              ? "The prioritized review queue is clear."
              : "Generate a transcript or add source text after manual annotation."}
          </div>
        )}
      </div>
    </section>
  );
}

function Overlap({ detail, annotation, onRecover, onTranscribe, onReload, setError }: { detail: SourceDetail; annotation: Annotation; onRecover: () => void; onTranscribe: (regionId: string) => void; onReload: () => void; setError: (value: string) => void }) {
  const [auditioned, setAuditioned] = useState<Record<string, boolean>>({});
  const recoveryReady = Boolean(
    annotation.assistant_speaker && annotation.activities_finalized,
  );

  async function decide(regionId: string, decision: "approve" | "reject") {
    try {
      await api(
        `/api/sources/${detail.id}/overlaps/${regionId}/decision`,
        jsonRequest("POST", { decision, auditioned: Boolean(auditioned[regionId]) }),
      );
      onReload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <section>
      <div className="studio-heading">
        <div><span className="eyebrow">Mixed audio safeguard</span><h1>Recover overlap, region by region.</h1><p>Unapproved or failed regions remain muted in both output channels.</p></div>
        <button className="primary" disabled={!recoveryReady} onClick={onRecover}>
          Run overlap recovery
        </button>
      </div>
      {!recoveryReady && (
        <div className="validation-box invalid">
          <strong>Overlap recovery is locked</strong>
          <span>
            Return to Source activity, choose the Moshi speaker, review the A/B
            regions, enable “Human speaker regions are finalized,” and wait for the
            saved revision.
          </span>
        </div>
      )}
      <div className="overlap-list">
        {detail.overlap_recoveries.map((item, index) => (
          <article className="overlap-card card" key={item.region_id}>
            <div className="overlap-title"><span className="source-number">{String(index + 1).padStart(2, "0")}</span><div><strong>{seconds(item.start_sample)}–{seconds(item.end_sample)}s</strong><small>{item.status}</small></div><span className={`pill ${item.decision === "approve" ? "good" : "warn"}`}>{item.decision || "needs review"}</span></div>
            <div className="audition-grid">
              <label>Original mixture<audio controls src={`/media/${detail.id}/overlap/${item.region_id}/original`} /></label>
              {item.status === "recovered" && <><label>Recovered Moshi<audio controls src={`/media/${detail.id}/overlap/${item.region_id}/assistant`} /></label><label>Recovered user<audio controls src={`/media/${detail.id}/overlap/${item.region_id}/user`} /></label></>}
            </div>
            {item.status === "recovered" && (
              <>
              <div className="stem-transcript-tools">
                <button onClick={() => onTranscribe(item.region_id)}>
                  Transcribe isolated stems
                </button>
                <small>
                  Compare isolated-voice text with the mixed transcript before approval.
                </small>
              </div>
              {Boolean(item.details.stem_transcripts) && (
                <div className="stem-transcripts">
                  {Object.entries(
                    item.details.stem_transcripts as Record<
                      string,
                      { text: string; model: string; quality_flags: string[] }
                    >,
                  ).map(([role, candidate]) => (
                    <div key={role}>
                      <strong>{role === "assistant" ? "Recovered Moshi" : "Recovered user"}</strong>
                      <small>{candidate.model}</small>
                      <p dir="rtl" lang="ar">{candidate.text || "No speech decoded"}</p>
                      <div className="reason-list">
                        {candidate.quality_flags.map((flag) => (
                          <span key={flag}>{flag.replaceAll("_", " ")}</span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <div className="clip-decision">
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={Boolean(auditioned[item.region_id] || item.auditioned)}
                    onChange={(event) =>
                      setAuditioned((values) => ({
                        ...values,
                        [item.region_id]: event.target.checked,
                      }))
                    }
                  />
                  I auditioned the original and both recovered voices.
                </label>
                <div className="heading-actions">
                  <button
                    className="primary"
                    disabled={!auditioned[item.region_id] && !item.auditioned}
                    onClick={() => decide(item.region_id, "approve")}
                  >
                    Approve recovery
                  </button>
                  <button
                    className="danger-soft"
                    disabled={!auditioned[item.region_id] && !item.auditioned}
                    onClick={() => decide(item.region_id, "reject")}
                  >
                    Reject and mute
                  </button>
                </div>
              </div>
              </>
            )}
          </article>
        ))}
        {!detail.overlap_recoveries.length && <div className="empty-card">{detail.overlaps.length ? `${detail.overlaps.length} overlap regions are ready for recovery.` : "No overlap is currently derived from the speaker lanes."}</div>}
      </div>
    </section>
  );
}

function Clips({ detail, onGenerate, onReload, setError }: { detail: SourceDetail; onGenerate: () => void; onReload: () => void; setError: (value: string) => void }) {
  const [mode, setMode] = useState<"" | "count" | "target_duration" | "manual">("");
  const [value, setValue] = useState("5");
  const [manual, setManual] = useState("0, 60");
  const [boundaries, setBoundaries] = useState<number[]>([]);
  const plan = detail.clip_plan;
  const artifacts = detail.clip_artifacts?.artifacts || [];
  const minimumClipCount = Math.max(
    1,
    Math.ceil((detail.duration_samples || 0) / (100 * 24_000)),
  );

  useEffect(() => {
    if (!plan?.clips.length) {
      setBoundaries([]);
      return;
    }
    setBoundaries([
      plan.clips[0].start_sample,
      ...plan.clips.map((clip) => clip.end_sample),
    ]);
    if (plan.mode === "count" || plan.mode === "target_duration" || plan.mode === "manual") {
      setMode(plan.mode);
    }
    if (plan.mode === "count" && plan.request.count) {
      setValue(String(plan.request.count));
    } else if (plan.mode === "target_duration" && plan.request.target_duration_seconds) {
      setValue(String(plan.request.target_duration_seconds));
    }
  }, [plan]);

  async function propose(
    selectedMode = mode,
    adjusted = boundaries,
    countOverride?: number,
  ) {
    if (!selectedMode) {
      setError("Choose a planning mode first");
      return;
    }
    const body = selectedMode === "count"
      ? { mode: selectedMode, count: countOverride ?? Number(value) }
      : selectedMode === "target_duration"
        ? { mode: selectedMode, target_duration_seconds: Number(value) }
        : {
            mode: "manual",
            boundaries_samples: adjusted.length >= 2
              ? adjusted
              : manual.split(",").map((item) => Math.round(Number(item.trim()) * 24_000)),
          };
    try {
      await api(`/api/sources/${detail.id}/clip-plan`, jsonRequest("POST", body));
      onReload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  function moveBoundary(index: number, position: number) {
    setBoundaries((current) =>
      current.map((valueAtIndex, itemIndex) =>
        itemIndex === index
          ? Math.max(current[index - 1] + 1, Math.min(current[index + 1] - 1, position))
          : valueAtIndex,
      ),
    );
  }

  function addBoundary() {
    if (boundaries.length < 2) return;
    let longestIndex = 0;
    for (let index = 1; index < boundaries.length - 1; index += 1) {
      if (
        boundaries[index + 1] - boundaries[index]
        > boundaries[longestIndex + 1] - boundaries[longestIndex]
      ) {
        longestIndex = index;
      }
    }
    const position = Math.round(
      (boundaries[longestIndex] + boundaries[longestIndex + 1]) / 2,
    );
    setBoundaries([
      ...boundaries.slice(0, longestIndex + 1),
      position,
      ...boundaries.slice(longestIndex + 1),
    ]);
  }

  return (
    <section>
      <div className="studio-heading"><div><span className="eyebrow">Conversation-aware boundaries</span><h1>Plan, render, then listen.</h1><p>Final clips must be 20–100 seconds and contain both speakers plus an exchange.</p></div></div>
      <div className="planner card">
        <label>Planning mode<select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}><option value="">Choose a mode…</option><option value="count">Desired clip count</option><option value="target_duration">Target duration</option><option value="manual">Manual boundaries</option></select></label>
        {mode === "manual" ? <label className="wide">Boundary seconds, comma separated<input value={manual} onChange={(event) => setManual(event.target.value)} /></label> : mode ? <label>{mode === "count" ? "Clip count" : "Target seconds"}<input type="number" value={value} onChange={(event) => setValue(event.target.value)} /></label> : null}
        <button className="primary" disabled={!mode} onClick={() => propose()}>Propose boundaries</button>
        <button
          onClick={() => {
            setMode("count");
            setValue(String(minimumClipCount));
            void propose("count", boundaries, minimumClipCount);
          }}
        >
          Find safe {minimumClipCount}+-clip plan
        </button>
        <small className="wide">
          This {seconds(detail.duration_samples || 0)}s source needs at least {minimumClipCount} clips.
          Automatic planning searches aligned-word pauses inside long speaker turns.
        </small>
      </div>
      {plan && <div className="plan-summary"><span className={`pill ${plan.feasible ? "good" : "bad"}`}>{plan.feasible ? "Feasible" : "Needs adjustment"}</span><p>{plan.message || `${plan.clips.length} valid conversation clips`}</p><button className="primary" disabled={!plan.feasible} onClick={onGenerate}>{detail.clips_stale ? "Generate stereo clips" : "Regenerate clips"}</button></div>}
      {boundaries.length >= 2 && (
        <div className="boundary-editor card">
          <div>
            <span className="eyebrow">Manual refinement</span>
            <h2>Drag, add, or remove boundaries</h2>
            <p>Automatic planning is recommended. Manual markers are re-checked for words, overlap, speaker balance, and duration after saving.</p>
          </div>
          {boundaries.slice(1, -1).map((position, innerIndex) => {
            const index = innerIndex + 1;
            return (
              <label key={`boundary-${index}`}>
                <span>Boundary {index} · {seconds(position)}s</span>
                <input
                  type="range"
                  min={boundaries[index - 1] + 1}
                  max={boundaries[index + 1] - 1}
                  step="1"
                  value={position}
                  onChange={(event) => moveBoundary(index, Number(event.target.value))}
                />
                <button
                  className="danger-soft"
                  onClick={() =>
                    setBoundaries(boundaries.filter((_, itemIndex) => itemIndex !== index))
                  }
                >
                  Remove
                </button>
              </label>
            );
          })}
          <div className="heading-actions">
            <button onClick={addBoundary}>Add approximate boundary</button>
            <button className="primary" onClick={() => propose("manual", boundaries)}>
              Save adjusted boundaries
            </button>
          </div>
        </div>
      )}
      <div className="clip-list">
        {(artifacts.length ? artifacts : (plan?.clips || []).map((clip) => ({ clip } as ClipArtifact))).map((item, index) => (
          <ClipReview key={item.clip.id} detail={detail} item={item} index={index} rendered={artifacts.length > 0} onReload={onReload} setError={setError} />
        ))}
      </div>
    </section>
  );
}

function ClipReview({ detail, item, index, rendered, onReload, setError }: { detail: SourceDetail; item: ClipArtifact; index: number; rendered: boolean; onReload: () => void; setError: (value: string) => void }) {
  const [listened, setListened] = useState(item.decision?.auditioned || false);

  async function decide(decision: "approve" | "reject" | "needs_work") {
    try {
      await api(`/api/sources/${detail.id}/clips/${item.clip.id}/decision`, jsonRequest("POST", { decision, auditioned: listened }));
      onReload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <article className="clip-card card">
      <div className="clip-heading"><span className="source-number">{String(index + 1).padStart(2, "0")}</span><div><strong>{seconds(item.clip.start_sample)}–{seconds(item.clip.end_sample)}s</strong><small>{Number(item.clip.metrics.duration_seconds).toFixed(1)} seconds · {item.clip.metrics.speaker_exchanges} exchanges</small></div><span className={`pill ${rendered && item.qc?.status === "PASS" ? "good" : item.clip.status === "valid" ? "warn" : "bad"}`}>{rendered ? item.qc.status : item.clip.status}</span></div>
      {rendered && (
        <>
          <div className="playback-compare">
            <label>
              Original mixture
              <audio
                controls
                preload="metadata"
                src={`${detail.urls.canonical_audio}#t=${seconds(item.clip.start_sample)},${seconds(item.clip.end_sample)}`}
              />
            </label>
            <label>
              Rendered stereo
              <StereoPlayer src={`/media/${detail.id}/clips/${item.clip.id}/audio`} />
            </label>
          </div>
          <div className="metric-grid">
            {Object.entries(item.qc.metrics).slice(0, 8).map(([name, metric]) => (
              <span key={name}>
                <small>{name.replaceAll("_", " ")}</small>
                <strong>{typeof metric === "number" ? metric.toFixed(3) : String(metric)}</strong>
              </span>
            ))}
          </div>
          <div className="reason-list">
            {Number(item.clip.metrics.exclusion_ratio) > 0 && <span>contains muted exclusions</span>}
            {item.raw_overlap_ratio > 0 && <span>contains overlap</span>}
            {item.separation_used && <span>approved overlap recovery used</span>}
            {item.routing_method && (
              <span>routing: {item.routing_method.replaceAll("_", " ")}</span>
            )}
            {item.recovery_method && (
              <span>recovery: {item.recovery_method.replaceAll("_", " ")}</span>
            )}
          </div>
          {!!item.transcript?.original_and_normalized.length && (
            <p className="clip-transcript" dir="rtl" lang="ar">
              {item.transcript.original_and_normalized.map((word) => word.original).join(" ")}
            </p>
          )}
        </>
      )}
      <div className="reason-list">{(rendered ? item.qc.reasons : item.clip.reasons).map((reason) => <span key={reason}>{reason.replaceAll("_", " ")}</span>)}</div>
      {rendered && <div className="clip-decision"><label className="checkbox"><input type="checkbox" checked={listened} onChange={(event) => setListened(event.target.checked)} /> I listened to the complete stereo clip.</label><div className="heading-actions"><button className="primary" disabled={!listened || item.qc.status === "REJECT"} onClick={() => decide("approve")}>Approve</button><button onClick={() => decide("needs_work")}>Needs work</button><button className="danger-soft" onClick={() => decide("reject")}>Reject</button></div>{item.decision && <span className="pill good">Saved: {item.decision.decision}</span>}</div>}
    </article>
  );
}

export default App;
