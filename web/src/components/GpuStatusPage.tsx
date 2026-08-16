import { useEffect, useState } from "react";
import {
  ApiError,
  getGpuChecks,
  getGpuStatus,
  triggerGpuCheck,
} from "../api";
import type {
  GpuCheck,
  GpuCheckStatus,
  GpuSystemStatus,
  GpuTopLevelState,
} from "../types";

const TRANSITIONAL_STATES = new Set<GpuTopLevelState>([
  "STARTING",
  "CHECKING",
  "BUSY",
  "STOPPING",
  "UNKNOWN",
]);

const ACTIVE_CHECK_STATES = new Set<GpuCheckStatus>([
  "requested",
  "queued",
  "starting",
  "waiting",
  "running",
]);

export function displayGpuState(status: GpuSystemStatus): GpuTopLevelState {
  return status.machine.instance_state === "stopped" ? "OFF" : status.state;
}

export function gpuPollIntervalMs(state: GpuTopLevelState): number {
  return TRANSITIONAL_STATES.has(state) ? 5_000 : 30_000;
}

function stateClass(value: string): string {
  return value.toLocaleLowerCase().replaceAll(/[^a-z0-9]+/g, "-").replaceAll(/^-|-$/g, "");
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return "Never";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3_600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3_600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "Never";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Unknown" : timestamp.toLocaleString();
}

function shortIdentifier(value: string | null): string {
  if (!value) return "Unknown";
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}

function formatMilliseconds(value: number | null): string {
  if (value === null) return "—";
  if (value < 1_000) return `${Math.round(value)} ms`;
  return `${(value / 1_000).toFixed(1)} s`;
}

function formatRetryAfter(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return minutes > 0
    ? `${minutes}m ${remainingSeconds.toString().padStart(2, "0")}s`
    : `${remainingSeconds}s`;
}

function safeError(reason: unknown, fallback: string): string {
  return reason instanceof ApiError && reason.message ? reason.message : fallback;
}

function isAbort(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

function Metric({
  label,
  value,
  title,
}: {
  label: string;
  value: React.ReactNode;
  title?: string;
}) {
  return (
    <div className="gpu-metric">
      <small>{label}</small>
      <strong title={title}>{value}</strong>
    </div>
  );
}

function StatePill({ value }: { value: string }) {
  return <span className={`gpu-state-pill state-${stateClass(value)}`}>{value.replaceAll("_", " ")}</span>;
}

function CheckDetails({ check }: { check: GpuCheck }) {
  const cer = check.cer === null
    ? "—"
    : `${check.cer.toFixed(3)}${check.cer_threshold === null ? "" : ` / ≤ ${check.cer_threshold.toFixed(3)}`}`;

  return (
    <>
      <div className="gpu-card-heading">
        <div>
          <span className="eyebrow">Current boot and configuration</span>
          <h2>Functional check</h2>
        </div>
        <StatePill value={check.status} />
      </div>
      <div className="gpu-metric-grid">
        <Metric label="Trigger" value={check.trigger.replaceAll("_", " ")} />
        <Metric label="Requested" value={formatTimestamp(check.requested_at)} />
        <Metric label="Started" value={formatTimestamp(check.started_at)} />
        <Metric label="Finished" value={formatTimestamp(check.finished_at)} />
        <Metric label="Valid until" value={formatTimestamp(check.valid_until)} />
        <Metric
          label="Model revision"
          value={shortIdentifier(check.model_revision)}
          title={check.model_revision || undefined}
        />
        <Metric
          label="Config"
          value={shortIdentifier(check.config_fingerprint)}
          title={check.config_fingerprint || undefined}
        />
        <Metric
          label="Fixture"
          value={check.fixture_id || "Unknown"}
          title={check.fixture_hash_prefix ? `SHA-256 ${check.fixture_hash_prefix}…` : undefined}
        />
        <Metric label="CER / threshold" value={cer} />
        <Metric label="Model load" value={formatMilliseconds(check.model_load_ms)} />
        <Metric label="Inference" value={formatMilliseconds(check.inference_ms)} />
        <Metric label="Total" value={formatMilliseconds(check.total_ms)} />
        <Metric label="GPU" value={check.gpu_name || check.device || "Unknown"} />
        <Metric label="Segments" value={check.segment_count ?? "—"} />
      </div>
      {check.failure_summary && (
        <div className="gpu-inline-error" role="status">
          <strong>{check.failure_class || "Functional check failed"}</strong>
          <span>{check.failure_summary}</span>
        </div>
      )}
    </>
  );
}

export default function GpuStatusPage() {
  const [status, setStatus] = useState<GpuSystemStatus | null>(null);
  const [checks, setChecks] = useState<GpuCheck[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [triggering, setTriggering] = useState(false);
  const [retryAfterSeconds, setRetryAfterSeconds] = useState<number | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let nextState: GpuTopLevelState = "UNKNOWN";

    async function poll() {
      controller = new AbortController();
      try {
        const [nextStatus, history] = await Promise.all([
          getGpuStatus(controller.signal),
          getGpuChecks(10, controller.signal),
        ]);
        if (disposed) return;
        nextState = displayGpuState(nextStatus);
        setStatus(nextStatus);
        setChecks(history.checks);
        setLoadError("");
      } catch (reason) {
        if (disposed || isAbort(reason)) return;
        setLoadError(safeError(reason, "GPU status is temporarily unavailable."));
      } finally {
        if (!disposed) {
          setLoading(false);
          timer = window.setTimeout(() => {
            void poll();
          }, gpuPollIntervalMs(nextState));
        }
      }
    }

    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
    };
  }, [refreshVersion]);

  useEffect(() => {
    if (retryAfterSeconds === null) return;
    if (retryAfterSeconds <= 0) {
      setRetryAfterSeconds(null);
      return;
    }
    const timer = window.setTimeout(() => {
      setRetryAfterSeconds((current) => current === null ? null : current - 1);
    }, 1_000);
    return () => window.clearTimeout(timer);
  }, [retryAfterSeconds]);

  async function runManualCheck() {
    setTriggering(true);
    setActionError("");
    setActionNotice("");
    try {
      const result = await triggerGpuCheck();
      setChecks((current) => [
        result.check,
        ...current.filter((check) => check.id !== result.check.id),
      ].slice(0, 10));
      setActionNotice(
        result.created
          ? result.cost_notice
          : `An existing functional check was reused. ${result.cost_notice}`,
      );
      setRefreshVersion((value) => value + 1);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 429) {
        const retryAfter = reason.retryAfterSeconds;
        setRetryAfterSeconds(retryAfter);
        setActionError(reason.message);
      } else if (reason instanceof ApiError && reason.status === 409) {
        setActionError(`The GPU is busy and cannot run a manual check yet. ${reason.message}`);
      } else {
        setActionError(safeError(reason, "The functional check could not be requested."));
      }
    } finally {
      setTriggering(false);
    }
  }

  if (loading && !status) {
    return (
      <section className="page gpu-page" aria-live="polite">
        <div className="gpu-loading card">Loading shared GPU status…</div>
      </section>
    );
  }

  if (!status) {
    return (
      <section className="page gpu-page">
        <div className="gpu-empty-state card">
          <span className="eyebrow">Shared infrastructure</span>
          <h1>GPU status is unavailable.</h1>
          <p role="alert">{loadError || "The m8i status API did not return a snapshot."}</p>
        </div>
      </section>
    );
  }

  const overallState = displayGpuState(status);
  const check = status.functional_check;
  const checkActive = check ? ACTIVE_CHECK_STATES.has(check.status) : false;
  const realJobRunning = status.service.running_count > 0 || status.service.state === "busy";
  const rateLimited = retryAfterSeconds !== null;
  const manualDisabled = triggering || checkActive || realJobRunning || rateLimited;
  const historicalPass = overallState === "OFF" && (
    check?.status === "passed" || checks.some((item) => item.status === "passed")
  );

  return (
    <section className="page gpu-page">
      <div className="gpu-page-heading">
        <div>
          <span className="eyebrow">Shared infrastructure</span>
          <h1>GPU processing</h1>
          <p>
            This view reads durable m8i observations. Your browser never contacts EC2 or the
            private GPU intake directly.
          </p>
        </div>
        <div className={`gpu-overall-state state-${stateClass(overallState)}`}>
          <small>Current state</small>
          <strong data-testid="gpu-overall-state">{overallState}</strong>
        </div>
      </div>

      {loadError && <div className="gpu-page-alert" role="alert">{loadError}</div>}

      <div className="gpu-status-grid">
        <article className="card gpu-status-card">
          <div className="gpu-card-heading">
            <div>
              <span className="eyebrow">EC2 lifecycle</span>
              <h2>Machine</h2>
            </div>
            <StatePill value={status.machine.instance_state} />
          </div>
          <div className="gpu-metric-grid">
            <Metric label="Desired state" value={status.machine.desired_state} />
            <Metric label="Instance" value={status.machine.instance_id || "Not configured"} />
            <Metric
              label="Last AWS observation"
              value={formatAge(status.machine.observation_age_seconds)}
              title={formatTimestamp(status.machine.last_aws_observation)}
            />
            <Metric label="Last transition" value={formatTimestamp(status.machine.last_transition_at)} />
            <Metric label="Approximate idle stop" value={formatTimestamp(status.machine.idle_stop_at)} />
          </div>
          {status.machine.last_error && (
            <div className="gpu-inline-error" role="status">
              <strong>Lifecycle error</strong>
              <span>{status.machine.last_error}</span>
            </div>
          )}
        </article>

        <article className="card gpu-status-card">
          <div className="gpu-card-heading">
            <div>
              <span className="eyebrow">Private push worker</span>
              <h2>GPU service</h2>
            </div>
            <StatePill value={status.service.state} />
          </div>
          <div className="gpu-metric-grid">
            <Metric
              label="Intake observation"
              value={formatAge(status.service.observation_age_seconds)}
              title={formatTimestamp(status.service.last_intake_observation)}
            />
            <Metric
              label="Worker heartbeat"
              value={formatAge(status.service.worker_age_seconds)}
              title={formatTimestamp(status.service.last_worker_heartbeat)}
            />
            <Metric label="Current job" value={status.service.current_job_id || "None"} />
            <Metric label="GPU" value={status.service.gpu_name || "Tesla T4"} />
            <Metric
              label="Dispatch protocol"
              value={`${status.service.dispatch_protocol_version || "—"} / ${status.service.expected_dispatch_protocol_version}`}
            />
            <Metric
              label="Worker protocol"
              value={`${status.service.worker_protocol_version || "—"} / ${status.service.expected_worker_protocol_version}`}
            />
            <Metric
              label="Build"
              value={`${shortIdentifier(status.service.build_id)} / ${shortIdentifier(status.service.expected_build_id)}`}
              title={`Actual: ${status.service.build_id || "unknown"}; expected: ${status.service.expected_build_id}`}
            />
            <Metric label="Queue / running" value={`${status.service.queue_count} / ${status.service.running_count}`} />
            <Metric label="Callback" value={status.service.callback_ready ? "Ready" : "Not ready"} />
            <Metric label="Dispatch intake" value={status.service.accepting_dispatches ? "Accepting" : "Paused"} />
            <Metric label="Operational" value={status.service.operational_ready ? "Ready" : "Not ready"} />
            <Metric label="Dispatcher" value={status.dispatcher.state} />
          </div>
          {status.dispatcher.active_dispatch_id && (
            <p className="gpu-dispatch-note">
              Active dispatch <code>{shortIdentifier(status.dispatcher.active_dispatch_id)}</code>
            </p>
          )}
          {status.dispatcher.last_error && (
            <div className="gpu-inline-error" role="status">
              <strong>Dispatcher error</strong>
              <span>{status.dispatcher.last_error}</span>
            </div>
          )}
        </article>
      </div>

      <article className="card gpu-check-card">
        <div className="gpu-check-layout">
          <div>
            {check ? (
              <CheckDetails check={check} />
            ) : (
              <>
                <div className="gpu-card-heading">
                  <div>
                    <span className="eyebrow">Current boot and configuration</span>
                    <h2>Functional check</h2>
                  </div>
                  <StatePill value="never" />
                </div>
                <p>No functional check has been observed for the current GPU configuration.</p>
              </>
            )}
            {historicalPass && (
              <p className="gpu-history-warning">
                The previous pass is historical only. A stopped instance remains OFF and must pass
                the current boot and configuration check before processing.
              </p>
            )}
          </div>
          <aside className="gpu-check-action">
            <strong>Run a real WhisperX check</strong>
            <p>
              Starting a stopped GPU incurs compute cost. One check is shared by all authenticated
              users and reused while it remains valid.
            </p>
            <button
              className="primary"
              type="button"
              disabled={manualDisabled}
              onClick={() => void runManualCheck()}
            >
              {triggering ? "Requesting…" : checkActive ? "Check in progress" : "Run functional check"}
            </button>
            {realJobRunning && <small>A processing job is running; manual checks wait.</small>}
            {retryAfterSeconds !== null && (
              <small>
                Manual-check cooldown: {formatRetryAfter(retryAfterSeconds)} remaining.
                Rejected requests do not extend this deadline.
              </small>
            )}
            {actionNotice && <div className="gpu-action-notice" role="status">{actionNotice}</div>}
            {actionError && <div className="gpu-action-error" role="alert">{actionError}</div>}
          </aside>
        </div>
      </article>

      <section className="gpu-history-section" aria-labelledby="gpu-history-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Durable shared record</span>
            <h2 id="gpu-history-title">Functional-check history</h2>
          </div>
          <span>{checks.length} recent checks</span>
        </div>
        <div className="gpu-history-table card">
          {checks.length ? (
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Trigger</th>
                  <th>Requested</th>
                  <th>Build</th>
                  <th>Model / fixture</th>
                  <th>CER</th>
                  <th>Total</th>
                </tr>
              </thead>
              <tbody>
                {checks.map((item) => (
                  <tr key={item.id}>
                    <td><StatePill value={item.status} /></td>
                    <td>{item.trigger.replaceAll("_", " ")}</td>
                    <td>{formatTimestamp(item.requested_at)}</td>
                    <td title={item.actual_build_id || undefined}>{shortIdentifier(item.actual_build_id)}</td>
                    <td>
                      <strong title={item.model_revision || undefined}>{shortIdentifier(item.model_revision)}</strong>
                      <small>{item.fixture_id || "Unknown fixture"}</small>
                    </td>
                    <td>{item.cer === null ? "—" : item.cer.toFixed(3)}</td>
                    <td>{formatMilliseconds(item.total_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="gpu-history-empty">No functional checks have been recorded.</div>
          )}
        </div>
      </section>
    </section>
  );
}
