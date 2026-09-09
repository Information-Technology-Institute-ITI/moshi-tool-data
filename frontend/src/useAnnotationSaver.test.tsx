// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDraftsForUser, useAnnotationSaver, type Conflict } from "./useAnnotationSaver";
import type { Annotation } from "./types";

function annotation(version: number, note: string): Annotation {
  return {
    source_id: "source_1",
    version,
    assistant_speaker: null,
    channel_routing_mode: "mono",
    channel_routing_verified: false,
    speaker_channel_map: {},
    activities_finalized: false,
    activities: [],
    speaker_references: [],
    exclusions: [],
    transcript: [],
    aligned_words: [],
    note,
  };
}

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

type Harness = ReturnType<typeof useAnnotationSaver>;
let harness: Harness;
let conflicts: Conflict[];
let errors: string[];
let container: HTMLDivElement;
let root: Root;

function Probe() {
  harness = useAnnotationSaver({
    sourceId: "source_1",
    onConflict: (value) => conflicts.push(value),
    onError: (value) => errors.push(value),
  });
  return null;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  conflicts = [];
  errors = [];
  window.localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function mount() {
  await act(async () => root.render(<Probe />));
}

describe("explicit annotation saving", () => {
  it("sends exactly the snapshot passed to Save", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(annotation(4, "stored")));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    let saved: Annotation | null = null;
    await act(async () => {
      saved = await harness.save(annotation(3, "manual edit"));
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body));
    expect(body.expected_version).toBe(3);
    expect(body.annotation.note).toBe("manual edit");
    expect(saved!.version).toBe(4);
    expect(harness.status).toBe("saved");
  });

  it("ignores a duplicate Save while the first request is running", async () => {
    let finish!: (value: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    let first!: Promise<Annotation | null>;
    let second!: Promise<Annotation | null>;
    await act(async () => {
      first = harness.save(annotation(3, "first"));
      second = harness.save(annotation(3, "duplicate"));
    });
    expect(await second).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    finish(response(annotation(4, "first")));
    await act(async () => { await first; });
  });

  it("keeps a failed save failed until the reviewer retries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => { await harness.save(annotation(3, "unsaved")); });
    expect(harness.status).toBe("failed");
    expect(errors).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("loads the authoritative document when an explicit save conflicts", async () => {
    const server = annotation(9, "other reviewer");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ detail: "stale" }, 409))
      .mockResolvedValueOnce(response({ annotation: server, revisions: [{ version: 9 }] }));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => { await harness.save(annotation(3, "my work")); });
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0].local.note).toBe("my work");
    expect(conflicts[0].server.version).toBe(9);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

it("removes only obsolete drafts for the selected user", () => {
  window.localStorage.setItem("moshi.draft.user_a.source_1.v3", "old");
  window.localStorage.setItem("moshi.draft.user_b.source_1.v3", "other user");
  window.localStorage.setItem("unrelated", "keep");
  clearDraftsForUser("user_a");
  expect(window.localStorage.getItem("moshi.draft.user_a.source_1.v3")).toBeNull();
  expect(window.localStorage.getItem("moshi.draft.user_b.source_1.v3")).toBe("other user");
  expect(window.localStorage.getItem("unrelated")).toBe("keep");
});
