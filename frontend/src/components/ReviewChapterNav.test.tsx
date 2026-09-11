// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReviewChapterSet } from "../productContracts";
import type { TranscriptUtterance } from "../types";
import ReviewChapterNav from "./ReviewChapterNav";
import VirtualTranscriptList from "./VirtualTranscriptList";

const chapterSet: ReviewChapterSet = {
  contract_version: "studio.review/v1",
  id: "set_1",
  source_id: "source_1",
  annotation_version: 2,
  active: true,
  config: {
    contract_version: "studio.review/v1",
    mode: "max_duration",
    max_duration_seconds: 1_800,
    count: null,
    manual_boundaries_samples: [],
    boundary_search_seconds: 30,
  },
  chapters: [
    { id: "one", ordinal: 1, start_sample: 0, end_sample: 43_200_000, boundary_reason: "silence" },
    { id: "two", ordinal: 2, start_sample: 43_200_000, end_sample: 86_400_000, boundary_reason: "segment" },
  ],
};

function segment(id: string, start_sample: number): TranscriptUtterance {
  return {
    id,
    speaker: "A",
    start_sample,
    end_sample: start_sample + 24_000,
    text: id,
    model_text: id,
    model_speaker: "A",
    quality_flags: [],
    alignment_status: "aligned",
    human_verified: false,
    review_candidates: [],
  };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("review chapter navigation", () => {
  it("selects chapters using global time labels", async () => {
    const select = vi.fn();
    const segments = [
      segment("first", 0),
      segment("second", 24_000),
      segment("third", 43_200_000),
    ];
    await act(async () => root.render(
      <ReviewChapterNav
        chapterSet={chapterSet}
        currentId="one"
        segments={segments}
        disabled={false}
        configuring={false}
        onSelect={select}
        onConfigure={vi.fn()}
      />,
    ));
    expect(container.textContent).toContain("1 of 2");
    expect(container.textContent).toContain("2 segments | global 1-2 of 3 | 0:00 to 30:00");
    expect(container.textContent).toContain("Chapter setup: maximum 30 minutes");
    expect(container.querySelector("select")?.textContent).toContain("(2 segments)");
    expect(container.querySelector("select")?.textContent).toContain("(1 segment)");
    const next = container.querySelector<HTMLButtonElement>('[aria-label="Next chapter"]')!;
    await act(async () => next.click());
    expect(select).toHaveBeenCalledWith("two");
  });

  it("recommends one chapter for recordings shorter than thirty minutes", async () => {
    const configure = vi.fn();
    const shortSet = {
      ...chapterSet,
      chapters: [
        { ...chapterSet.chapters[0], end_sample: 2_160_000 },
        { ...chapterSet.chapters[1], start_sample: 2_160_000, end_sample: 4_320_000 },
      ],
    };
    await act(async () => root.render(
      <ReviewChapterNav
        chapterSet={shortSet}
        currentId="one"
        segments={[]}
        disabled={false}
        configuring={false}
        onSelect={vi.fn()}
        onConfigure={configure}
      />,
    ));
    expect(container.textContent).toContain("under 30 minutes");
    await act(async () => (
      Array.from(container.querySelectorAll("button"))
        .find((button) => button.textContent?.trim() === "Use one chapter")!
        .click()
    ));
    expect(configure).toHaveBeenCalledWith(expect.objectContaining({
      mode: "max_duration",
      max_duration_seconds: 1_800,
      count: null,
    }));
  });

  it("keeps the transcript DOM bounded for large chapters", async () => {
    const items = Array.from({ length: 2_000 }, (_, index) => ({ id: `row_${index}` }));
    await act(async () => root.render(
      <VirtualTranscriptList
        items={items}
        empty={null}
        renderItem={(item) => <button>{item.id}</button>}
      />,
    ));
    expect(container.querySelectorAll(".virtual-transcript-row").length).toBeLessThan(30);
    expect(container.querySelector(".virtual-transcript-spacer")).not.toBeNull();
  });
});
