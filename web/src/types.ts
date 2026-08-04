export type Speaker = "A" | "B";

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
    metrics: Record<string, number | string | boolean>;
  };
  raw_overlap_ratio: number;
  separation_used: boolean;
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
