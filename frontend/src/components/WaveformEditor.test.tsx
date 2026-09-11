// @vitest-environment jsdom

import { act, createRef, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WaveformEditor, { type WaveformEditorHandle } from "./WaveformEditor";
import type { Annotation } from "../types";

// The real player needs Web Audio and a decodable file; this exercises the
// controls around it, so a stub that records its listeners is enough.
const listeners = new Map<string, (...args: unknown[]) => void>();
const instance = {
  on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
    listeners.set(event, handler);
    return () => listeners.delete(event);
  }),
  once: vi.fn(),
  destroy: vi.fn(),
  zoom: vi.fn(),
  setTime: vi.fn(),
  play: vi.fn(async () => undefined),
  pause: vi.fn(),
  playPause: vi.fn(),
  setPlaybackRate: vi.fn(),
  getCurrentTime: vi.fn(() => 0),
  getDuration: vi.fn(() => 3),
  getScroll: vi.fn(() => 0),
  setScrollTime: vi.fn(),
  getWrapper: vi.fn(() => document.createElement("div")),
};
const plugin = {
  on: vi.fn(),
  clearRegions: vi.fn(),
  addRegion: vi.fn(),
};

vi.mock("wavesurfer.js", () => ({
  default: { create: () => instance },
}));
vi.mock("wavesurfer.js/dist/plugins/regions.esm.js", () => ({
  default: { create: () => plugin },
}));

const annotation: Annotation = {
  source_id: "source_1",
  version: 1,
  assistant_speaker: null,
  channel_routing_mode: "mono",
  channel_routing_verified: false,
  speaker_channel_map: {},
  activities_finalized: false,
  activities: [
    { id: "act_1", speaker: "A", start_sample: 0, end_sample: 24_000, origin: "model" },
  ],
  speaker_references: [],
  exclusions: [],
  transcript: [],
  aligned_words: [],
  note: "",
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  listeners.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

async function render(overrides: Partial<ComponentProps<typeof WaveformEditor>> = {}) {
  await act(async () => root.render(
    <WaveformEditor
      audioUrl="/media/source_1/canonical"
      annotation={annotation}
      durationSamples={72_000}
      frameRate={25}
      onChange={() => undefined}
      {...overrides}
    />,
  ));
}

async function ready() {
  await act(async () => {
    listeners.get("ready")?.(3);
    await Promise.resolve();
  });
}

function labels(): string[] {
  return Array.from(container.querySelectorAll("button")).map(
    (node) => node.textContent?.trim() || "",
  );
}

describe("waveform controls", () => {
  it("offers no loop-start, exclude, or transcript-segment buttons", async () => {
    await render();
    const text = labels();
    expect(text).not.toContain("Set loop start");
    expect(text).not.toContain("Finish loop");
    expect(text).not.toContain("Finish as Exclude");
    expect(text).not.toContain("Finish as transcript segment");
  });

  it("keeps the controls the review still needs", async () => {
    await render();
    const text = labels();
    expect(text).toContain("Play");
    expect(text).toContain("1. Mark range start");
    expect(text).toContain("2. End as Speaker A");
    expect(text).toContain("2. End as Speaker B");
  });

  it("drops the shortcuts for the removed actions", async () => {
    await render();
    const note = container.querySelector(".shortcut-note")?.textContent || "";
    expect(note).not.toMatch(/X|T\b|L sets loop/);
    expect(note).toContain("[ then A or B");
  });

  it("draws the speaker regions once the audio is decoded, not before", async () => {
    await render();
    // Regression: regions added before decode had no duration to position
    // against and stayed invisible until a click, zoom or resize.
    expect(plugin.addRegion).not.toHaveBeenCalled();

    await ready();
    expect(plugin.addRegion).toHaveBeenCalledTimes(1);
    expect(plugin.addRegion.mock.calls[0][0]).toMatchObject({ id: "act_1", content: "A" });
  });

  it("shows only speaker lanes from the selected chapter using chapter-relative positions", async () => {
    const chapterAnnotation = {
      ...annotation,
      activities: [
        ...annotation.activities,
        { id: "act_2", speaker: "B" as const, start_sample: 48_000, end_sample: 72_000, origin: "model" as const },
      ],
    };
    await render({
      annotation: chapterAnnotation,
      timelineRange: { start_sample: 24_000, end_sample: 72_000 },
    });
    const regions = container.querySelectorAll<HTMLElement>(".lane-region");
    expect(regions).toHaveLength(1);
    expect(regions[0].textContent).toBe("B");
    expect(regions[0].style.left).toBe("50%");
    expect(regions[0].style.width).toBe("50%");
  });

  it("mounts annotation tools into the requested left-rail target", async () => {
    const target = document.createElement("div");
    document.body.append(target);
    await render({ toolTarget: target });
    expect(container.querySelector(".annotation-actions")).toBeNull();
    expect(target.querySelector(".annotation-actions")?.textContent).toContain("End as Speaker A");
    target.remove();
  });

  it("exposes transport and annotation commands through one imperative surface", async () => {
    const ref = createRef<WaveformEditorHandle>();
    await render({ ref });
    await ready();
    await act(async () => ref.current?.seekBySeconds(1));
    expect(instance.setTime).toHaveBeenLastCalledWith(1);
    await act(async () => ref.current?.togglePlayback());
    expect(instance.play).toHaveBeenCalled();
    await act(async () => ref.current?.pausePlayback());
    expect(instance.pause).toHaveBeenCalled();
  });

  it("shows separate Play and Pause controls in the main transport", async () => {
    await render();
    await ready();
    const play = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.trim() === "Play")!;
    const pause = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.trim() === "Pause")!;
    expect(play).toBeTruthy();
    expect(pause.disabled).toBe(true);
    await act(async () => listeners.get("play")?.());
    expect(pause.disabled).toBe(false);
    await act(async () => pause.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(instance.pause).toHaveBeenCalled();
  });

  it("seeks by exactly one source-video frame", async () => {
    await render({ frameRate: 25 });
    await ready();
    const forward = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent?.trim() === "+ frame",
    );
    expect(forward).toBeTruthy();
    await act(async () => forward!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(instance.setTime).toHaveBeenLastCalledWith(0.04);
  });

  it("starts Play ranges and stops them at the exact segment end", async () => {
    await render({
      focusRange: {
        start_sample: 2_400,
        end_sample: 12_000,
        behavior: "once",
        nonce: 1,
      },
    });
    await ready();
    expect(instance.setTime).toHaveBeenCalledWith(0.1);
    expect(instance.play).toHaveBeenCalledOnce();

    await act(async () => listeners.get("timeupdate")?.(0.51));
    expect(instance.pause).toHaveBeenCalled();
    expect(instance.setTime).toHaveBeenLastCalledWith(0.5);
  });

  it("repeats Loop ranges and renders video only as a muted follower", async () => {
    const mediaPlay = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    await render({
      videoUrl: "/media/source_1/video",
      focusRange: {
        start_sample: 4_800,
        end_sample: 9_600,
        behavior: "loop",
        nonce: 1,
      },
    });
    const video = container.querySelector("video")!;
    expect(video.muted).toBe(true);
    expect(video.controls).toBe(false);

    await ready();
    expect(mediaPlay).toHaveBeenCalled();
    await act(async () => listeners.get("timeupdate")?.(0.41));
    expect(instance.setTime).toHaveBeenLastCalledWith(0.2);
    expect(labels()).toContain("Clear loop");
  });

  it("offers channel audition only for verified independent stereo routing", async () => {
    await render({
      channelAudioUrl: "/media/source_1/channels",
      annotation: {
        ...annotation,
        channel_routing_mode: "independent_stereo",
        channel_routing_verified: true,
        speaker_channel_map: { A: 0, B: 1 },
      },
    });
    const selector = container.querySelector<HTMLSelectElement>('[aria-label="Channel audition mode"]');
    expect(selector).toBeTruthy();
    expect(Array.from(selector!.options).map((option) => option.text)).toEqual([
      "Mixed",
      "Left channel",
      "Right channel",
      "Speaker A",
      "Speaker B",
    ]);
    await act(async () => {
      selector!.value = "speaker_b";
      selector!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(selector!.value).toBe("speaker_b");
  });
});
