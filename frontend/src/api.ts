import type {
  AdminUser,
  AuthUser,
  GpuCheckHistory,
  GpuCheckTriggerResult,
  GpuSystemStatus,
  Project,
  ProjectDeletionResult,
} from "./types";

export class ApiError extends Error {
  status: number;
  retryAfterSeconds: number | null;

  constructor(status: number, message: string, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function retryAfterSeconds(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return null;
  return Math.max(0, Math.ceil((timestamp - Date.now()) / 1000));
}

export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(
      response.status,
      body.detail || response.statusText,
      retryAfterSeconds(response.headers.get("Retry-After")),
    );
  }
  return body as T;
}

export function jsonRequest(method: string, value?: unknown): RequestInit {
  return {
    method,
    headers: { "content-type": "application/json" },
    body: value === undefined ? undefined : JSON.stringify(value),
  };
}

export function getCurrentUser(): Promise<{
  user: AuthUser | null;
  required: boolean;
}> {
  return api<{ user: AuthUser | null; required: boolean }>("/api/auth/me", {
    credentials: "same-origin",
  });
}

export function signup(value: {
  display_name: string;
  email: string;
  password: string;
}): Promise<{ message: string }> {
  return api<{ message: string }>("/api/auth/signup", {
    ...jsonRequest("POST", value),
    credentials: "same-origin",
  });
}

export function resendActivation(email: string): Promise<{ message: string }> {
  return api<{ message: string }>("/api/auth/resend-activation", {
    ...jsonRequest("POST", { email }),
    credentials: "same-origin",
  });
}

export function activateAccount(token: string): Promise<{ user: AuthUser }> {
  return api<{ user: AuthUser }>("/api/auth/activate", {
    ...jsonRequest("POST", { token }),
    credentials: "same-origin",
  });
}

export function signin(
  email: string,
  password: string,
): Promise<{ user: AuthUser }> {
  return api<{ user: AuthUser }>("/api/auth/signin", {
    ...jsonRequest("POST", { email, password }),
    credentials: "same-origin",
  });
}

export function signout(): Promise<{ signed_out: boolean }> {
  return api<{ signed_out: boolean }>("/api/auth/signout", {
    ...jsonRequest("POST"),
    credentials: "same-origin",
  });
}

export function listProjects(): Promise<{ projects: Project[] }> {
  return api<{ projects: Project[] }>("/api/projects", {
    credentials: "same-origin",
  });
}

/** Administrator-only. Used to populate the ownership transfer selector. */
export function listAdminUsers(): Promise<{ users: AdminUser[] }> {
  return api<{ users: AdminUser[] }>("/api/admin/users", {
    credentials: "same-origin",
  });
}

/** Administrator-only ownership transfer. The server rejects inactive owners. */
export function transferProjectOwner(
  projectId: string,
  ownerUserId: string,
): Promise<Project> {
  return api<Project>(`/api/admin/projects/${projectId}/owner`, {
    ...jsonRequest("PATCH", { owner_user_id: ownerUserId }),
    credentials: "same-origin",
  });
}

/**
 * Permanently deletes a dataset. The server requires X-Confirm-Delete to equal
 * the project id exactly, so the caller confirms intent before reaching here.
 */
export function deleteProject(projectId: string): Promise<ProjectDeletionResult> {
  return api<ProjectDeletionResult>(`/api/projects/${projectId}`, {
    method: "DELETE",
    headers: { "x-confirm-delete": projectId },
    credentials: "same-origin",
  });
}

export function getGpuStatus(signal?: AbortSignal): Promise<GpuSystemStatus> {
  return api<GpuSystemStatus>("/api/system/gpu", {
    credentials: "same-origin",
    signal,
  });
}

export function getGpuChecks(
  limit = 10,
  signal?: AbortSignal,
): Promise<GpuCheckHistory> {
  return api<GpuCheckHistory>(`/api/system/gpu/checks?limit=${limit}`, {
    credentials: "same-origin",
    signal,
  });
}

export function triggerGpuCheck(signal?: AbortSignal): Promise<GpuCheckTriggerResult> {
  return api<GpuCheckTriggerResult>("/api/system/gpu/checks", {
    ...jsonRequest("POST"),
    credentials: "same-origin",
    signal,
  });
}

export function seconds(samples: number): string {
  return (samples / 24_000).toFixed(2);
}

export function sampleId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

export function watchJob(
  jobId: string,
  onUpdate: (job: import("./types").Job) => void,
): () => void {
  const events = new EventSource(`/api/jobs/${jobId}/events`);
  events.onmessage = (event) => {
    const job = JSON.parse(event.data);
    onUpdate(job);
    if (job.status === "complete" || job.status === "failed") {
      events.close();
    }
  };
  events.onerror = () => events.close();
  return () => events.close();
}
