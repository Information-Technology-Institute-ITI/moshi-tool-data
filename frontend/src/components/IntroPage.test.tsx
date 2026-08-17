// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import IntroPage from "./IntroPage";

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

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

describe("intro page", () => {
  it("leads with signin without a local bypass", async () => {
    const signIn = vi.fn();
    await act(async () => root.render(
      <IntroPage
        user={null}
        onSignIn={signIn}
        onEnter={vi.fn()}
      />,
    ));

    expect(container.querySelector("h1")?.textContent).toBe("Moshi Dataset Studio");
    expect(container.textContent).not.toContain("Open local workspace");
    const button = Array.from(container.querySelectorAll("button")).find(
      (value) => value.textContent === "Sign in",
    );
    await act(async () => button?.click());
    expect(signIn).toHaveBeenCalledOnce();
  });

  it("opens the workspace directly for an authenticated user", async () => {
    const enter = vi.fn();
    await act(async () => root.render(
      <IntroPage
        user={{
          id: "user-1",
          email: "reviewer@example.test",
          display_name: "Reviewer",
          role: "user",
          status: "active",
        }}
        onSignIn={vi.fn()}
        onEnter={enter}
      />,
    ));
    const button = Array.from(container.querySelectorAll("button")).find(
      (value) => value.textContent === "Open workspace",
    );
    await act(async () => button?.click());
    expect(enter).toHaveBeenCalledOnce();
  });
});
