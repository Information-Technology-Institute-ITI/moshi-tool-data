// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Annotation, AuthUser, TranscriptUtterance } from "./types";

// wavesurfer needs a real audio stack; the review screen is exercised through
// the transcript surface instead, so the player is stubbed out. Its props are
// held so tests can fire the callbacks a real click on the waveform would.
type WaveProps = {
  onRegionClick?: (regionId: string, atSample: number) => void;
  onRegionDelete?: (regionId: string) => void;
  onTimeChange?: (sample: number) => void;
};
const waveProps: { current: WaveProps } = { current: {} };
vi.mock("./components/WaveformEditor", () => ({
  default: (props: WaveProps) => {
    waveProps.current = props;
    return null;
  },
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

  it("joins same-speaker neighbours and runs their texts together", async () => {
    await openReview(routedFetch());

    await click(container.querySelectorAll(".transcript-entry")[0]);
    const join = byText(".inspector-actions button", "Join with next") as HTMLButtonElement;
    expect(join.disabled).toBe(false);
    await click(join);

    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);
    expect(container.textContent).toContain("أهلا بك كيف حالك");
  });

  it("joins across speakers, because neighbours nearly always differ", async () => {
    // Regression: Join with next was disabled unless the next segment had the
    // same speaker, which diarisation almost never produces, so on real
    // material the button could never be pressed.
    await openReview(routedFetch());

    // u2 is speaker A and u3 is speaker B.
    await click(container.querySelectorAll(".transcript-entry")[1]);
    const join = byText(".inspector-actions button", "Join with next") as HTMLButtonElement;
    expect(join.disabled).toBe(false);
    await click(join);

    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);
    expect(container.textContent).toContain("كيف حالك بخير الحمد لله");
  });

  it("offers Join with next on every entry but the last", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[2]);
    const join = byText(".inspector-actions button", "Join with next") as HTMLButtonElement;
    expect(join.disabled).toBe(true);
  });

  it("splits without needing the playhead moved into the segment", async () => {
    // Regression: the split point was read from the live playhead. Selecting a
    // segment parks the playhead exactly on its start and clicking the waveform
    // lands on a speaker region, so Split stayed greyed out on real material.
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const point = container.querySelector<HTMLInputElement>(".split-point input");
    expect(point).toBeTruthy();
    // Defaults to the midpoint of the selected segment, so it is ready to use.
    expect(point!.value).toBe("0.50");

    const split = byText(".inspector-actions button", "Split here") as HTMLButtonElement;
    expect(split.disabled).toBe(false);
    await click(split);

    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(4);
    expect(container.textContent).toContain("0.00–0.50s");
    expect(container.textContent).toContain("0.50–1.00s");
  });

  it("refuses a split point outside the selected segment", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const point = container.querySelector<HTMLInputElement>(".split-point input")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(point, "9.00");
      point.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await flush();

    const split = byText(".inspector-actions button", "Split here") as HTMLButtonElement;
    expect(split.disabled).toBe(true);
  });

  it("selects the segment under the click, not the first one in the region", async () => {
    // Regression: one speaker region routinely spans many segments. Clicking it
    // always selected the earliest, so the point the user clicked was outside
    // the selected segment and Split could not be used on it.
    await openReview(routedFetch());

    // act_1 runs 0-48000 and covers both u1 (0-24000) and u2 (24000-48000).
    await act(async () => waveProps.current.onRegionClick!("act_1", 30_000));
    await flush();

    const selected = container.querySelector(".transcript-entry.selected");
    expect(selected?.textContent).toContain("كيف حالك");
  });

  it("follows the playhead into the segment being split", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[1]);

    await act(async () => waveProps.current.onTimeChange!(36_000));
    await flush();

    const point = container.querySelector<HTMLInputElement>(".split-point input");
    expect(point!.value).toBe("1.50");
  });

  it("steps to the next and previous segment from the inspector", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);

    expect(container.querySelector(".segment-inspector .eyebrow")?.textContent)
      .toContain("1 of 3");
    const previous = byText(".inspector-play button", "‹ Previous") as HTMLButtonElement;
    expect(previous.disabled).toBe(true);

    await click(byText(".inspector-play button", "Next ›"));
    expect(container.querySelector(".segment-inspector textarea")!.textContent)
      .toBe("كيف حالك");
    expect(container.querySelector(".segment-inspector .eyebrow")?.textContent)
      .toContain("2 of 3");

    await click(byText(".inspector-play button", "Next ›"));
    expect(container.querySelector(".segment-inspector .eyebrow")?.textContent)
      .toContain("3 of 3");
    expect((byText(".inspector-play button", "Next ›") as HTMLButtonElement).disabled)
      .toBe(true);

    await click(byText(".inspector-play button", "‹ Previous"));
    expect(container.querySelector(".segment-inspector .eyebrow")?.textContent)
      .toContain("2 of 3");
  });

  it("offers no quality-flag editor on a segment", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const inspector = container.querySelector(".segment-inspector")!;
    expect(inspector.querySelector(".flag-editor")).toBeNull();
    expect(inspector.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
    expect(inspector.textContent).not.toContain("Quality flags");
  });

  it("still shows the flags the model produced, read only", async () => {
    await openReview(routedFetch());
    // u1 carries repeated_ngram from the GPU; it is information, not a control.
    expect(container.querySelector(".flag-chip")?.textContent).toBe("repeated ngram");
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

describe("background job updates do not blank the page", () => {
  /** Stands in for the server-sent-event stream a running job opens. */
  class FakeEventSource {
    static last: FakeEventSource | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    closed = false;
    constructor(public url: string) {
      FakeEventSource.last = this;
    }
    close() {
      this.closed = true;
    }
  }

  const job = {
    id: "job_1",
    kind: "initialize",
    source_id: "source_1",
    status: "queued",
    progress: 0,
    message: "",
    error: null,
  };

  function fetchWithInitialize() {
    const uploaded = { ...sourceDetail, status: "uploaded" };
    return routedFetch((url, init) => {
      if (url === "/api/sources/source_1") return response(uploaded);
      if (url === "/api/sources/source_1/initialize" && init?.method === "POST") {
        return response(job);
      }
      return undefined;
    });
  }

  it("ignores a job finishing for a source the user has left", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchMock = fetchWithInitialize();
    await openReview(fetchMock);

    // Start preparation, which opens the job stream.
    await click(byText(".choice-grid button strong", "Assisted start")?.closest("button"));
    expect(FakeEventSource.last).toBeTruthy();

    // Leave the source before the job finishes.
    await click(byText("button.back", "← Cairo"));
    expect(container.querySelector(".onboarding")).toBeNull();

    const callsBefore = fetchMock.mock.calls.length;
    await act(async () => {
      FakeEventSource.last!.onmessage!({
        data: JSON.stringify({ ...job, status: "complete" }),
      });
    });
    await flush();

    // Regression: the completion handler held the source from the render that
    // started the job, reopened it, and rendered the studio with no project
    // behind it, which threw and left a white page.
    expect(container.querySelector(".error-boundary")).toBeNull();
    expect(container.textContent).toContain("initialize complete");
    // It did not drag the user back into the source it had left.
    const reopened = fetchMock.mock.calls
      .slice(callsBefore)
      .map((call) => String(call[0]))
      .filter((url) => url === "/api/sources/source_1");
    expect(reopened).toEqual([]);
  });

  it("survives a malformed frame on the job stream", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    await openReview(fetchWithInitialize());
    await click(byText(".choice-grid button strong", "Assisted start")?.closest("button"));

    await act(async () => {
      FakeEventSource.last!.onmessage!({ data: "not json" });
    });
    await flush();

    expect(container.querySelector(".error-boundary")).toBeNull();
    expect(FakeEventSource.last!.closed).toBe(false);
  });
});

describe("save conflicts", () => {
  it("shows the conflict dialog instead of crashing the screen", async () => {
    // Regression: the 409 handler read GET /annotations as a bare annotation,
    // but it answers {annotation, revisions}. Every field the dialog reads was
    // undefined, and `conflict.server.transcript.length` threw, which the error
    // boundary then caught as "Cannot read properties of undefined".
    const serverCopy = {
      ...annotation,
      version: 9,
      transcript: [annotation.transcript[0], annotation.transcript[1]],
    };
    const fetchMock = routedFetch((url, init) => {
      if (url === "/api/sources/source_1/annotations" && init?.method === "PUT") {
        return response({ detail: "stale" }, 409);
      }
      if (url === "/api/sources/source_1/annotations") {
        return response({ annotation: serverCopy, revisions: [{ version: 9 }] });
      }
      return undefined;
    });
    await openReview(fetchMock);

    await click(container.querySelectorAll(".transcript-entry")[0]);
    await click(byText(".inspector-actions button", "Delete segment"));
    await click(byText(".rail-actions button", "Save now"));
    await flush();
    await flush();

    expect(container.querySelector(".error-boundary")).toBeNull();
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("This annotation changed elsewhere");
    // Both sides report a real segment count, which is what used to throw.
    expect(dialog!.textContent).toContain("2 segments");
    expect(dialog!.textContent).toContain("revision 9");
  });
});

describe("removing a speaker rectangle from the timeline", () => {
  // act_1 covers 0-48000, which holds u1 and u2 entirely, so it owns both.
  // act_3 matches u3 exactly, so it owns that one.
  const laned = {
    ...sourceDetail,
    annotation: {
      ...annotation,
      activities: [
        { id: "act_1", speaker: "A", start_sample: 0, end_sample: 48_000, origin: "model" },
        { id: "act_3", speaker: "B", start_sample: 48_000, end_sample: 72_000, origin: "model" },
      ],
    },
  };

  function open() {
    return routedFetch((url) =>
      url === "/api/sources/source_1" ? response(laned) : undefined,
    );
  }

  it("asks before removing anything", async () => {
    await openReview(open());
    await act(async () => waveProps.current.onRegionDelete!("act_3"));
    await flush();

    // Regression: a double-click deleted the rectangle silently.
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("Remove speaker B");
    // Nothing has gone yet.
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
  });

  it("cancelling leaves the timeline and the transcript alone", async () => {
    await openReview(open());
    await act(async () => waveProps.current.onRegionDelete!("act_3"));
    await flush();
    await click(byText(".modal-actions button", "Cancel"));

    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
  });

  it("removes the segment the rectangle owned once confirmed", async () => {
    await openReview(open());
    await act(async () => waveProps.current.onRegionDelete!("act_3"));
    await flush();

    // The dialog names what goes with it.
    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("its own transcript segment");
    expect(dialog.querySelector(".region-delete-list")?.textContent)
      .toContain("بخير الحمد لله");

    await click(byText(".modal-actions button", "Remove region and segments"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);
    expect(container.textContent).not.toContain("بخير الحمد لله");
  });

  it("removes every segment a wider rectangle fully contains", async () => {
    await openReview(open());
    await act(async () => waveProps.current.onRegionDelete!("act_1"));
    await flush();

    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("2 transcript segments");
    await click(byText(".modal-actions button", "Remove region and segments"));

    // Only the untouched B segment is left.
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(1);
    expect(container.textContent).toContain("بخير الحمد لله");
  });

  it("keeps a segment that spans more than the rectangle being removed", async () => {
    // u3 runs 48000-72000 but the B lane draws it as two rectangles, so neither
    // owns it on its own. This is the un-split case that must be protected.
    const split = {
      ...laned,
      annotation: {
        ...laned.annotation,
        activities: [
          { id: "b_left", speaker: "B", start_sample: 48_000, end_sample: 60_000, origin: "model" },
          { id: "b_right", speaker: "B", start_sample: 60_000, end_sample: 72_000, origin: "model" },
        ],
      },
    };
    await openReview(
      routedFetch((url) =>
        url === "/api/sources/source_1" ? response(split) : undefined,
      ),
    );
    await act(async () => waveProps.current.onRegionDelete!("b_left"));
    await flush();

    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("No transcript segment belongs to this region");
    await click(byText(".modal-actions button", "Remove region"));

    // The transcript is untouched; only the rectangle went.
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
    expect(container.textContent).toContain("بخير الحمد لله");
  });

  it("undo restores the rectangle and its segment together", async () => {
    await openReview(open());
    await act(async () => waveProps.current.onRegionDelete!("act_3"));
    await flush();
    await click(byText(".modal-actions button", "Remove region and segments"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);

    await click(byText(".rail-actions button", "Undo"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(3);
    expect(container.textContent).toContain("بخير الحمد لله");
  });
});

describe("speaker turns are divided automatically on open", () => {
  const S = 24_000;
  // u1 runs 0-3s but the A lane draws it as two rectangles with a gap.
  const spanning = {
    ...sourceDetail,
    duration_samples: 4 * S,
    annotation: {
      ...annotation,
      transcript: [utterance("u1", 0, 3 * S, "A", "hello there friend")],
      activities: [
        { id: "r1", speaker: "A", start_sample: 0, end_sample: 1 * S, origin: "model" },
        { id: "r2", speaker: "A", start_sample: 2 * S, end_sample: 3 * S, origin: "model" },
      ],
      aligned_words: [
        { word: "hello", start: 0.1, end: 0.8, speaker: "A" },
        { word: "there", start: 2.1, end: 2.4, speaker: "A" },
        { word: "friend", start: 2.5, end: 2.9, speaker: "A" },
      ],
    },
  };

  function open() {
    return routedFetch((url) =>
      url === "/api/sources/source_1" ? response(spanning) : undefined,
    );
  }

  it("divides the segment without the user pressing anything", async () => {
    await openReview(open());

    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(2);
    expect(container.textContent).toContain("0.00–1.00s");
    expect(container.textContent).toContain("2.00–3.00s");
    // Words land on the rectangle they were spoken in.
    const entries = container.querySelectorAll(".transcript-entry");
    expect(entries[0].textContent).toContain("hello");
    expect(entries[1].textContent).toContain("there friend");
  });

  it("says what it did, and undo puts it back", async () => {
    await openReview(open());
    expect(container.querySelector(".banner.success")?.textContent)
      .toContain("divided to match the speaker turns");

    await click(byText(".rail-actions button", "Undo"));
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(1);
    expect(container.textContent).toContain("hello there friend");
  });

  it("offers no Split by speaker turns button anywhere", async () => {
    await openReview(open());
    await click(container.querySelectorAll(".transcript-entry")[0]);
    expect(container.textContent).not.toContain("Split by speaker turns");
  });

  it("saves the divided transcript", async () => {
    const fetchMock = open();
    await openReview(fetchMock);
    await click(byText(".rail-actions button", "Save now"));
    await flush();

    const put = fetchMock.mock.calls.find((call) => call[1]?.method === "PUT");
    const body = JSON.parse(String(put![1]!.body));
    expect(body.annotation.transcript).toHaveLength(2);
    // The lanes are the input, so they are unchanged.
    expect(body.annotation.activities).toHaveLength(2);
  });

  it("leaves a source alone while it is still processing", async () => {
    await openReview(
      routedFetch((url) =>
        url === "/api/sources/source_1"
          ? response({ ...spanning, status: "processing" })
          : undefined,
      ),
    );
    // Read-only sources are never rewritten behind the reviewer's back.
    expect(container.querySelectorAll(".transcript-entry")).toHaveLength(1);
  });
});

describe("typing does not save on every keystroke", () => {
  function type(area: HTMLTextAreaElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!;
    setter.call(area, value);
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function puts(fetchMock: ReturnType<typeof routedFetch>) {
    return fetchMock.mock.calls.filter((call) => call[1]?.method === "PUT");
  }

  it("holds the text while the reviewer writes", async () => {
    // Regression: each letter armed the autosave, so a sentence became a pile
    // of revisions on the server.
    const fetchMock = routedFetch();
    await openReview(fetchMock);
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const area = container.querySelector("textarea")!;
    for (const value of ["ك", "كي", "كيف", "كيف "]) {
      await act(async () => type(area, value));
    }
    // Wait past the autosave debounce: the point is that no timer was ever
    // armed, not merely that one has not fired yet.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
    });
    await flush();

    expect(puts(fetchMock)).toHaveLength(0);
    // What was typed is on screen and recoverable, just not sent yet.
    expect(container.querySelector("textarea")!.value).toBe("كيف ");
  });

  it("saves once when the box loses focus", async () => {
    const fetchMock = routedFetch();
    await openReview(fetchMock);
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const area = container.querySelector("textarea")!;
    await act(async () => type(area, "مرحبا"));
    // React's onBlur is delivered through the native focusout event.
    await act(async () => area.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    await flush();

    const calls = puts(fetchMock);
    expect(calls).toHaveLength(1);
    const body = JSON.parse(String(calls[0][1]!.body));
    expect(body.annotation.transcript[0].text).toBe("مرحبا");
  });

  it("saves when moving to another segment", async () => {
    const fetchMock = routedFetch();
    await openReview(fetchMock);
    await click(container.querySelectorAll(".transcript-entry")[0]);

    await act(async () => type(container.querySelector("textarea")!, "نص جديد"));
    await click(container.querySelectorAll(".transcript-entry")[1]);
    await flush();

    const calls = puts(fetchMock);
    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0][1]!.body)).annotation.transcript[0].text)
      .toBe("نص جديد");
  });

  it("undoes a whole run of typing in one step", async () => {
    await openReview(routedFetch());
    await click(container.querySelectorAll(".transcript-entry")[0]);

    const area = container.querySelector("textarea")!;
    for (const value of ["a", "ab", "abc"]) {
      await act(async () => type(area, value));
    }
    await flush();

    await click(byText(".rail-actions button", "Undo"));
    // One undo, not three.
    expect(container.querySelector("textarea")!.value).toBe("أهلا بك");
  });
});
