import type { PlaybackRange, PlaybackState } from "./productContracts";

export const PLAYBACK_SAMPLE_RATE = 24_000;
export const VIDEO_DRIFT_TOLERANCE_SECONDS = 0.12;

export interface AudioTransport {
  play(stopAtSeconds?: number): Promise<void> | void;
  pause(): void;
  setTime(seconds: number): void;
  getCurrentTime(): number;
  getDuration(): number;
  setPlaybackRate(rate: number): void;
}

export interface VideoTransport {
  currentTime: number;
  playbackRate: number;
  muted: boolean;
  paused: boolean;
  play(): Promise<void> | void;
  pause(): void;
}

export interface PlaybackSnapshot extends PlaybackState {
  error: string | null;
}

type Listener = (state: PlaybackSnapshot) => void;

function copyRange(range: PlaybackRange | null): PlaybackRange | null {
  return range ? { ...range } : null;
}

export class PlaybackController {
  private readonly audio: AudioTransport;
  private readonly durationSamples: number;
  private readonly listeners = new Set<Listener>();
  private video: VideoTransport | null = null;
  private ready = false;
  private pendingRange: PlaybackRange | null = null;
  private externalSeekTarget: number | null = null;
  private playRequest = 0;
  private state: PlaybackSnapshot = {
    status: "loading",
    current_sample: 0,
    active_range: null,
    rate: 1,
    audition_mode: "mixed",
    error: null,
  };

  constructor(audio: AudioTransport, durationSamples: number) {
    this.audio = audio;
    this.durationSamples = Math.max(0, Math.round(durationSamples));
  }

  snapshot(): PlaybackSnapshot {
    return { ...this.state, active_range: copyRange(this.state.active_range) };
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    return () => this.listeners.delete(listener);
  }

  setVideo(video: VideoTransport | null): void {
    this.video = video;
    if (!video) return;
    video.muted = true;
    video.playbackRate = this.state.rate;
    this.syncVideo(true);
    if (this.state.status === "playing") this.playVideo();
    else if (!video.paused) video.pause();
  }

  async markReady(): Promise<void> {
    if (this.ready) return;
    this.ready = true;
    this.update({ status: "paused", error: null });
    const pending = this.pendingRange;
    this.pendingRange = null;
    if (pending) await this.startRange(pending);
  }

  markError(error: unknown): void {
    this.playRequest += 1;
    this.pendingRange = null;
    this.audio.pause();
    if (this.video && !this.video.paused) this.video.pause();
    this.update({
      status: "error",
      active_range: null,
      error: error instanceof Error ? error.message : String(error || "Playback failed"),
    });
  }

  async playRange(range: PlaybackRange): Promise<void> {
    const normalized = this.normalizeRange(range);
    if (!normalized) {
      this.markError("Invalid playback range");
      return;
    }
    this.pendingRange = normalized;
    this.update({
      active_range: normalized,
      current_sample: normalized.start_sample,
      error: null,
    });
    if (!this.ready) return;
    this.pendingRange = null;
    await this.startRange(normalized);
  }

  async toggle(): Promise<void> {
    if (!this.ready || this.state.status === "loading") return;
    if (this.state.status === "playing") {
      this.pause();
      return;
    }
    await this.play();
  }

  /** Starts or resumes playback without turning an already-playing transport off. */
  async play(): Promise<void> {
    if (!this.ready || this.state.status === "loading" || this.state.status === "playing") return;
    const range = this.state.active_range;
    if (
      range
      && (this.state.current_sample < range.start_sample
        || this.state.current_sample >= range.end_sample)
    ) {
      this.seekTransport(range.start_sample);
    }
    await this.beginPlayback();
  }

  pause(): void {
    this.playRequest += 1;
    this.audio.pause();
    if (this.video && !this.video.paused) this.video.pause();
    if (this.state.status !== "error") this.update({ status: "paused" });
  }

  stop(): void {
    this.pendingRange = null;
    this.pause();
    this.update({ active_range: null });
  }

  clearRange(): void {
    this.pendingRange = null;
    this.update({ active_range: null });
    if (this.state.status === "playing") {
      this.audio.setTime(this.state.current_sample / PLAYBACK_SAMPLE_RATE);
    }
  }

  seekToSample(sample: number): void {
    const target = this.clampSample(sample);
    const range = this.state.active_range;
    const activeRange = range && (target < range.start_sample || target >= range.end_sample)
      ? null
      : range;
    this.update({ current_sample: target, active_range: activeRange });
    this.audio.setTime(target / PLAYBACK_SAMPLE_RATE);
    this.syncVideo(true);
    if (activeRange && this.state.status === "playing") void this.beginPlayback();
  }

  seekBySeconds(seconds: number): void {
    this.seekToSample(
      this.state.current_sample + Math.round(seconds * PLAYBACK_SAMPLE_RATE),
    );
  }

  setRate(rate: number): void {
    if (!Number.isFinite(rate) || rate <= 0 || rate > 4) {
      this.markError("Playback rate must be greater than 0 and at most 4");
      return;
    }
    this.audio.setPlaybackRate(rate);
    if (this.video) this.video.playbackRate = rate;
    this.update({ rate, error: null });
  }

  setAuditionMode(mode: PlaybackState["audition_mode"]): void {
    if (!["mixed", "left", "right", "speaker_a", "speaker_b"].includes(mode)) return;
    this.update({ audition_mode: mode });
  }

  handleTimeUpdate(seconds: number): void {
    const sample = this.clampSample(Math.round(seconds * PLAYBACK_SAMPLE_RATE));
    if (this.externalSeekTarget !== null) {
      this.update({ current_sample: sample });
      this.syncVideo(false);
      return;
    }
    const range = this.state.active_range;
    if (range && sample >= range.end_sample) {
      if (range.behavior === "loop") {
        this.seekTransport(range.start_sample);
        void this.beginPlayback();
        return;
      }
      this.playRequest += 1;
      this.update({
        status: "paused",
        current_sample: range.end_sample,
        active_range: null,
      });
      this.audio.pause();
      this.audio.setTime(range.end_sample / PLAYBACK_SAMPLE_RATE);
      if (this.video && !this.video.paused) this.video.pause();
      this.syncVideo(true);
      return;
    }
    if (range && sample < range.start_sample) {
      this.update({ current_sample: sample, active_range: null });
    } else {
      this.update({ current_sample: sample });
    }
    this.syncVideo(false);
  }

  handleInteraction(seconds: number): void {
    const sample = this.clampSample(Math.round(seconds * PLAYBACK_SAMPLE_RATE));
    this.externalSeekTarget = null;
    const range = this.state.active_range;
    this.update({
      current_sample: sample,
      active_range: range && (sample < range.start_sample || sample >= range.end_sample)
        ? null
        : range,
    });
    this.syncVideo(true);
    if (this.state.active_range && this.state.status === "playing") {
      void this.beginPlayback();
    }
  }

  prepareExternalSeek(sample: number): void {
    const target = this.clampSample(sample);
    this.externalSeekTarget = target;
    const range = this.state.active_range;
    this.update({
      active_range: range && (target < range.start_sample || target >= range.end_sample)
        ? null
        : range,
    });
  }

  handleAudioPlay(): void {
    if (this.state.status === "error") return;
    this.update({ status: "playing", error: null });
    this.playVideo();
  }

  handleAudioPause(): void {
    if (this.video && !this.video.paused) this.video.pause();
    if (this.state.status === "playing") this.update({ status: "paused" });
  }

  handleAudioEnded(): void {
    const range = this.state.active_range;
    if (range?.behavior === "loop") {
      this.seekTransport(range.start_sample);
      void this.beginPlayback();
      return;
    }
    this.playRequest += 1;
    if (this.video && !this.video.paused) this.video.pause();
    if (range) {
      this.update({
        status: "paused",
        current_sample: range.end_sample,
        active_range: null,
      });
      this.audio.setTime(range.end_sample / PLAYBACK_SAMPLE_RATE);
      this.syncVideo(true);
    } else {
      this.update({ status: "ended", active_range: null });
    }
  }

  destroy(): void {
    this.playRequest += 1;
    this.pendingRange = null;
    this.listeners.clear();
    this.video = null;
  }

  private async startRange(range: PlaybackRange): Promise<void> {
    this.update({ active_range: range, error: null });
    this.seekTransport(range.start_sample);
    await this.beginPlayback();
  }

  private async beginPlayback(): Promise<void> {
    const request = ++this.playRequest;
    const stopAt = this.state.active_range
      ? this.state.active_range.end_sample / PLAYBACK_SAMPLE_RATE
      : undefined;
    try {
      await this.audio.play(stopAt);
      if (request !== this.playRequest) return;
      this.update({ status: "playing", error: null });
      this.playVideo();
    } catch (error) {
      if (request === this.playRequest) this.markError(error);
    }
  }

  private playVideo(): void {
    if (!this.video) return;
    this.video.muted = true;
    this.video.playbackRate = this.state.rate;
    this.syncVideo(false);
    void Promise.resolve(this.video.play()).catch(() => undefined);
  }

  private seekTransport(sample: number): void {
    const target = this.clampSample(sample);
    this.update({ current_sample: target });
    this.audio.setTime(target / PLAYBACK_SAMPLE_RATE);
    this.syncVideo(true);
  }

  private syncVideo(force: boolean): void {
    if (!this.video) return;
    const seconds = this.state.current_sample / PLAYBACK_SAMPLE_RATE;
    if (force || Math.abs(this.video.currentTime - seconds) > VIDEO_DRIFT_TOLERANCE_SECONDS) {
      this.video.currentTime = seconds;
    }
  }

  private maxSample(): number {
    if (this.durationSamples > 0) return this.durationSamples;
    return Math.max(0, Math.round(this.audio.getDuration() * PLAYBACK_SAMPLE_RATE));
  }

  private clampSample(sample: number): number {
    const value = Number.isFinite(sample) ? Math.round(sample) : 0;
    const maximum = this.maxSample();
    return Math.max(0, maximum > 0 ? Math.min(maximum, value) : value);
  }

  private normalizeRange(range: PlaybackRange): PlaybackRange | null {
    if (
      !Number.isFinite(range.start_sample)
      || !Number.isFinite(range.end_sample)
      || range.start_sample < 0
      || range.end_sample <= range.start_sample
      || !["once", "loop"].includes(range.behavior)
    ) {
      return null;
    }
    const startSample = this.clampSample(range.start_sample);
    const endSample = this.clampSample(range.end_sample);
    if (endSample <= startSample) return null;
    return {
      start_sample: startSample,
      end_sample: endSample,
      behavior: range.behavior,
    };
  }

  private update(patch: Partial<PlaybackSnapshot>): void {
    this.state = {
      ...this.state,
      ...patch,
      active_range: patch.active_range === undefined
        ? this.state.active_range
        : copyRange(patch.active_range),
    };
    const snapshot = this.snapshot();
    this.listeners.forEach((listener) => listener(snapshot));
  }
}
