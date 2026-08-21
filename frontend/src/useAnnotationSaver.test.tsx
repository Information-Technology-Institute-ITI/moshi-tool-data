// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AUTOSAVE_DEBOUNCE_MS,
  clearDraftsForUser,
  readDraft,
  useAnnotationSaver,
  type Conflict,
} from "./useAnnotationSaver";
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

type Harness = {
  schedule: (value: Annotation) => void;
  saveNow: () => void;
  flush: () => Promise<boolean>;
  status: string;
};

let harness: Harness;
let saved: Annotation[];
let conflicts: Conflict[];
let errors: string[];

function Probe({ userId = "user_a", sourceId = "source_1" }) {
  const saver = useAnnotationSaver({
    sourceId,
    userId,
    onSaved: (value) => saved.push(value),
    onConflict: (value) => conflicts.push(value),
    onError: (value) => errors.push(value),
  });
  harness = saver;
  return null;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  saved = [];
  conflicts = [];
  errors = [];
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mount(props: { userId?: string; sourceId?: string } = {}) {
  await act(async () => root.render(<Probe {...props} />));
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("debounced autosave", () => {
  it("coalesces rapid edits into one save carrying the newest content", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(response(annotation(4, "saved")));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => {
      harness.schedule(annotation(3, "first"));
      harness.schedule(annotation(3, "second"));
      harness.schedule(annotation(3, "third"));
    });
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(AUTOSAVE_DEBOUNCE_MS);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body));
    expect(body.annotation.note).toBe("third");
    expect(body.expected_version).toBe(3);
  });

  it("sends the save to the annotation endpoint and never enqueues GPU work", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(annotation(4, "saved")));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.saveNow());
    await act(async () => harness.schedule(annotation(3, "edit")));
    await act(async () => {
      harness.saveNow();
    });
    await settle();

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls).toContain("/api/sources/source_1/annotations");
    // No initialize, transcribe, rediarize, realign, overlap, clip or export call.
    expect(
      urls.filter((url) => /initialize|transcribe|rediarize|realign|overlap|generate|clip|export/.test(url)),
    ).toEqual([]);
  });
});

describe("recoverable local drafts", () => {
  it("keeps a draft when the save fails and clears it on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "unsaved work")));
    await act(async () => harness.saveNow());
    await settle();

    expect(errors.length).toBeGreaterThan(0);
    expect(readDraft("user_a", "source_1", 3)?.note).toBe("unsaved work");

    fetchMock.mockResolvedValue(response(annotation(4, "stored")));
    await act(async () => harness.saveNow());
    await settle();

    expect(saved.at(-1)?.version).toBe(4);
    expect(readDraft("user_a", "source_1", 3)).toBeNull();
  });

  it("scopes drafts by user and by source", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    await mount({ userId: "user_a", sourceId: "source_1" });
    await act(async () => harness.schedule(annotation(3, "alice work")));
    await act(async () => harness.saveNow());
    await settle();

    expect(readDraft("user_a", "source_1", 3)?.note).toBe("alice work");
    // Another user, another source, and another base version all miss.
    expect(readDraft("user_b", "source_1", 3)).toBeNull();
    expect(readDraft("user_a", "source_2", 3)).toBeNull();
    expect(readDraft("user_a", "source_1", 4)).toBeNull();
  });

  it("clears every draft belonging to a user on sign-out", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    await mount({ userId: "user_a" });
    await act(async () => harness.schedule(annotation(3, "work")));
    await act(async () => harness.saveNow());
    await settle();
    expect(readDraft("user_a", "source_1", 3)).not.toBeNull();

    clearDraftsForUser("user_a");
    expect(readDraft("user_a", "source_1", 3)).toBeNull();
  });
});

describe("draft recovery is defensive", () => {
  it("ignores a stored draft that is missing a list the screen reads", () => {
    // A draft from an older build could lack a field; installing it would throw
    // while rendering and blank the screen, so it is discarded instead.
    window.localStorage.setItem(
      "moshi.draft.user_a.source_1.v3",
      JSON.stringify({ source_id: "source_1", version: 3, note: "old shape" }),
    );
    expect(readDraft("user_a", "source_1", 3)).toBeNull();
  });

  it("returns a complete draft unchanged", () => {
    window.localStorage.setItem(
      "moshi.draft.user_a.source_1.v3",
      JSON.stringify(annotation(3, "kept")),
    );
    expect(readDraft("user_a", "source_1", 3)?.note).toBe("kept");
  });
});

describe("version conflicts", () => {
  /**
   * What GET /api/sources/{id}/annotations really answers with. The previous
   * test mocked a bare annotation, which is not the server's contract, so a
   * mismatch here went unnoticed until the conflict dialog crashed on it.
   */
  function envelope(value: Annotation) {
    return { annotation: value, revisions: [{ version: value.version }] };
  }

  it("reports both copies on 409 and creates no revision", async () => {
    const server = annotation(9, "server copy");
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") return response({ detail: "stale" }, 409);
      return response(envelope(server));
    });
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "my work")));
    await act(async () => harness.saveNow());
    await settle();

    expect(conflicts).toHaveLength(1);
    expect(conflicts[0].local.note).toBe("my work");
    expect(conflicts[0].server.version).toBe(9);
    // Nothing was reported as saved, and the local copy survives.
    expect(saved).toEqual([]);
    expect(readDraft("user_a", "source_1", 3)?.note).toBe("my work");
  });

  it("unwraps the server envelope so the conflict carries a usable annotation", async () => {
    const server = annotation(9, "server copy");
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") return response({ detail: "stale" }, 409);
      return response(envelope(server));
    });
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "my work")));
    await act(async () => harness.saveNow());
    await settle();

    // Regression: this arrived as {annotation, revisions}, so every field the
    // conflict dialog reads was undefined and reading .length threw.
    expect(Array.isArray(conflicts[0].server.transcript)).toBe(true);
    expect(conflicts[0].server.source_id).toBe("source_1");
    expect(conflicts[0].server).not.toHaveProperty("revisions");
  });

  it("reports an error rather than a broken conflict when the shape is wrong", async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") return response({ detail: "stale" }, 409);
      return response({ unexpected: true });
    });
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "my work")));
    await act(async () => harness.saveNow());
    await settle();

    expect(conflicts).toEqual([]);
    expect(errors[0]).toContain("could not be loaded");
    // The local copy is still recoverable.
    expect(readDraft("user_a", "source_1", 3)?.note).toBe("my work");
  });
});

describe("flush before navigation", () => {
  it("resolves true once the pending edit is stored", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(annotation(4, "stored")));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "pending")));
    let result: boolean | undefined;
    await act(async () => {
      result = await harness.flush();
    });

    expect(result).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(saved.at(-1)?.version).toBe(4);
  });

  it("resolves false when the save failed so the caller can stay put", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    await mount();

    await act(async () => harness.schedule(annotation(3, "pending")));
    let result: boolean | undefined;
    await act(async () => {
      result = await harness.flush();
    });

    expect(result).toBe(false);
    expect(readDraft("user_a", "source_1", 3)?.note).toBe("pending");
  });
});
