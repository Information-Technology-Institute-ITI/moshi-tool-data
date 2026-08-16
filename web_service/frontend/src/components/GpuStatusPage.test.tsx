// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GpuStatusPage, {
  displayGpuState,
  gpuPollIntervalMs,
} from "./GpuStatusPage";
import type {
  GpuCheck,
  GpuSystemStatus,
  GpuTopLevelState,
} from "../types";

const allStates: GpuTopLevelState[] = [
  "OFF",
  "STARTING",
  "CHECKING",
  "READY",
  "BUSY",
  "DEGRADED",
  "INCOMPATIBLE",
  "BLOCKED",
  "ERROR",
  "BLOCKED/ERROR",
  "STOPPING",
  "UNKNOWN",
];

function makeCheck(overrides: Partial<GpuCheck> = {}): GpuCheck {
  return {
    id: "check-1",
    gpu_check_id: "gpu-check-1",
    instance_id: "i-gpu",
    trigger: "manual",
    requested_by: "operator",
    status: "passed",
    requirement_key: "requirement-1",
    host_boot_id: "host-boot",
    service_boot_id: "service-boot",
    dispatch_protocol: "2.0",
    worker_protocol: "1.0",
    actual_build_id: "gpu-build-test",
    expected_build_id: "gpu-build-test",
    model_revision: "model-revision-123456",
    config_fingerprint: "config-fingerprint-123456",
    fixture_id: "fixture-ar-v1",
    fixture_hash_prefix: "abcdef123456",
    requested_at: "2026-08-16T18:00:00Z",
    started_at: "2026-08-16T18:01:00Z",
    finished_at: "2026-08-16T18:02:00Z",
    valid_until: "2026-08-17T00:02:00Z",
    updated_at: "2026-08-16T18:02:00Z",
    gpu_name: "Tesla T4",
    device: "cuda",
    segment_count: 2,
    cer: 0.08,
    cer_threshold: 0.2,
    model_load_ms: 1_250,
    inference_ms: 800,
    total_ms: 2_050,
    failure_class: null,
    failure_summary: null,
    ...overrides,
  };
}

function makeStatus({
  state = "READY",
  instanceState = "running",
  serviceState = "online",
  runningCount = 0,
  check = makeCheck(),
}: {
  state?: GpuTopLevelState;
  instanceState?: GpuSystemStatus["machine"]["instance_state"];
  serviceState?: GpuSystemStatus["service"]["state"];
  runningCount?: number;
  check?: GpuCheck | null;
} = {}): GpuSystemStatus {
  return {
    state,
    machine: {
      instance_id: "i-gpu",
      instance_state: instanceState,
      desired_state: "running",
      last_aws_observation: "2026-08-16T18:03:00Z",
      observation_age_seconds: 4,
      last_transition_at: "2026-08-16T17:58:00Z",
      last_error: null,
      idle_stop_at: "2026-08-16T18:20:00Z",
    },
    service: {
      state: serviceState,
      last_intake_observation: "2026-08-16T18:03:00Z",
      observation_age_seconds: 3,
      last_worker_heartbeat: "2026-08-16T18:03:00Z",
      worker_age_seconds: 2,
      current_job_id: runningCount ? "job-1" : null,
      gpu_name: "Tesla T4",
      dispatch_protocol_version: "2.0",
      expected_dispatch_protocol_version: "2.0",
      worker_protocol_version: "1.0",
      expected_worker_protocol_version: "1.0",
      build_id: "gpu-build-test",
      expected_build_id: "gpu-build-test",
      queue_count: 0,
      running_count: runningCount,
      accepting_dispatches: runningCount === 0,
      callback_ready: true,
      operational_ready: runningCount === 0,
    },
    functional_check: check,
    dispatcher: {
      state: runningCount ? "running" : "idle",
      active_dispatch_id: runningCount ? "dispatch-1" : null,
      last_error: null,
    },
  };
}

function response(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  const normalized = new Map(
    Object.entries(headers).map(([key, value]) => [key.toLocaleLowerCase(), value]),
  );
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 429 ? "Too Many Requests" : status >= 400 ? "Request failed" : "OK",
    headers: {
      get: (name: string) => normalized.get(name.toLocaleLowerCase()) || null,
    },
    json: async () => body,
  } as Response;
}

function installFetch(
  readStatus: () => GpuSystemStatus,
  readChecks: () => GpuCheck[] = () => [makeCheck()],
  trigger: () => Response = () => response({
    check: makeCheck({ id: "manual-check" }),
    created: true,
    cost_notice: "The GPU may start and incur compute cost.",
  }, 202),
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input);
    if (url === "/api/system/gpu") return response(readStatus());
    if (url === "/api/system/gpu/checks?limit=10") {
      return response({ checks: readChecks() });
    }
    if (url === "/api/system/gpu/checks" && options?.method === "POST") return trigger();
    return response({ detail: "Unexpected browser request" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

let container: HTMLDivElement;
let root: Root;

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function renderPage() {
  await act(async () => {
    root.render(<GpuStatusPage />);
  });
  await flush();
}

function manualButton(): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll("button")).find((item) =>
    item.textContent?.includes("functional check")
  );
  if (!(button instanceof HTMLButtonElement)) throw new Error("Manual-check button not found");
  return button;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("GPU state derivation and polling", () => {
  it("covers every top-level state and forces a stopped machine to OFF", () => {
    for (const state of allStates) {
      expect(displayGpuState(makeStatus({ state }))).toBe(state);
    }
    expect(displayGpuState(makeStatus({ state: "READY", instanceState: "stopped" }))).toBe("OFF");

    for (const state of ["STARTING", "CHECKING", "BUSY", "STOPPING", "UNKNOWN"] as const) {
      expect(gpuPollIntervalMs(state)).toBe(5_000);
    }
    for (const state of allStates.filter(
      (value) => !["STARTING", "CHECKING", "BUSY", "STOPPING", "UNKNOWN"].includes(value),
    )) {
      expect(gpuPollIntervalMs(state)).toBe(30_000);
    }
  });

  it("polls at 5 seconds while transitional and 30 seconds once stable", async () => {
    vi.useFakeTimers();
    let snapshot = makeStatus({ state: "STARTING", serviceState: "starting", check: null });
    const fetchMock = installFetch(() => snapshot, () => []);

    await renderPage();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(4_999));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    snapshot = makeStatus();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(4);

    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(fetchMock).toHaveBeenCalledTimes(4);

    await act(async () => vi.advanceTimersByTimeAsync(1));
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });
});

describe("GPU status page", () => {
  it("renders the four shared sections and treats a stopped pass as history", async () => {
    const passed = makeCheck();
    installFetch(
      () => makeStatus({ state: "READY", instanceState: "stopped", check: passed }),
      () => [passed],
    );

    await renderPage();

    expect(container.querySelector('[data-testid="gpu-overall-state"]')?.textContent).toBe("OFF");
    expect(container.textContent).toContain("Machine");
    expect(container.textContent).toContain("GPU service");
    expect(container.textContent).toContain("Functional check");
    expect(container.textContent).toContain("Functional-check history");
    expect(container.textContent).toContain("fixture-ar-v1");
    expect(container.textContent).toContain("previous pass is historical only");
    expect(container.textContent).toContain("Starting a stopped GPU incurs compute cost");
    const metricLabels = Array.from(container.querySelectorAll(".gpu-metric small"))
      .map((item) => item.textContent);
    expect(metricLabels.filter((value) => value === "Callback")).toHaveLength(1);
    expect(metricLabels.filter((value) => value === "Dispatch intake")).toHaveLength(1);
  });

  it("renders history and reports a deduplicated manual request without browser identity", async () => {
    const existing = makeCheck({ id: "existing-check" });
    const fetchMock = installFetch(
      () => makeStatus({ check: existing }),
      () => [existing],
      () => response({
        check: existing,
        created: false,
        cost_notice: "No additional cold start was requested.",
      }),
    );
    await renderPage();

    await act(async () => {
      manualButton().click();
    });
    await flush();

    expect(container.textContent).toContain("An existing functional check was reused.");
    const postCall = fetchMock.mock.calls.find(
      ([url, options]) => String(url) === "/api/system/gpu/checks" && options?.method === "POST",
    );
    expect(postCall).toBeDefined();
    expect(postCall?.[1]?.body).toBeUndefined();
    expect(JSON.stringify(postCall?.[1]?.headers)).not.toMatch(/user|identity/i);
    expect(fetchMock.mock.calls.every(([url]) => String(url).startsWith("/api/system/gpu"))).toBe(true);
  });

  it("disables manual checks while a real job is running", async () => {
    installFetch(() => makeStatus({ state: "BUSY", serviceState: "busy", runningCount: 1 }));
    await renderPage();

    expect(manualButton().disabled).toBe(true);
    expect(container.textContent).toContain("A processing job is running");
  });

  it("shows server errors and honors manual Retry-After", async () => {
    const fetchMock = installFetch(
      () => makeStatus(),
      undefined,
      () => response(
        { detail: "Manual functional-check cooldown is active." },
        429,
        { "Retry-After": "120" },
      ),
    );
    await renderPage();

    await act(async () => {
      manualButton().click();
    });
    await flush();

    expect(container.textContent).toContain("Manual functional-check cooldown is active.");
    expect(container.textContent).toContain("2m 00s remaining.");
    expect(container.textContent).toContain("Rejected requests do not extend this deadline.");
    expect(manualButton().disabled).toBe(true);
    expect(fetchMock).toHaveBeenCalled();
  });

  it("shows a bounded status error when the m8i API is unavailable", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/system/gpu") {
        return response({ detail: "GPU observations are temporarily unavailable." }, 503);
      }
      return response({ checks: [] });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderPage();

    expect(container.textContent).toContain("GPU status is unavailable.");
    expect(container.textContent).toContain("GPU observations are temporarily unavailable.");
  });
});
