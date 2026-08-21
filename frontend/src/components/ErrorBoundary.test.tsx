// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ErrorBoundary from "./ErrorBoundary";

let container: HTMLDivElement;
let root: Root;

function Boom({ fail }: { fail: boolean }): JSX.Element {
  if (fail) throw new Error("Cannot read properties of null");
  return <p className="fine">Working</p>;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  // React logs the caught error itself; the test asserts on what it renders.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("recovering from a render error", () => {
  it("shows a recovery panel instead of blanking the page", async () => {
    await act(async () => root.render(
      <ErrorBoundary><Boom fail /></ErrorBoundary>,
    ));

    // Regression: an unhandled render error unmounted the whole tree, leaving a
    // white page that only a browser reload recovered from.
    expect(container.textContent).toContain("Something on this screen stopped working");
    expect(container.querySelector(".error-boundary-detail")?.textContent)
      .toContain("Cannot read properties of null");
    expect(container.innerHTML).not.toBe("");
  });

  it("offers a way back that keeps the session", async () => {
    const onReset = vi.fn();
    await act(async () => root.render(
      <ErrorBoundary onReset={onReset} resetLabel="Back to my datasets">
        <Boom fail />
      </ErrorBoundary>,
    ));

    const back = Array.from(container.querySelectorAll("button"))
      .find((node) => node.textContent === "Back to my datasets");
    expect(back).toBeTruthy();
    await act(async () => {
      back!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("renders its children untouched when nothing throws", async () => {
    await act(async () => root.render(
      <ErrorBoundary><Boom fail={false} /></ErrorBoundary>,
    ));
    expect(container.querySelector(".fine")?.textContent).toBe("Working");
    expect(container.querySelector(".error-boundary")).toBeNull();
  });
});
