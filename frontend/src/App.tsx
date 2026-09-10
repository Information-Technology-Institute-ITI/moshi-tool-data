import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  deleteProject,
  exportDataset,
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
import ErrorBoundary from "./components/ErrorBoundary";
import GpuStatusPage from "./components/GpuStatusPage";
import IntroPage from "./components/IntroPage";
import JobProgress from "./components/JobProgress";
import CommandPalette from "./components/CommandPalette";
import ReviewPlayerCard from "./components/ReviewPlayerCard";
import ReviewToolRail from "./components/ReviewToolRail";
import TranscriptPanel, { type TranscriptPanelHandle } from "./components/TranscriptPanel";
import WaveformEditor, {
  type FocusRange,
  type WaveformEditorHandle,
} from "./components/WaveformEditor";
import {
  commandIdForKeyboardEvent,
  REVIEW_COMMANDS,
  type ReviewCommand,
} from "./reviewCommands";
import {
  addAllOverlapSegments,
  addOverlapSegments,
  addSegment,
  deleteActivity,
  deleteSegment,
  intersecting,
  joinSegments,
  segmentsForActivity,
  prepareSplit,
  splitSegment,
  type SplitPreview,
  chronological,
} from "./transcript";
import {
  clearDraftsForUser,
  useAnnotationSaver,
  type Conflict,
} from "./useAnnotationSaver";
import type {
  AdminUser,
  Annotation,
  AuthUser,
  Job,
  Project,
  Source,
  SourceDetail,
  Speaker,
  TranscriptUtterance,
} from "./types";

type ProjectDetail = {
  project: Project;
  sources: Source[];
  jobs: Job[];
};

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
  const studioNavigationGuard = useRef<null | ((action: () => void) => void)>(null);
  const studioDirty = useRef(false);
  // The job watcher is a server-sent-event stream that outlives the render that
  // started it. Reading the open screen from refs keeps it from acting on a
  // dataset the user has already navigated away from.
  const openSourceId = useRef<string | null>(null);
  const openProjectId = useRef<string | null>(null);
  const isAdmin = authUser?.role === "admin";

  function navigate(action: () => void) {
    const guard = studioNavigationGuard.current;
    if (guard) guard(action);
    else action();
  }

  useEffect(() => {
    clearDraftsForUser(authUser?.id || "local");
  }, [authUser?.id]);

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
      if (value.status !== "complete") return;
      setNotice(`${value.kind.replaceAll("_", " ")} complete`);
      // Refresh only what the user is actually looking at now. Reopening a
      // source they had left used to drop them back into it with no project
      // loaded behind it, which blanked the page.
      const sourceId = openSourceId.current;
      const projectId = openProjectId.current;
      if (sourceId) {
        if (studioDirty.current) {
          setNotice("New server data is available. Save or discard your changes before reloading.");
          return;
        }
        await openSource(sourceId);
        if (projectId) {
          const refreshed = await run(() =>
            api<ProjectDetail>(`/api/projects/${projectId}`),
          );
          if (refreshed) setProject(refreshed);
        }
      } else if (projectId) {
        await openProject(projectId);
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
    // Drafts are scoped per user and must never surface for the next person
    // signing in on this browser.
    if (authUser) clearDraftsForUser(authUser.id);
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

  openSourceId.current = source?.id ?? null;
  openProjectId.current = project?.project.id ?? null;

  const view = page === "gpu" && isAdmin ? (
    <GpuStatusPage />
  ) : source && project ? (
    <Studio
      detail={source}
      project={project}
      onBack={() => setSource(null)}
      onDeleted={() => openProject(project.project.id)}
      onJob={monitor}
      setNotice={setNotice}
      setError={setError}
      onDirtyChange={(dirty) => {
        studioDirty.current = dirty;
      }}
      registerNavigationGuard={(guard) => {
        studioNavigationGuard.current = guard;
      }}
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
            navigate(() => {
              setPage("workspace");
              setProject(null);
              setSource(null);
            });
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
              onClick={() => navigate(() => setPage("gpu"))}
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
            <button
              className="system-nav"
              type="button"
              onClick={() => navigate(() => void handleSignout())}
            >
              Sign out
            </button>
          )}
        </div>
      </header>
      {error && <div className="banner error" role="alert">{error}<button onClick={() => setError("")}>×</button></div>}
      {notice && <div className="banner success">{notice}<button onClick={() => setNotice("")}>×</button></div>}
      <JobProgress job={job} onRetry={retryJob} />
      <ErrorBoundary onReset={returnToLibrary} resetLabel="Back to my datasets">
        {view}
      </ErrorBoundary>
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
  const [exportFor, setExportFor] = useState<Project | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);

  /** Builds and downloads the dataset archive. Administrators only. */
  async function exportOne(project: Project, includeMedia: boolean) {
    setExportFor(null);
    setExporting(project.id);
    try {
      await exportDataset(project.id, { includeMedia });
      setNotice(
        `"${project.name}" was exported${includeMedia ? " with its audio" : " without audio"}.`
          + " Check your downloads.",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setExporting(null);
    }
  }

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
                <button
                  type="button"
                  disabled={!item.ready_sources || exporting === item.id}
                  title={
                    item.ready_sources
                      ? "Download this dataset's transcripts, word alignment and"
                        + " diarization, with the audio if you ask for it"
                      : "No source in this dataset has been prepared yet"
                  }
                  onClick={() => setExportFor(item)}
                >
                  {exporting === item.id ? "Preparing…" : "Export"}
                </button>
              )}
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
      {exportFor && (
        <ExportDatasetDialog
          project={exportFor}
          onCancel={() => setExportFor(null)}
          onConfirm={(includeMedia) => exportOne(exportFor, includeMedia)}
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
 * Asks what to put in the download before building it.
 *
 * The audio is by far the largest part of an archive and is usually already to
 * hand, so it is left out unless it is asked for. The transcripts name the file
 * every range belongs to either way.
 */
function ExportDatasetDialog({
  project,
  onCancel,
  onConfirm,
}: {
  project: Project;
  onCancel: () => void;
  onConfirm: (includeMedia: boolean) => void | Promise<void>;
}) {
  const [includeMedia, setIncludeMedia] = useState(false);
  useEscapeToClose(onCancel);

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-title"
      >
        <h2 id="export-title">Export “{project.name}”</h2>
        <p>
          Downloads one archive holding a CSV of every transcript segment, and the final
          transcript, word alignment and diarization of each source as JSON.
        </p>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={includeMedia}
            onChange={(event) => setIncludeMedia(event.target.checked)}
          />
          Include the audio files
        </label>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" onClick={() => void onConfirm(includeMedia)}>
            Export
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
  const [projectName, setProjectName] = useState(detail.project.name);
  const [language, setLanguage] = useState(detail.project.language);
  const activeJob = detail.jobs.find((item) =>
    item.status === "queued" || item.status === "running",
  );

  useEffect(() => {
    setProjectName(detail.project.name);
    setLanguage(detail.project.language);
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
    </section>
  );
}


/**
 * The single Review audio and transcript screen.
 *
 * Video frames, waveform, speaker A/B lanes, the complete chronological
 * transcript, and quality flags share one sample timeline. Selecting anything
 * focuses the matching content elsewhere. Every edit changes annotation only:
 * no action here creates, cuts, or re-encodes audio, and no action enqueues GPU
 * work. A successful source can never be reprocessed from this screen.
 */
function annotationContent(annotation: Annotation): string {
  const { version: _version, ...content } = annotation;
  return JSON.stringify(content);
}

type PendingSplit = SplitPreview;

function Studio({
  detail,
  project,
  onBack,
  onDeleted,
  onJob,
  setNotice,
  setError,
  onDirtyChange,
  registerNavigationGuard,
}: {
  detail: SourceDetail;
  project: ProjectDetail;
  onBack: () => void;
  onDeleted: () => void;
  onJob: (job: Job) => void;
  setNotice: (message: string) => void;
  setError: (message: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  registerNavigationGuard: (guard: null | ((action: () => void) => void)) => void;
}) {
  const [annotation, setAnnotation] = useState<Annotation>(detail.annotation);
  const [savedAnnotation, setSavedAnnotation] = useState<Annotation>(detail.annotation);
  const [revisions, setRevisions] = useState(detail.annotation_revisions);
  const [history, setHistory] = useState<Annotation[]>([]);
  const [future, setFuture] = useState<Annotation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filteredIds, setFilteredIds] = useState<string[] | null>(null);
  const [focusRange, setFocusRange] = useState<FocusRange | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [regionToDelete, setRegionToDelete] = useState<string | null>(null);
  const [splitPreview, setSplitPreview] = useState<PendingSplit | null>(null);
  const [pendingBounds, setPendingBounds] = useState(false);
  const [pendingNavigation, setPendingNavigation] = useState<null | (() => void)>(null);
  const [railOpen, setRailOpen] = useState(false);
  const [commandDialog, setCommandDialog] = useState<"palette" | "help" | null>(null);
  const [inspectorTarget, setInspectorTarget] = useState<HTMLDivElement | null>(null);
  const [timelineToolsTarget, setTimelineToolsTarget] = useState<HTMLDivElement | null>(null);
  const transcriptPanel = useRef<TranscriptPanelHandle | null>(null);
  const waveformEditor = useRef<WaveformEditorHandle | null>(null);
  const focusNonce = useRef(0);
  // True between the first keystroke of a run of typing and the moment it is
  // committed, so a whole edit undoes at once instead of one letter at a time.
  const typing = useRef(false);

  const processing = detail.status === "processing";
  const readOnly = processing;
  const dirty =
    pendingBounds || annotationContent(annotation) !== annotationContent(savedAnnotation);

  const saver = useAnnotationSaver({
    sourceId: detail.id,
    onConflict: (value) => setConflict(value),
    onError: setError,
  });
  const saving = saver.status === "saving";
  const locked = readOnly || saving || !!splitPreview;

  useEffect(() => {
    setAnnotation(detail.annotation);
    setSavedAnnotation(detail.annotation);
    setRevisions(detail.annotation_revisions);
    setHistory([]);
    setFuture([]);
    setSelectedId(null);
    setFilteredIds(null);
    setFocusRange(null);
    focusNonce.current = 0;
    setConflict(null);
    setSplitPreview(null);
    setPendingBounds(false);
    setRailOpen(false);
    setCommandDialog(null);
    typing.current = false;
    saver.reset();
  }, [detail.id, detail.annotation.version]);

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  function leave(action: () => void) {
    if (saving) return;
    if (!dirty) {
      action();
      return;
    }
    setPendingNavigation(() => action);
  }

  useEffect(() => {
    registerNavigationGuard(leave);
    return () => {
      registerNavigationGuard(null);
      onDirtyChange(false);
    };
  });

  function applyLocalEdit(next: Annotation) {
    typing.current = false;
    setHistory((values) => [...values.slice(-49), annotation]);
    setFuture([]);
    setAnnotation(next);
  }

  /** Records one local, undoable edit. */
  function edit(next: Annotation) {
    if (locked || saver.isSaving()) return;
    applyLocalEdit(next);
  }

  /**
   * Records typing as local work. Only the first keystroke of a run adds to undo history, so
   * one undo takes back the whole edit rather than one letter.
   */
  function editText(next: Annotation) {
    if (locked || saver.isSaving()) return;
    if (!typing.current) {
      typing.current = true;
      setHistory((values) => [...values.slice(-49), annotation]);
    }
    setFuture([]);
    setAnnotation(next);
  }

  /** Ends a run of typing without saving it. */
  function commitEdit() {
    typing.current = false;
  }

  function undo() {
    if (locked || saver.isSaving()) return;
    const previous = history.at(-1);
    if (!previous) return;
    typing.current = false;
    setFuture((values) => [annotation, ...values]);
    setHistory((values) => values.slice(0, -1));
    const restored = { ...previous, version: annotation.version };
    setAnnotation(restored);
  }

  function redo() {
    if (locked || saver.isSaving()) return;
    const next = future[0];
    if (!next) return;
    typing.current = false;
    setHistory((values) => [...values, annotation]);
    setFuture((values) => values.slice(1));
    const restored = { ...next, version: annotation.version };
    setAnnotation(restored);
  }

  /** Plays the exact segment range once or continuously through the shared controller. */
  function playSegment(segment: TranscriptUtterance, loop: boolean) {
    focusNonce.current += 1;
    setFocusRange({
      start_sample: segment.start_sample,
      end_sample: segment.end_sample,
      behavior: loop ? "loop" : "once",
      nonce: focusNonce.current,
    });
  }

  /** Clicking an A/B region focuses the transcript entries it intersects. */
  function focusRegion(regionId: string, atSample?: number) {
    const region = annotation.activities.find((item) => item.id === regionId);
    if (!region) return;
    const matches = intersecting(
      annotation.transcript,
      region.start_sample,
      region.end_sample,
    );
    setFilteredIds(matches.map((item) => item.id));
    // An overlap region sits on top of the dominant speaker's long segment, so
    // prefer entries belonging to the region's own speaker.
    const own = matches.filter((item) => (item.speaker || "A") === region.speaker);
    const pool = own.length ? own : matches;
    // One region routinely covers many segments, so select the one actually
    // under the click rather than whichever comes first.
    const under = atSample === undefined
      ? undefined
      : pool.find(
          (item) => item.start_sample <= atSample && atSample < item.end_sample,
        );
    const best = under || pool[0];
    if (best) setSelectedId(best.id);
  }

  /** Removes a speaker rectangle and any segment it owned, once confirmed. */
  function confirmRegionDelete(regionId: string) {
    const result = deleteActivity(annotation, regionId);
    setRegionToDelete(null);
    if (result.annotation === annotation) return;
    edit(result.annotation);
    if (result.removedSegments.some((item) => item.id === selectedId)) {
      setSelectedId(null);
    }
    setNotice(
      result.removedSegments.length
        ? `Speaker region removed, along with ${result.removedSegments.length} transcript`
          + ` segment${result.removedSegments.length === 1 ? "" : "s"}. Undo restores both.`
        : "Speaker region removed. No transcript segment belonged to it.",
    );
  }

  function addSegmentAt(startSample: number, endSample: number) {
    const speaker: Speaker = annotation.transcript.at(-1)?.speaker === "A" ? "B" : "A";
    const result = addSegment(annotation, startSample, endSample, speaker);
    edit(result.annotation);
    setSelectedId(result.id);
    setNotice("Segment added. Type its text in the inspector.");
  }

  function addSegmentAtPlayhead() {
    const start = Math.max(0, playhead);
    const limit = detail.duration_samples || start + 2 * 24_000;
    const end = Math.min(limit, start + 2 * 24_000);
    if (end <= start) {
      setError("Move the playhead before the end of the source to add a segment.");
      return;
    }
    addSegmentAt(start, end);
  }

  function splitAt(id: string, atSample: number, textOffset: number) {
    if (locked) return;
    const proposal = prepareSplit(annotation, id, atSample, textOffset);
    if ("reason" in proposal) {
      setError(proposal.reason);
      return;
    }
    setSplitPreview(proposal);
  }

  function confirmSplit() {
    if (!splitPreview || saving) return;
    const result = splitSegment(
      annotation,
      splitPreview.id,
      splitPreview.atSample,
      undefined,
      [splitPreview.firstText, splitPreview.secondText],
    );
    if (!result.ok) {
      setError(result.reason);
      return;
    }
    setSplitPreview(null);
    applyLocalEdit(result.annotation);
    setSelectedId(result.ids[0]);
    const at = (result.atSample / 24_000).toFixed(2);
    setNotice(
      result.snappedFrom === null
        ? `Segment split at ${at}s.`
        : `Segment split at ${at}s, moved from ${(result.snappedFrom / 24_000).toFixed(2)}s`
          + " to keep the spoken word whole.",
    );
  }

  /** Gives the quieter speaker their own segment over each overlapped stretch. */
  function addOverlap(id: string) {
    const result = addOverlapSegments(annotation, id);
    if (!result.ids.length) {
      setError("The other speaker already has a segment over every overlap here.");
      return;
    }
    edit(result.annotation);
    setSelectedId(result.ids[0]);
    setNotice(
      result.ids.length === 1
        ? "Overlap segment added. The original segment is unchanged."
        : `${result.ids.length} overlap segments added. The original segments are`
          + " unchanged.",
    );
  }

  function addAllOverlaps() {
    const result = addAllOverlapSegments(annotation);
    if (!result.ids.length) {
      setError("Every overlap on the timeline already has a segment.");
      return;
    }
    edit(result.annotation);
    setSelectedId(result.ids[0]);
    setNotice(
      `${result.ids.length} overlap segment${result.ids.length === 1 ? "" : "s"} added.`
      + " The original segments are unchanged.",
    );
  }

  function joinWith(firstId: string, secondId: string) {
    const result = joinSegments(annotation, firstId, secondId);
    if (!result.ok) {
      setError(result.reason);
      return;
    }
    edit(result.annotation);
    setSelectedId(result.id);
    setNotice(
      result.absorbedSpeaker
        ? `Segments joined. The merged segment keeps speaker ${
          result.annotation.transcript.find((item) => item.id === result.id)?.speaker
        } and now holds speaker ${result.absorbedSpeaker}'s text too.`
        : "Segments joined.",
    );
  }

  function removeSegment(id: string) {
    edit(deleteSegment(annotation, id));
    setSelectedId(null);
    setNotice("Segment deleted. Undo restores it.");
  }

  async function saveNow() {
    if (readOnly || saving || splitPreview || saver.isSaving()) return;
    const prepared = transcriptPanel.current?.prepareForSave() ?? annotation;
    if (!prepared) {
      setError("Fix the highlighted timing fields before saving.");
      return;
    }
    let snapshot = prepared;
    if (prepared !== annotation) {
      applyLocalEdit(prepared);
      snapshot = prepared;
    }
    if (annotationContent(snapshot) === annotationContent(savedAnnotation)) return;
    const saved = await saver.save(snapshot);
    if (!saved) return;
    setAnnotation(saved);
    setSavedAnnotation(saved);
    setPendingBounds(false);
    setRevisions((values) => [
      { version: saved.version, created_at: new Date().toISOString() },
      ...values.filter((value) => value.version !== saved.version),
    ]);
    setNotice(`Annotation revision ${saved.version} saved`);
  }

  async function deleteSource() {
    if (locked || saver.isSaving()) return;
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
    if (locked || saver.isSaving()) return;
    try {
      const previous = await api<Annotation>(
        `/api/sources/${detail.id}/annotations/${version}`,
      );
      edit({ ...previous, version: annotation.version });
      setNotice(`Revision ${version} loaded as unsaved changes. Click Save to keep it.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  /**
   * Starts the one allowed preparation pass. Reachable only before a source has
   * succeeded, so there is no path from this screen to a second pass.
   */
  async function startInitialization(mode: "assisted" | "manual") {
    try {
      const job = await api<Job>(
        `/api/sources/${detail.id}/initialize`,
        jsonRequest("POST", { mode }),
      );
      onJob(job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  const failedInit = project.jobs.find(
    (job) =>
      job.kind === "initialize" && job.source_id === detail.id && job.status === "failed",
  );

  const visibleSegments = chronological(annotation.transcript).filter(
    (segment) => !filteredIds || filteredIds.includes(segment.id),
  );
  const selectedPosition = visibleSegments.findIndex((segment) => segment.id === selectedId);
  const selectedSegment = selectedPosition >= 0 ? visibleSegments[selectedPosition] : null;
  const previousSegment = selectedPosition > 0
    ? visibleSegments[selectedPosition - 1]
    : selectedPosition < 0
      ? visibleSegments.at(-1) || null
      : null;
  const nextSegment =
    selectedPosition >= 0 && selectedPosition + 1 < visibleSegments.length
      ? visibleSegments[selectedPosition + 1]
      : selectedPosition < 0
        ? visibleSegments[0] || null
        : null;

  function selectAndPlay(segment: TranscriptUtterance | null) {
    if (!segment) return;
    commitEdit();
    setSelectedId(segment.id);
    playSegment(segment, false);
  }

  const commandRuns: Record<(typeof REVIEW_COMMANDS)[number]["id"], () => void> = {
    save: () => void saveNow(),
    undo,
    redo,
    toggle_playback: () => waveformEditor.current?.togglePlayback(),
    seek_back: () => waveformEditor.current?.seekBySeconds(-1),
    seek_forward: () => waveformEditor.current?.seekBySeconds(1),
    frame_back: () => waveformEditor.current?.seekBySeconds(-1 / (detail.inspection?.video_frame_rate || 25)),
    frame_forward: () => waveformEditor.current?.seekBySeconds(1 / (detail.inspection?.video_frame_rate || 25)),
    previous_segment: () => selectAndPlay(previousSegment),
    next_segment: () => selectAndPlay(nextSegment),
    play_segment: () => selectedSegment && playSegment(selectedSegment, false),
    loop_segment: () => selectedSegment && playSegment(selectedSegment, true),
    add_segment: addSegmentAtPlayhead,
    selection_start: () => waveformEditor.current?.beginSelection(),
    selection_speaker_a: () => waveformEditor.current?.finishActivity("A"),
    selection_speaker_b: () => waveformEditor.current?.finishActivity("B"),
    open_palette: () => setCommandDialog("palette"),
    open_shortcuts: () => setCommandDialog("help"),
  };
  const commandEnabled: Record<(typeof REVIEW_COMMANDS)[number]["id"], boolean> = {
    save: dirty && !locked,
    undo: !!history.length && !locked,
    redo: !!future.length && !locked,
    toggle_playback: true,
    seek_back: true,
    seek_forward: true,
    frame_back: true,
    frame_forward: true,
    previous_segment: !!previousSegment,
    next_segment: !!nextSegment,
    play_segment: !!selectedSegment,
    loop_segment: !!selectedSegment,
    add_segment: !locked,
    selection_start: !locked,
    selection_speaker_a: !locked,
    selection_speaker_b: !locked,
    open_palette: true,
    open_shortcuts: true,
  };
  const commands: ReviewCommand[] = REVIEW_COMMANDS.map((definition) => ({
    ...definition,
    enabled: commandEnabled[definition.id],
    run: commandRuns[definition.id],
  }));

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const id = commandIdForKeyboardEvent(event);
      if (!id) return;
      const command = commands.find((candidate) => candidate.id === id);
      if (!command) return;
      event.preventDefault();
      if (command.enabled) command.run();
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [commands]);

  if (detail.status === "uploaded" || detail.status === "failed") {
    return (
      <section className="page">
        <button className="back" onClick={onBack}>← {project.project.name}</button>
        <div className="onboarding card">
          <span className="eyebrow">New source</span>
          <h1>{detail.original_name}</h1>
          {failedInit ? (
            <>
              <p className="danger-text">
                Preparation did not finish: {failedInit.error || failedInit.message}
              </p>
              <p>Retrying reuses this same source and its original media.</p>
              <div className="choice-grid">
                <button onClick={() => startInitialization("assisted")}>
                  <strong>Retry assisted preparation</strong>
                  <span>Speaker timestamps, draft transcript, word timing, and quality flags.</span>
                </button>
                <button onClick={() => startInitialization("manual")}>
                  <strong>Retry manual preparation</strong>
                  <span>Waveform and video proxy only, with no draft transcript.</span>
                </button>
              </div>
            </>
          ) : (
            <>
              <p>
                Choose once how this source is prepared. Assisted runs a single pass that
                returns speaker timestamps, a complete draft transcript, word timing, and
                quality flags. It cannot be repeated after it succeeds.
              </p>
              <div className="choice-grid">
                <button onClick={() => startInitialization("assisted")}>
                  <strong>Assisted start</strong>
                  <span>Speaker timestamps, draft transcript, word timing, and quality flags.</span>
                </button>
                <button onClick={() => startInitialization("manual")}>
                  <strong>Manual start</strong>
                  <span>Waveform and video proxy only. You write the transcript yourself.</span>
                </button>
              </div>
            </>
          )}
        </div>
      </section>
    );
  }

  const saveLabel =
    saving
      ? "Saving…"
      : saver.status === "failed"
        ? "Save failed — changes unsaved"
        : dirty
          ? "Unsaved changes"
          : `Saved · v${savedAnnotation.version}`;
  const saveTone = saving ? "saving" : saver.status === "failed" ? "failed" : dirty ? "dirty" : "saved";

  return (
    <section className="studio-page">
      <ReviewToolRail
        open={railOpen}
        projectName={project.project.name}
        sourceName={detail.original_name}
        durationSamples={detail.duration_samples || 0}
        savedVersion={savedAnnotation.version}
        segmentCount={annotation.transcript.length}
        saveLabel={saveLabel}
        saveTone={saveTone}
        locked={locked}
        dirty={dirty}
        saving={saving}
        canUndo={!!history.length}
        canRedo={!!future.length}
        revisions={revisions}
        onClose={() => setRailOpen(false)}
        onBack={() => void leave(onBack)}
        onUndo={undo}
        onRedo={redo}
        onSave={() => void saveNow()}
        onRestore={(version) => void restoreRevision(version)}
        onDelete={() => void deleteSource()}
        onOpenPalette={() => setCommandDialog("palette")}
        onOpenHelp={() => setCommandDialog("help")}
        onPause={() => waveformEditor.current?.pausePlayback()}
        inspectorRef={setInspectorTarget}
        timelineToolsRef={setTimelineToolsTarget}
      />
      <div className="studio-main">
        <button type="button" className="review-tools-toggle" onClick={() => setRailOpen(true)}>
          Tools &amp; selected segment
        </button>
        {processing && (
          <div className="inline-banner" role="status">
            Preparing this source. Editing unlocks when the result is committed.
          </div>
        )}

        <ReviewPlayerCard processing={processing}>
          <WaveformEditor
            ref={waveformEditor}
            audioUrl={detail.urls.canonical_audio}
            videoUrl={detail.urls.video_proxy}
            annotation={annotation}
            durationSamples={detail.duration_samples || 0}
            frameRate={detail.inspection?.video_frame_rate || 25}
            readOnly={locked}
            focusRange={focusRange}
            toolTarget={timelineToolsTarget}
            onTimeChange={setPlayhead}
            onRegionClick={focusRegion}
            onRegionDelete={setRegionToDelete}
            onChange={edit}
          />
        </ReviewPlayerCard>

        <section className="review-segments-card card" aria-label="Transcript segments">
          <TranscriptPanel
            ref={transcriptPanel}
            annotation={annotation}
            durationSamples={detail.duration_samples || 0}
            selectedId={selectedId}
            filteredIds={filteredIds}
            playheadSample={playhead}
            readOnly={locked}
            onSelect={(id) => {
              // Moving to another segment finishes the edit in the box.
              commitEdit();
              setSelectedId(id);
            }}
            onPlay={playSegment}
            onChange={edit}
            onChangeText={editText}
            onCommitText={commitEdit}
            onSplit={splitAt}
            onAddOverlap={addOverlap}
            onAddAllOverlaps={addAllOverlaps}
            onJoin={joinWith}
            onDelete={removeSegment}
            onAdd={addSegmentAtPlayhead}
            onClearFilter={() => setFilteredIds(null)}
            onPendingBoundsChange={setPendingBounds}
            inspectorTarget={inspectorTarget}
          />
        </section>
      </div>

      <CommandPalette
        open={commandDialog !== null}
        mode={commandDialog || "palette"}
        commands={commands}
        onClose={() => setCommandDialog(null)}
      />

      {regionToDelete && (
        <DeleteRegionDialog
          annotation={annotation}
          regionId={regionToDelete}
          onCancel={() => setRegionToDelete(null)}
          onConfirm={() => confirmRegionDelete(regionToDelete)}
        />
      )}
      {conflict && (
        <ConflictDialog
          conflict={conflict}
          onKeepLocal={() => {
            // Rebase the user's content onto the newer server version so the next
            // save is accepted without discarding either side.
            const rebased = { ...conflict.local, version: conflict.server.version };
            setConflict(null);
            setSavedAnnotation(conflict.server);
            setAnnotation(rebased);
            saver.reset();
            setNotice("Your edits are still unsaved. Click Save to replace the latest revision.");
          }}
          onTakeServer={() => {
            setConflict(null);
            setAnnotation(conflict.server);
            setSavedAnnotation(conflict.server);
            setHistory([]);
            setFuture([]);
            setPendingBounds(false);
            setRevisions((values) => [
              { version: conflict.server.version, created_at: new Date().toISOString() },
              ...values.filter((value) => value.version !== conflict.server.version),
            ]);
            saver.reset();
            setNotice("The server revision was loaded.");
          }}
        />
      )}
      {splitPreview && (
        <SplitPreviewDialog
          preview={splitPreview}
          segment={annotation.transcript.find((item) => item.id === splitPreview.id) || null}
          onChange={(next) => setSplitPreview(next)}
          onCancel={() => setSplitPreview(null)}
          onConfirm={confirmSplit}
        />
      )}
      {pendingNavigation && (
        <UnsavedChangesDialog
          onStay={() => setPendingNavigation(null)}
          onDiscard={() => {
            const action = pendingNavigation;
            setPendingNavigation(null);
            action();
          }}
        />
      )}
    </section>
  );
}

function SplitPreviewDialog({
  preview,
  segment,
  onChange,
  onCancel,
  onConfirm,
}: {
  preview: PendingSplit;
  segment: TranscriptUtterance | null;
  onChange: (preview: PendingSplit) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  useEscapeToClose(onCancel);
  if (!segment) return null;
  const textChanged = preview.firstText + preview.secondText !== preview.originalText;
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal card split-preview" role="dialog" aria-modal="true" aria-labelledby="split-preview-title">
        <h2 id="split-preview-title">Preview transcript split</h2>
        <p>
          Speaker {segment.speaker || "A"} will be divided at {seconds(preview.atSample)}s.
          {preview.snappedFrom !== null && (
            <> The requested point was moved from {seconds(preview.snappedFrom)}s to keep a spoken word whole.</>
          )}
        </p>
        <div className="split-original">
          <strong>Current transcript text</strong>
          <p dir="auto">{preview.originalText || <em>(empty)</em>}</p>
        </div>
        <div className="split-preview-fields">
          <label>
            {seconds(segment.start_sample)}–{seconds(preview.atSample)}s
            <textarea
              dir="auto"
              rows={4}
              value={preview.firstText}
              onChange={(event) => onChange({ ...preview, firstText: event.target.value })}
            />
          </label>
          <label>
            {seconds(preview.atSample)}–{seconds(segment.end_sample)}s
            <textarea
              dir="auto"
              rows={4}
              value={preview.secondText}
              onChange={(event) => onChange({ ...preview, secondText: event.target.value })}
            />
          </label>
        </div>
        <p className={textChanged ? "split-text-warning" : "inspector-note"} role="status">
          {textChanged
            ? "The two fields no longer reproduce the original text. Confirm only if this edit is intentional."
            : "Every character from the current transcript is preserved across the two fields."}
        </p>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" className="primary" onClick={onConfirm}>Confirm split</button>
        </div>
      </div>
    </div>
  );
}

function UnsavedChangesDialog({
  onStay,
  onDiscard,
}: {
  onStay: () => void;
  onDiscard: () => void;
}) {
  useEscapeToClose(onStay);
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal card" role="dialog" aria-modal="true" aria-labelledby="unsaved-title">
        <h2 id="unsaved-title">Discard unsaved changes?</h2>
        <p>The server still has the last saved revision. Leaving now discards the changes on this screen.</p>
        <div className="modal-actions">
          <button type="button" className="primary" onClick={onStay}>Stay</button>
          <button type="button" className="danger" onClick={onDiscard}>Discard changes</button>
        </div>
      </div>
    </div>
  );
}

/**
 * Confirms removing a speaker rectangle, naming the transcript segments that go
 * with it. A double-click used to delete silently, which was easy to do by
 * accident and hard to notice.
 */
function DeleteRegionDialog({
  annotation,
  regionId,
  onCancel,
  onConfirm,
}: {
  annotation: Annotation;
  regionId: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  useEscapeToClose(onCancel);
  const region = annotation.activities.find((item) => item.id === regionId);
  const owned = region ? segmentsForActivity(annotation.transcript, region) : [];
  if (!region) return null;

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-region-title"
      >
        <h2 id="delete-region-title">
          Remove speaker {region.speaker} from {seconds(region.start_sample)}–
          {seconds(region.end_sample)}s?
        </h2>
        {owned.length ? (
          <>
            <p>
              This region has {owned.length === 1 ? "its own transcript segment" : `${owned.length} transcript segments`},
              {" "}which {owned.length === 1 ? "is" : "are"} removed with it.
            </p>
            <ul className="region-delete-list">
              {owned.map((segment) => (
                <li key={segment.id} dir="auto">
                  <span className="region-delete-time">
                    {seconds(segment.start_sample)}–{seconds(segment.end_sample)}s
                  </span>
                  {segment.text.trim() || <em>(empty)</em>}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p>
            No transcript segment belongs to this region on its own, so only the
            region on the timeline is removed. A segment covering this region and
            others stays as it is.
          </p>
        )}
        <p className="inspector-note">
          Nothing is deleted on the server until the next save, and Undo restores it.
        </p>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" className="danger" onClick={onConfirm}>
            {owned.length ? "Remove region and segments" : "Remove region"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ConflictDialog({
  conflict,
  onKeepLocal,
  onTakeServer,
}: {
  conflict: Conflict;
  onKeepLocal: () => void;
  onTakeServer: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal card" role="dialog" aria-modal="true" aria-labelledby="conflict-title">
        <h2 id="conflict-title">This annotation changed elsewhere</h2>
        <p>
          Your edits were based on revision {conflict.local.version}, but the server is now at
          revision {conflict.server.version}. Nothing was overwritten and no revision was
          created.
        </p>
        <dl className="transfer-summary">
          <div>
            <dt>Your copy</dt>
            <dd>{conflict.local.transcript.length} segments</dd>
          </div>
          <div>
            <dt>Server copy</dt>
            <dd>{conflict.server.transcript.length} segments</dd>
          </div>
        </dl>
        <div className="modal-actions">
          <button type="button" onClick={onTakeServer}>Load server revision</button>
          <button type="button" className="primary" onClick={onKeepLocal}>
            Keep my edits
          </button>
        </div>
      </div>
    </div>
  );
}

export default App;
