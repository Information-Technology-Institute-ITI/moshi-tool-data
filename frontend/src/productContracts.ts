/** Versioned cross-layer contracts approved in implementation phase P0. */

export const REVIEW_CONTRACT_VERSION = "studio.review/v1" as const;
export const EVALUATION_CONTRACT_VERSION = "studio.evaluation/v1" as const;
export const TRAINING_CONTRACT_VERSION = "studio.training/v1" as const;
export const ERROR_CONTRACT_VERSION = "studio.error/v1" as const;

export interface SampleRange {
  start_sample: number;
  end_sample: number;
}

export interface ReviewChapterConfig {
  contract_version: typeof REVIEW_CONTRACT_VERSION;
  mode: "max_duration" | "count" | "manual";
  max_duration_seconds: number;
  count?: number | null;
  manual_boundaries_samples: number[];
  boundary_search_seconds: number;
}

export interface ReviewChapter extends SampleRange {
  id: string;
  ordinal: number;
  boundary_reason: "source_edge" | "silence" | "segment" | "manual";
}

export interface ReviewChapterSet {
  contract_version: typeof REVIEW_CONTRACT_VERSION;
  id: string;
  source_id: string;
  annotation_version: number;
  active: boolean;
  config: ReviewChapterConfig;
  chapters: ReviewChapter[];
}

export interface WaveformPeakWindow extends SampleRange {
  contract_version: typeof REVIEW_CONTRACT_VERSION;
  source_id: string;
  sample_rate: number;
  duration_samples: number;
  samples_per_point: number;
  points: [number, number][];
}

export interface PlaybackRange extends SampleRange {
  behavior: "once" | "loop";
}

export interface PlaybackState {
  status: "loading" | "paused" | "playing" | "ended" | "error";
  current_sample: number;
  active_range: PlaybackRange | null;
  rate: number;
  audition_mode: "mixed" | "left" | "right" | "speaker_a" | "speaker_b";
}

export interface ApiErrorDetail {
  code: string;
  message: string;
  field?: string | null;
  retryable: boolean;
  context: Record<string, unknown>;
}

export interface ApiErrorEnvelope {
  contract_version: typeof ERROR_CONTRACT_VERSION;
  error: ApiErrorDetail;
}

export interface ModelRunSummary {
  contract_version: typeof EVALUATION_CONTRACT_VERSION;
  id: string;
  source_id: string;
  initialization_job_id?: string | null;
  model_name: string;
  model_revision?: string | null;
  language?: string | null;
  config_fingerprint?: string | null;
  raw_transcript_artifact_id?: string | null;
  aligned_transcript_artifact_id?: string | null;
  diarization_artifact_id?: string | null;
  comparison_available: boolean;
  created_at: string;
}

export interface OverlapReviewRecord extends SampleRange {
  contract_version: typeof REVIEW_CONTRACT_VERSION;
  id: string;
  source_id: string;
  annotation_version: number;
  speaker_a_activity_id: string;
  speaker_b_activity_id: string;
  classification: "confirmed" | "false_positive" | "third_speaker" | "noise" | "unintelligible";
  training_decision: "raw" | "separate" | "exclude" | "needs_work";
  state: "current" | "stale";
  note: string;
  reviewer_user_id?: string | null;
  recovery_artifact_ids: string[];
}

export interface EvaluationScope {
  contract_version: typeof EVALUATION_CONTRACT_VERSION;
  project_id?: string | null;
  source_ids: string[];
  model_run_ids: string[];
  annotation_versions: Record<string, number>;
  chapter_ids: string[];
  speakers: ("A" | "B")[];
  verified_only: boolean;
  normalization_policy: string;
  overlap_policy: "deduplicate" | "include" | "exclude";
}

export interface ErrorMetric {
  distance: number;
  substitutions: number;
  insertions: number;
  deletions: number;
  reference_units: number;
  hypothesis_units: number;
  rate?: number | null;
}

export interface EvaluationPreview {
  contract_version: typeof EVALUATION_CONTRACT_VERSION;
  scope: EvaluationScope;
  word_error: ErrorMetric;
  character_error: ErrorMetric;
  normalized_word_error: ErrorMetric;
  normalized_character_error: ErrorMetric;
  evaluated_duration_samples: number;
  eligible_duration_samples: number;
  coverage: number;
  breakdowns: Record<string, unknown>;
}

export interface TrainingProfile {
  contract_version: typeof TRAINING_CONTRACT_VERSION;
  id?: string | null;
  name: string;
  target: "whisper" | "moshi";
  min_duration_seconds: number;
  max_duration_seconds: number;
  padding_seconds: number;
  verified_only: boolean;
  normalization_policy: string;
  overlap_policy: "raw" | "separate" | "exclude";
  speaker_scope: "both" | "assistant" | "user";
  channel_mode: "mono" | "stereo";
  manifest_format: "jsonl" | "csv" | "huggingface";
  settings: Record<string, unknown>;
}

export interface ValidationMessage {
  code: string;
  message: string;
  source_id?: string | null;
  segment_id?: string | null;
}

export interface TrainingValidation {
  contract_version: typeof TRAINING_CONTRACT_VERSION;
  valid: boolean;
  profile: TrainingProfile;
  annotation_versions: Record<string, number>;
  blockers: ValidationMessage[];
  warnings: ValidationMessage[];
  proposed_train_sources: string[];
  proposed_eval_sources: string[];
  sample_count: number;
  duration_seconds: number;
}

export interface TrainingPackageSummary {
  contract_version: typeof TRAINING_CONTRACT_VERSION;
  id: string;
  project_id: string;
  version: number;
  target: "whisper" | "moshi";
  status: "queued" | "running" | "complete" | "failed" | "stale";
  profile: TrainingProfile;
  annotation_versions: Record<string, number>;
  artifact_id?: string | null;
  created_by_user_id: string;
  created_at: string;
}
