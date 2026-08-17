export type Speaker = "A" | "B";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "user";
  status: "pending" | "active" | "disabled";
  group_name?: string | null;
  email_verified_at?: string | null;
}

export interface Project {
  id: string;
  name: string;
  language: string;
  source_count?: number;
  ready_sources?: number;
  created_at: string;
  updated_at: string;
}

export interface Source {
  id: string;
  project_id: string;
  original_name: string;
  status: string;
  init_mode?: string;
  duration_samples?: number;
  origin: string;
  rights_basis?: string;
  rights_notes: string;
  rights_confirmed: boolean;
  clips_stale: boolean;
  active_annotation_version: number;
  inspection?: {
    video_frame_rate?: number | null;
    channel_routing?: {
      channel_count: number;
      routing_candidate: boolean;
      recommended_mode: "mono" | "independent_stereo";
      reason: string;
      channel_rms_db?: number[];
      absolute_correlation?: number;
      estimated_lag_ms?: number;
      left_dominant_fraction?: number;
      right_dominant_fraction?: number;
      dual_mono?: boolean;
      suggested_speaker_channel_map?: Partial<Record<Speaker, number>>;
      mapping_suggestion_error?: string;
    };
  };
}

export interface ActivityRegion {
  id: string;
  speaker: Speaker;
  start_sample: number;
  end_sample: number;
  origin: "manual" | "model";
  confidence?: number | null;
}

export interface ExclusionRegion {
  id: string;
  kind: "music" | "advertisement" | "noise" | "third_speaker" | "unusable";
  start_sample: number;
  end_sample: number;
  note: string;
}

export interface SpeakerReferenceRegion {
  id: string;
  speaker: Speaker;
  start_sample: number;
  end_sample: number;
  note: string;
}

export interface TranscriptCandidate {
  source: "retry" | "secondary_asr" | "overlap_assistant" | "overlap_user";
  model: string;
  text: string;
  average_log_probability?: number | null;
  quality_flags: string[];
}

export interface TranscriptUtterance {
  id: string;
  speaker?: Speaker | null;
  start_sample: number;
  end_sample: number;
  text: string;
  model_text: string;
  model_speaker?: Speaker | null;
  quality_flags: string[];
  alignment_status: "not_run" | "aligned" | "low_confidence" | "unaligned";
  human_verified: boolean;
  review_candidates: TranscriptCandidate[];
}

export interface Annotation {
  source_id: string;
  version: number;
  assistant_speaker?: Speaker | null;
  channel_routing_mode: "mono" | "independent_stereo";
  channel_routing_verified: boolean;
  speaker_channel_map: Partial<Record<Speaker, number>>;
  activities_finalized: boolean;
  activities: ActivityRegion[];
  speaker_references: SpeakerReferenceRegion[];
  exclusions: ExclusionRegion[];
  transcript: TranscriptUtterance[];
  aligned_words: Record<string, unknown>[];
  note: string;
}

export interface Clip {
  id: string;
  start_sample: number;
  end_sample: number;
  status: "valid" | "invalid";
  reasons: string[];
  metrics: Record<string, number | string>;
}

export interface ClipArtifact {
  clip: Clip;
  qc: {
    status: "PASS" | "REVIEW" | "REJECT";
    reasons: string[];
    metrics: Record<string, number | string | boolean | null>;
  };
  raw_overlap_ratio: number;
  separation_used: boolean;
  recovery_method?: string | null;
  routing_method?: string;
  transcript?: {
    assistant_speaker: Speaker;
    original_and_normalized: {
      original: string;
      normalized: string;
      start: number;
      end: number;
      score?: number | null;
    }[];
    skipped_words: { word: string; reason: string }[];
  };
  decision?: {
    decision: "approve" | "reject" | "needs_work";
    auditioned: boolean;
  };
}

export interface OverlapRecovery {
  region_id: string;
  start_sample: number;
  end_sample: number;
  status: "recovered" | "failed";
  decision?: "approve" | "reject" | null;
  auditioned: boolean;
  details: Record<string, unknown>;
}

export interface SourceDetail extends Source {
  annotation: Annotation;
  annotation_revisions: { version: number; created_at: string }[];
  overlaps: { start_sample: number; end_sample: number }[];
  silences: { start_sample: number; end_sample: number }[];
  overlap_recoveries: OverlapRecovery[];
  quality_dashboard: {
    utterances: number;
    flagged_utterances: number;
    unresolved_flagged_utterances: number;
    assistant_unresolved: number;
    assistant_alignment_coverage: number;
    golden_examples: number;
    golden_target: number;
    model_character_error_rate?: number | null;
    speaker_correction_rate?: number | null;
    recovered_overlap_approval_rate?: number | null;
    review_queue: {
      utterance_id: string;
      priority: number;
      start_sample: number;
      end_sample: number;
      speaker?: Speaker | null;
      flags: string[];
    }[];
  };
  clip_plan?: {
    feasible: boolean;
    message: string;
    mode: string;
    request: {
      mode: string;
      count?: number | null;
      target_duration_seconds?: number | null;
      boundaries_samples?: number[];
    };
    clips: Clip[];
  } | null;
  clip_artifacts: {
    stale: boolean;
    artifacts: ClipArtifact[];
  };
  urls: {
    canonical_audio: string;
    canonical_channels?: string | null;
    video_proxy?: string | null;
    peaks?: string | null;
    original: string;
  };
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "complete" | "failed";
  progress: number;
  message: string;
  error?: string | null;
  result?: Record<string, unknown> | null;
}

export interface ProjectValidation {
  valid: boolean;
  blockers: string[];
  warnings: string[];
  ready_sources: number;
  approved_clips: number;
  sources: {
    source_id: string;
    original_name: string;
    status: "ready" | "blocked" | "skipped";
    generated_clips: number;
    approved_clips: number;
    messages: string[];
  }[];
}

export type GpuTopLevelState =
  | "OFF"
  | "STARTING"
  | "CHECKING"
  | "READY"
  | "BUSY"
  | "DEGRADED"
  | "INCOMPATIBLE"
  | "BLOCKED"
  | "ERROR"
  | "BLOCKED/ERROR"
  | "STOPPING"
  | "UNKNOWN";

export type GpuInstanceState =
  | "pending"
  | "running"
  | "shutting-down"
  | "terminated"
  | "stopping"
  | "stopped"
  | "unknown";

export type GpuDesiredState = "running" | "stopped" | "unknown";

export type GpuServiceState =
  | "offline"
  | "starting"
  | "online"
  | "busy"
  | "draining"
  | "stale"
  | "incompatible"
  | "error"
  | "unknown";

export type GpuCheckStatus =
  | "never"
  | "requested"
  | "queued"
  | "starting"
  | "waiting"
  | "running"
  | "passed"
  | "failed"
  | "timed_out"
  | "stale"
  | "cancelled";

export type GpuCheckTrigger = "manual" | "job_preflight";

export type GpuDispatcherState =
  | "idle"
  | "claimed"
  | "prepared"
  | "creating"
  | "uploading"
  | "starting"
  | "accepted"
  | "running"
  | "completion_pending"
  | "cancel_requested"
  | "complete"
  | "failed"
  | "cancelled"
  | "fenced"
  | "blocked"
  | "error"
  | "unknown";

export interface GpuCheck {
  id: string;
  gpu_check_id: string | null;
  instance_id: string | null;
  trigger: GpuCheckTrigger;
  requested_by: string | null;
  status: GpuCheckStatus;
  requirement_key: string | null;
  host_boot_id: string | null;
  service_boot_id: string | null;
  dispatch_protocol: string | null;
  worker_protocol: string | null;
  actual_build_id: string | null;
  expected_build_id: string | null;
  model_revision: string | null;
  config_fingerprint: string | null;
  fixture_id: string | null;
  fixture_hash_prefix: string | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  valid_until: string | null;
  updated_at: string;
  gpu_name: string | null;
  device: string | null;
  segment_count: number | null;
  cer: number | null;
  cer_threshold: number | null;
  model_load_ms: number | null;
  inference_ms: number | null;
  total_ms: number | null;
  failure_class: string | null;
  failure_summary: string | null;
}

export interface GpuMachineStatus {
  instance_id: string | null;
  instance_state: GpuInstanceState;
  desired_state: GpuDesiredState;
  last_aws_observation: string | null;
  observation_age_seconds: number | null;
  last_transition_at: string | null;
  last_error: string | null;
  idle_stop_at: string | null;
}

export interface GpuServiceStatus {
  state: GpuServiceState;
  last_intake_observation: string | null;
  observation_age_seconds: number | null;
  last_worker_heartbeat: string | null;
  worker_age_seconds: number | null;
  current_job_id: string | null;
  gpu_name: string | null;
  dispatch_protocol_version: string | null;
  expected_dispatch_protocol_version: string;
  worker_protocol_version: string | null;
  expected_worker_protocol_version: string;
  build_id: string | null;
  expected_build_id: string;
  queue_count: number;
  running_count: number;
  accepting_dispatches: boolean;
  callback_ready: boolean;
  operational_ready: boolean;
}

export interface GpuDispatcherStatus {
  state: GpuDispatcherState;
  active_dispatch_id: string | null;
  last_error: string | null;
}

export interface GpuSystemStatus {
  state: GpuTopLevelState;
  machine: GpuMachineStatus;
  service: GpuServiceStatus;
  functional_check: GpuCheck | null;
  dispatcher: GpuDispatcherStatus;
}

export interface GpuCheckHistory {
  checks: GpuCheck[];
}

export interface GpuCheckTriggerResult {
  check: GpuCheck;
  created: boolean;
  cost_notice: string;
}
