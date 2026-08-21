// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Annotation, AuthUser, TranscriptUtterance } from "./types";

// wavesurfer needs a real audio stack; the review screen is exercised through
// the transcript surface instead, so the player is stubbed out.
vi.mock("./components/WaveformEditor", () => ({
  default: () => null,
}));

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const user: AuthUser = {
  id: "user_1",
  email: "editor@example.test",
  display_name: "Editor",
  role: "user",
  status: "active",
};

function utterance(
  id: string,
  start: number,
  end: number,
  speaker: "A" | "B",
  text: string,
  flags: string[] = [],
): TranscriptUtterance {
  return {
    id,
    speaker,
    start_sample: start,
    end_sample: end,
    text,
    model_text: "",
    model_speaker: null,
    quality_flags: flags,
    alignment_status: "aligned",
    human_verified: false,
    review_candidates: [],
  };
}

const annotation: Annotation = {
  source_id: "source_1",
  version: 2,
  assistant_speaker: null,
  channel_routing_mode: "mono",
  channel_routing_verified: false,
  speaker_channel_map: {},
  activities_finalized: false,
  activities: [
    { id: "act_1", speaker: "A", start_sample: 0, end_sample: 48_000, origin: "model" },
  ],
  speaker_references: [],
  exclusions: [],
  transcript: [
    utterance("u1", 0, 24_000, "A", "أهلا بك", ["repeated_ngram"]),
    utterance("u2", 24_000, 48_000, "A", "كيف حالك"),
    utterance("u3", 48_000, 72_000, "B", "بخير الحمد لله"),
  ],
  aligned_words: [],
  note: "",
};

const sourceDetail = {
  id: "source_1",
  project_id: "project_1",
  original_name: "episode.wav",
  status: "ready",
  duration_samples: 72_000,
  origin: "",
  rights_notes: "",
  rights_confirmed: false,
  clips_stale: false,
  active_annotation_version: 2,
  annotation,
  annotation_revisions: [{ version: 2, created_at: "2026-08-01T00:00:00Z" }],
  overlaps: [],
  silences: [],
  overlap_recoveries: [],
  inspection: { video_frame_rate: 25 },
  urls: { canonical_audio: "/media/source_1/canonical", original: "/media/source_1/original" },
};

const projectDetail = {
  project: {
    id: "project_1",
    name: "Cairo",
    language: "ar-EG",
    owner_user_id: "user_1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  },
  sources: [{ id: "source_1", original_name: "episode.wav", status: "ready" }],
  jobs: [],
};

function routedFetch(onCall?: (url: string, init?: RequestInit) => Response | undefined) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const custom = onCall?.(url, init);
    if (custom) return custom;
    if (url === "/api/auth/me") return response({ user, required: true });
    if (url === "/api/projects" && (!init?.method || init.method === "GET")) {
      return response({ projects: [projectDetail.project] });
    }
    if (url === "/api/projects/project_1") return response(projectDetail);
    if (url === "/api/sources/source_1") return response(sourceDetail);
    if (url === "/api/sources/source_1/annotations" && init?.method === "PUT") {
      return response({ ...annotation, version: 3 });
    }
    return response({}, 404);
  });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function byText(selector: string, text: string): HTMLElement | undefined {
  return Array.from(container.querySelectorAll<HTMLElement>(selector)).find(
    (node) => node.textContent?.trim() === text,
  );
}

async function click(node: Element | undefined | null) {
  expect(node).toBeTruthy();
  await act(async () => {
    node!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await flush();
}

/** Signs in, opens the project, then opens the source review screen. */
async function openReview(fetchMock: ReturnType<typeof routedFetch>) {
  vi.stubGlobal("fetch", fetchMock);
  await act(async () => root.render(<App />));
  await flush();
  await click(byText("button.intro-nav-action", "Open workspace"));
  await click(container.querySelector(".project-card-open"));
  await click(container.querySelector(".source-row-open"));
}

describe("unified review screen", () => {
  it("shows one review surface with the complete transcript and no tabs", async () => {
    await openReview(routedFetch());

    expect(container.textContent).toContain("Review audio and transcript");
    // The four-tab studio navigation is gone.
    expect(container.querySelector(".studio-rail nav")).toBeNull();
    for (const label of [
      "Source activity",
      "Transcript",
      "Overlap recovery",
      "Clips and review",
    ]) {
      expect(byText(".studio-rail button", label)).toBeUndefined();
    }
    // All three entries are listed, both speakers visible by default.
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
    expect(container.textContent).toContain("أهلا بك");
    expect(container.textContent).toContain("بخير الحمد لله");
  });

  it("renders GPU-produced quality flags", async () => {
    await openReview(routedFetch());
    expect(container.querySelector(".flag-chip")?.textContent).toBe("repeated ngram");
  });

  it("renders no removed control anywhere on the screen", async () => {
    await openReview(routedFetch());
    const text = container.textContent || "";
    for (const removed of [
      "Generate transcript",
      "Rediarize",
      "Realign",
      "Overlap",
      "Clips",
      "Golden",
      "Moshi speaker",
      "Rights",
      "Export",
      "Separation",
      "SpeechBrain",
      "Channel-first",
      "Accuracy",
    ]) {
      expect(text).not.toContain(removed);
    }
  });

  it("selecting an entry opens the inspector for editing", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[1]);

    const inspector = container.querySelector(".segment-inspector");
    expect(inspector).toBeTruthy();
    expect(inspector!.querySelector("textarea")!.value).toBe("كيف حالك");
    // Text direction is automatic so Arabic renders right to left.
    expect(inspector!.querySelector("textarea")!.getAttribute("dir")).toBe("auto");
  });

  it("joins same-speaker neighbours and refuses across speakers", async () => {
    await openReview(routedFetch());

    // u2 (speaker A) followed by u3 (speaker B): join must be unavailable.
    await click(container.querySelectorAll(".transcript-entry")[1]);
    let join = byText(".inspector-actions button", "Join with next") as HTMLButtonElement;
    expect(join.disabled).toBe(true);

    // u1 followed by u2, both speaker A: join is offered and merges them.
    await click(container.querySelectorAll(".transcript-entry")[0]);
    join = byText(".inspector-actions button", "Join with next") as HTMLButtonElement;
    expect(join.disabled).toBe(false);
    await click(join);

    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);
    expect(container.textContent).toContain("أهلا بك كيف حالك");
  });

  it("deletes a segment and restores it with undo", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);
    await click(byText(".inspector-actions button", "Delete segment"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);

    await click(byText(".rail-actions button", "Undo"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
  });

  it("adds a segment", async () => {
    await openReview(routedFetch());
    await click(byText(".transcript-header-actions button", "Add segment"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(4);
  });

  it("saves through the existing annotation API and enqueues no GPU job", async () => {
    const fetchMock = routedFetch();
    await openReview(fetchMock);

    await click(container.querySelectorAll(".transcript-entry")[0]);
    await click(byText(".inspector-actions button", "Delete segment"));
    await click(byText(".rail-actions button", "Save now"));
    await flush();

    const put = fetchMock.mock.calls.find((call) => call[1]?.method === "PUT");
    expect(put![0]).toBe("/api/sources/source_1/annotations");
    const body = JSON.parse(String(put![1]!.body));
    expect(body.expected_version).toBe(2);
    expect(body.annotation.transcript).toHaveLength(2);

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(
      urls.filter((url) =>
        /transcribe|rediarize|realign|overlap|generate|clip|export|initialize/.test(url),
      ),
    ).toEqual([]);
  });
});

describe("processing boundary", () => {
  it("offers the one-time Manual or Assisted choice before preparation", async () => {
    const uploaded = { ...sourceDetail, status: "uploaded" };
    await openReview(
      routedFetch((url) =>
        url === "/api/sources/source_1" ? response(uploaded) : undefined,
      ),
    );

    expect(byText(".choice-grid button strong", "Assisted start")).toBeTruthy();
    expect(byText(".choice-grid button strong", "Manual start")).toBeTruthy();
    expect(container.querySelector(".transcript-panel")).toBeNull();
  });

  it("offers a scoped retry after a failed first pass", async () => {
    const failed = { ...sourceDetail, status: "failed" };
    const withFailedJob = {
      ...projectDetail,
      jobs: [
        {
          id: "job_1",
          kind: "initialize",
          source_id: "source_1",
          status: "failed",
          progress: 0,
          message: "",
          error: "GPU ran out of memory",
        },
      ],
    };
    await openReview(
      routedFetch((url) => {
        if (url === "/api/sources/source_1") return response(failed);
        if (url === "/api/projects/project_1") return response(withFailedJob);
        return undefined;
      }),
    );

    expect(container.textContent).toContain("GPU ran out of memory");
    expect(byText(".choice-grid button strong", "Retry assisted preparation")).toBeTruthy();
  });

  it("is read-only while the source is still processing", async () => {
    const processing = { ...sourceDetail, status: "processing" };
    await openReview(
      routedFetch((url) =>
        url === "/api/sources/source_1" ? response(processing) : undefined,
      ),
    );

    expect(container.querySelector(".inline-banner")?.textContent).toContain(
      "Editing unlocks",
    );
    // No editing affordances are offered.
    expect(byText(".transcript-header-actions button", "Add segment")).toBeUndefined();
    await act(async () => {
      container.querySelectorAll<HTMLElement>(".transcript-entry")[0]
        ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    expect(container.querySelector(".inspector-actions")).toBeNull();
  });
});
