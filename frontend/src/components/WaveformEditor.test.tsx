// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WaveformEditor from "./WaveformEditor";
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
  playPause: vi.fn(),
  setPlaybackRate: vi.fn(),
  getCurrentTime: vi.fn(() => 0),
  getDuration: vi.fn(() => 3),
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

async function render() {
  await act(async () => root.render(
    <WaveformEditor
      audioUrl="/media/source_1/canonical"
      annotation={annotation}
      durationSamples={72_000}
      frameRate={25}
      onChange={() => undefined}
    />,
  ));
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
    expect(text).toContain("Play / pause");
    expect(text).toContain("Set selection start [");
    expect(text).toContain("Finish as A");
    expect(text).toContain("Finish as B");
  });

  it("drops the shortcuts for the removed actions", async () => {
    await render();
    const note = container.querySelector(".shortcut-note")?.textContent || "";
    expect(note).not.toMatch(/X|T\b|L sets loop/);
    expect(note).toContain("[ then A / B");
  });

  it("draws the speaker regions once the audio is decoded, not before", async () => {
    await render();
    // Regression: regions added before decode had no duration to position
    // against and stayed invisible until a click, zoom or resize.
    expect(plugin.addRegion).not.toHaveBeenCalled();

    await act(async () => listeners.get("ready")?.(3));
    expect(plugin.addRegion).toHaveBeenCalledTimes(1);
    expect(plugin.addRegion.mock.calls[0][0]).toMatchObject({ id: "act_1", content: "A" });
  });
});
