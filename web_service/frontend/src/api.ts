export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, body.detail || response.statusText);
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
