import { PLAYBACK_SAMPLE_RATE, PlaybackController } from "./playbackController";

const result = document.querySelector<HTMLOutputElement>("#result")!;
const audio = document.querySelector<HTMLAudioElement>("#audio")!;
const video = document.querySelector<HTMLVideoElement>("#video")!;
const mediaBase = new URLSearchParams(location.search).get("mediaBase")
  || "/p1-fixtures";

let scheduledEnd: number | undefined;
let finished = false;

function finish(status: "passed" | "failed", message: string) {
  if (finished) return;
  finished = true;
  result.dataset.status = status;
  result.textContent = message;
}

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function canPlay(element: HTMLMediaElement): Promise<void> {
  if (element.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return Promise.resolve();
  return new Promise((resolve, reject) => {
    element.addEventListener("canplay", () => resolve(), { once: true });
    element.addEventListener("error", () => reject(new Error("media decode failed")), { once: true });
  });
}

audio.src = `${mediaBase}/canonical.wav`;
video.src = `${mediaBase}/video.mp4`;
video.muted = true;
video.controls = false;

const controller = new PlaybackController(
  {
    play: async (stopAtSeconds) => {
      scheduledEnd = stopAtSeconds;
      await audio.play();
    },
    pause: () => audio.pause(),
    setTime: (seconds) => {
      audio.currentTime = seconds;
    },
    getCurrentTime: () => audio.currentTime,
    getDuration: () => audio.duration,
    setPlaybackRate: (rate) => {
      audio.playbackRate = rate;
    },
  },
  PLAYBACK_SAMPLE_RATE,
);
controller.setVideo(video);
audio.addEventListener("play", () => controller.handleAudioPlay());
audio.addEventListener("pause", () => controller.handleAudioPause());
audio.addEventListener("ended", () => controller.handleAudioEnded());
audio.addEventListener("error", () => controller.markError(audio.error || "audio error"));

async function run() {
  await Promise.all([canPlay(audio), canPlay(video)]);
  await controller.markReady();

  await controller.playRange({
    start_sample: 2_400,
    end_sample: 8_400,
    behavior: "once",
  });
  assert(!audio.paused, "Play did not start canonical audio");
  assert(scheduledEnd === 0.35, "once range was not scheduled at its exact end");
  audio.currentTime = 0.36;
  controller.handleTimeUpdate(0.36);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert(audio.paused, "once range did not pause");
  assert(
    Math.abs(audio.currentTime - 0.35) < 0.002,
    `once range did not stop exactly: ${audio.currentTime}`,
  );

  await controller.playRange({
    start_sample: 4_800,
    end_sample: 9_600,
    behavior: "loop",
  });
  audio.currentTime = 0.41;
  controller.handleTimeUpdate(0.41);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert(
    Math.abs(audio.currentTime - 0.2) < 0.002,
    `loop did not return to its exact start: ${audio.currentTime}`,
  );
  assert(controller.snapshot().active_range?.behavior === "loop", "loop range was lost");

  controller.setRate(1.25);
  assert(audio.playbackRate === 1.25, "canonical audio rate did not change");
  assert(video.playbackRate === 1.25, "video follower rate did not change");
  assert(video.muted && !video.controls, "video is not a muted follower");
  video.currentTime = 0;
  controller.handleTimeUpdate(0.3);
  assert(Math.abs(video.currentTime - 0.3) < 0.001, "video drift was not corrected");

  controller.stop();
  finish("passed", "P1 browser playback passed");
}

setTimeout(() => finish("failed", "timeout"), 8_000);
void run().catch((error) => finish("failed", error instanceof Error ? error.message : String(error)));
