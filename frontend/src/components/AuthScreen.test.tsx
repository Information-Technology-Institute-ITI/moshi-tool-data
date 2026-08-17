// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AuthScreen from "./AuthScreen";

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

async function setInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
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

describe("account access", () => {
  it("creates a pending account without exposing account existence", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      message: "If the address can be registered, an activation email has been sent.",
    }, 202));
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<AuthScreen onAuthenticated={vi.fn()} />));

    const createButton = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Create account",
    );
    await act(async () => createButton?.click());
    const inputs = container.querySelectorAll("input");
    await setInput(inputs[0], "Alexandria Reviewer");
    await setInput(inputs[1], "reviewer@example.test");
    await setInput(inputs[2], "secure-password");
    await act(async () => {
      container.querySelector("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });
    await flush();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/signup",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
      }),
    );
    expect(container.textContent).toContain("activation email has been sent");
  });

  it("activates a fragment token without putting it in a GET request", async () => {
    window.history.replaceState(null, "", "/#/activate?token=secret-token-value");
    const fetchMock = vi.fn().mockResolvedValue(response({
      user: {
        id: "user-1",
        email: "reviewer@example.test",
        display_name: "Reviewer",
        role: "user",
        status: "active",
      },
    }));
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<AuthScreen onAuthenticated={vi.fn()} />));
    await flush();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/activate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ token: "secret-token-value" }),
      }),
    );
    expect(window.location.hash).toBe("");
    expect(container.textContent).toContain("Account activated");
  });

  it("returns the authenticated user after signin", async () => {
    const user = {
      id: "user-1",
      email: "reviewer@example.test",
      display_name: "Reviewer",
      role: "user" as const,
      status: "active" as const,
    };
    const authenticated = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ user })));
    await act(async () => root.render(
      <AuthScreen onAuthenticated={authenticated} />,
    ));

    const inputs = container.querySelectorAll("input");
    await setInput(inputs[0], user.email);
    await setInput(inputs[1], "secure-password");
    await act(async () => {
      container.querySelector("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });
    await flush();

    expect(authenticated).toHaveBeenCalledWith(user);
  });
});
