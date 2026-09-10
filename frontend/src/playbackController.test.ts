import { describe, expect, it, vi } from "vitest";
import {
  PLAYBACK_SAMPLE_RATE,
  PlaybackController,
  type AudioTransport,
  type VideoTransport,
} from "./playbackController";

class AudioStub implements AudioTransport {
  currentTime = 0;
  duration = 10;
  paused = true;
  rate = 1;
  playError: Error | null = null;
  play = vi.fn(async (_stopAtSeconds?: number) => {
    if (this.playError) throw this.playError;
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
  setTime = vi.fn((seconds: number) => {
    this.currentTime = seconds;
  });
  getCurrentTime = vi.fn(() => this.currentTime);
  getDuration = vi.fn(() => this.duration);
  setPlaybackRate = vi.fn((rate: number) => {
    this.rate = rate;
  });
}

class VideoStub implements VideoTransport {
  currentTime = 0;
  playbackRate = 1;
  muted = false;
  paused = true;
  play = vi.fn(async () => {
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
}

function setup() {
  const audio = new AudioStub();
  const controller = new PlaybackController(audio, 10 * PLAYBACK_SAMPLE_RATE);
  return { audio, controller };
}

describe("PlaybackController", () => {
  it("queues the latest range while loading and starts it when ready", async () => {
    const { audio, controller } = setup();
    await controller.playRange({ start_sample: 1_000, end_sample: 2_000, behavior: "once" });
    await controller.playRange({ start_sample: 3_000, end_sample: 4_000, behavior: "loop" });
    expect(audio.play).not.toHaveBeenCalled();

    await controller.markReady();
    expect(audio.setTime).toHaveBeenLastCalledWith(3_000 / PLAYBACK_SAMPLE_RATE);
    expect(audio.play).toHaveBeenCalledOnce();
    expect(controller.snapshot()).toMatchObject({
      status: "playing",
      current_sample: 3_000,
      active_range: { start_sample: 3_000, end_sample: 4_000, behavior: "loop" },
    });
  });

  it("plays once, pauses exactly at the end, and clears the range", async () => {
    const { audio, controller } = setup();
    await controller.markReady();
    await controller.playRange({
      start_sample: PLAYBACK_SAMPLE_RATE,
      end_sample: 2 * PLAYBACK_SAMPLE_RATE,
      behavior: "once",
    });
    expect(audio.play).toHaveBeenLastCalledWith(2);

    controller.handleTimeUpdate(2.04);
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.setTime).toHaveBeenLastCalledWith(2);
    expect(controller.snapshot()).toMatchObject({
      status: "paused",
      current_sample: 2 * PLAYBACK_SAMPLE_RATE,
      active_range: null,
    });
  });

  it("loops from the exact start and keeps the active range", async () => {
    const { audio, controller } = setup();
    await controller.markReady();
    await controller.playRange({
      start_sample: PLAYBACK_SAMPLE_RATE,
      end_sample: 2 * PLAYBACK_SAMPLE_RATE,
      behavior: "loop",
    });

    controller.handleTimeUpdate(2.02);
    expect(audio.setTime).toHaveBeenLastCalledWith(1);
    expect(audio.pause).not.toHaveBeenCalled();
    expect(controller.snapshot()).toMatchObject({
      status: "playing",
      current_sample: PLAYBACK_SAMPLE_RATE,
      active_range: { behavior: "loop" },
    });
  });

  it("restarts the same requested range and replaces an existing range", async () => {
    const { audio, controller } = setup();
    await controller.markReady();
    const first = { start_sample: 1_000, end_sample: 2_000, behavior: "once" as const };
    await controller.playRange(first);
    await controller.playRange(first);
    await controller.playRange({ start_sample: 5_000, end_sample: 6_000, behavior: "loop" });
    expect(audio.play).toHaveBeenCalledTimes(3);
    expect(audio.setTime).toHaveBeenLastCalledWith(5_000 / PLAYBACK_SAMPLE_RATE);
    expect(controller.snapshot().active_range).toMatchObject({ start_sample: 5_000 });
  });

  it("preserves a range across pause but clears it after an outside seek", async () => {
    const { controller } = setup();
    await controller.markReady();
    await controller.playRange({ start_sample: 24_000, end_sample: 48_000, behavior: "loop" });
    controller.pause();
    expect(controller.snapshot().active_range).not.toBeNull();
    await controller.toggle();
    expect(controller.snapshot().status).toBe("playing");

    controller.seekToSample(72_000);
    expect(controller.snapshot().active_range).toBeNull();
    expect(controller.snapshot().current_sample).toBe(72_000);
  });

  it("clears a loop before an out-of-range waveform interaction is processed", async () => {
    const { audio, controller } = setup();
    await controller.markReady();
    await controller.playRange({ start_sample: 24_000, end_sample: 48_000, behavior: "loop" });
    const controllerSeekCount = audio.setTime.mock.calls.length;

    controller.prepareExternalSeek(72_000);
    controller.handleTimeUpdate(3);
    controller.handleInteraction(3);
    expect(controller.snapshot()).toMatchObject({
      current_sample: 72_000,
      active_range: null,
      status: "playing",
    });
    expect(audio.setTime).toHaveBeenCalledTimes(controllerSeekCount);
  });

  it("restarts a loop if the underlying media reports its natural end", async () => {
    const { audio, controller } = setup();
    await controller.markReady();
    await controller.playRange({ start_sample: 24_000, end_sample: 48_000, behavior: "loop" });
    controller.handleAudioEnded();
    await Promise.resolve();
    expect(audio.setTime).toHaveBeenLastCalledWith(1);
    expect(audio.play).toHaveBeenCalledTimes(2);
    expect(controller.snapshot().status).toBe("playing");
  });

  it("keeps video muted, rate-matched, and within the drift tolerance", async () => {
    const { controller } = setup();
    const video = new VideoStub();
    controller.setVideo(video);
    await controller.markReady();
    controller.setRate(1.5);
    await controller.playRange({ start_sample: 24_000, end_sample: 48_000, behavior: "once" });
    expect(video.muted).toBe(true);
    expect(video.playbackRate).toBe(1.5);
    expect(video.play).toHaveBeenCalled();

    video.currentTime = 1.05;
    controller.handleTimeUpdate(1.1);
    expect(video.currentTime).toBe(1.05);
    video.currentTime = 0.5;
    controller.handleTimeUpdate(1.1);
    expect(video.currentTime).toBe(1.1);
  });

  it("enters an error state when playback is rejected", async () => {
    const { audio, controller } = setup();
    audio.playError = new Error("decoder unavailable");
    await controller.markReady();
    await controller.playRange({ start_sample: 0, end_sample: 24_000, behavior: "once" });
    expect(controller.snapshot()).toMatchObject({
      status: "error",
      active_range: null,
      error: "decoder unavailable",
    });
  });
});
