// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AuthUser, Project } from "./types";

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const regular: AuthUser = {
  id: "user_regular",
  email: "hamza@example.com",
  display_name: "Hamza Z",
  role: "user",
  status: "active",
};

const administrator: AuthUser = {
  id: "user_admin",
  email: "admin@example.com",
  display_name: "Admin One",
  role: "admin",
  status: "active",
};

const ownDataset: Project = {
  id: "proj_owned",
  name: "Cairo conversations",
  language: "ar-EG",
  source_count: 3,
  ready_sources: 1,
  owner_user_id: "user_admin",
  owner: { id: "user_admin", display_name: "Admin One", email: "admin@example.com" },
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-10T10:00:00Z",
};

const otherDataset: Project = {
  id: "proj_other",
  name: "Alexandria podcast",
  language: "ar-EG",
  source_count: 7,
  ready_sources: 5,
  owner_user_id: "user_regular",
  owner: { id: "user_regular", display_name: "Hamza Z", email: "hamza@example.com" },
  created_at: "2026-08-02T10:00:00Z",
  updated_at: "2026-08-11T10:00:00Z",
};

/** Routes the App's requests without assuming call order. */
function routedFetch(options: {
  user: AuthUser | null;
  projects: Project[];
  onCall?: (url: string, init?: RequestInit) => Response | undefined;
}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const custom = options.onCall?.(url, init);
    if (custom) return custom;
    if (url === "/api/auth/me") {
      return response({ user: options.user, required: true });
    }
    if (url === "/api/projects" && (!init || !init.method || init.method === "GET")) {
      return response({ projects: options.projects });
    }
    if (url === "/api/admin/users") {
      return response({
        users: [
          { id: "user_admin", display_name: "Admin One", email: "admin@example.com" },
          { id: "user_regular", display_name: "Hamza Z", email: "hamza@example.com" },
        ],
      });
    }
    if (url === "/api/auth/signout") return response({ signed_out: true });
    return response({}, 404);
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function findByText(selector: string, text: string): HTMLElement | undefined {
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

async function setInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await flush();
}

/** Signs in and lands on the dataset library. */
async function openLibrary() {
  await act(async () => root.render(<App />));
  await flush();
  await click(findByText("button.intro-nav-action", "Open workspace"));
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

describe("private dataset library", () => {
  it("shows a regular user My datasets with no admin or GPU surface", async () => {
    const fetchMock = routedFetch({ user: regular, projects: [otherDataset] });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();

    expect(container.textContent).toContain("My datasets");
    expect(container.textContent).not.toContain("All datasets");
    expect(container.querySelector(".scope-toggle")).toBeNull();
    expect(container.querySelector(".library-filters")).toBeNull();
    expect(findByText("button.system-nav", "GPU status")).toBeUndefined();

    // A regular user must never request the administrator user list.
    const requested = fetchMock.mock.calls.map((call) => call[0]);
    expect(requested).not.toContain("/api/admin/users");
  });

  it("does not render owner identity on cards for a regular user", async () => {
    vi.stubGlobal("fetch", routedFetch({ user: regular, projects: [otherDataset] }));

    await openLibrary();

    // Owner metadata is an administrator affordance; the card must not carry it
    // even though the signed-in user's own identity appears in the top bar.
    expect(container.querySelector(".project-owner")).toBeNull();
    const grid = container.querySelector(".project-grid")!;
    expect(grid.textContent).toContain("Alexandria podcast");
    expect(grid.textContent).not.toContain("hamza@example.com");
    expect(grid.textContent).not.toContain("Hamza Z");
  });

  it("gives an administrator a scope toggle, owner metadata and GPU navigation", async () => {
    vi.stubGlobal("fetch", routedFetch({
      user: administrator,
      projects: [ownDataset, otherDataset],
    }));

    await openLibrary();

    expect(findByText("button.system-nav", "GPU status")).toBeTruthy();
    // Defaults to the administrator's own datasets.
    expect(container.textContent).toContain("Cairo conversations");
    expect(container.textContent).not.toContain("Alexandria podcast");

    await click(findByText(".scope-toggle button", "All datasets"));

    expect(container.textContent).toContain("Alexandria podcast");
    expect(container.querySelector(".project-owner")).toBeTruthy();
    expect(container.textContent).toContain("hamza@example.com");
  });

  it("filters all datasets by owner name, email and dataset name", async () => {
    vi.stubGlobal("fetch", routedFetch({
      user: administrator,
      projects: [ownDataset, otherDataset],
    }));

    await openLibrary();
    await click(findByText(".scope-toggle button", "All datasets"));

    const search = container.querySelector<HTMLInputElement>(
      '.library-filters input[type="search"]',
    )!;
    await setInput(search, "hamza@example.com");
    expect(container.textContent).toContain("Alexandria podcast");
    expect(container.textContent).not.toContain("Cairo conversations");

    await setInput(search, "Cairo");
    expect(container.textContent).toContain("Cairo conversations");
    expect(container.textContent).not.toContain("Alexandria podcast");
  });

  it("creates a dataset without sending an owner or role", async () => {
    const fetchMock = routedFetch({
      user: regular,
      projects: [],
      onCall: (url, init) =>
        url === "/api/projects" && init?.method === "POST"
          ? response({ id: "proj_new" }, 201)
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();

    const nameInput = container.querySelector<HTMLInputElement>(".create-card input")!;
    await setInput(nameInput, "New dataset");
    await act(async () => {
      container
        .querySelector(".create-card")!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await flush();

    const post = fetchMock.mock.calls.find(
      (call) => call[0] === "/api/projects" && call[1]?.method === "POST",
    );
    expect(post).toBeTruthy();
    const body = JSON.parse(String(post![1]!.body));
    expect(body).toEqual({ name: "New dataset", language: "ar-EG" });
    expect(body).not.toHaveProperty("owner_user_id");
    expect(body).not.toHaveProperty("role");
  });
});

describe("dataset deletion", () => {
  it("requires the exact dataset name and confirms with the project id", async () => {
    const fetchMock = routedFetch({
      user: regular,
      projects: [otherDataset],
      onCall: (url, init) =>
        init?.method === "DELETE"
          ? response({ deleted: otherDataset.id, recoverable: false, cleanup_state: "complete" })
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(findByText(".project-card-actions button", "Delete"));

    const confirm = findByText(".modal-actions button", "Delete forever") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    const input = container.querySelector<HTMLInputElement>(".modal input")!;
    await setInput(input, "Alexandria");
    expect(
      (findByText(".modal-actions button", "Delete forever") as HTMLButtonElement).disabled,
    ).toBe(true);

    await setInput(input, otherDataset.name);
    await click(findByText(".modal-actions button", "Delete forever"));

    const call = fetchMock.mock.calls.find((entry) => entry[1]?.method === "DELETE");
    expect(call![0]).toBe(`/api/projects/${otherDataset.id}`);
    expect(
      (call![1]!.headers as Record<string, string>)["x-confirm-delete"],
    ).toBe(otherDataset.id);
  });
});

describe("keyboard behaviour", () => {
  it("closes the delete dialog on Escape without deleting", async () => {
    const fetchMock = routedFetch({ user: regular, projects: [otherDataset] });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(findByText(".project-card-actions button", "Delete"));
    expect(container.querySelector(".modal")).toBeTruthy();

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    await flush();

    expect(container.querySelector(".modal")).toBeNull();
    expect(fetchMock.mock.calls.some((entry) => entry[1]?.method === "DELETE")).toBe(false);
  });
});

describe("ownership transfer", () => {
  it("shows both owners and patches the protected admin route", async () => {
    const fetchMock = routedFetch({
      user: administrator,
      projects: [ownDataset],
      onCall: (url, init) =>
        init?.method === "PATCH" ? response({ ...ownDataset, owner_user_id: "user_regular" }) : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(findByText(".project-card-actions button", "Transfer owner"));

    expect(container.querySelector(".transfer-summary")!.textContent).toContain("Admin One");

    const confirm = findByText(".modal-actions button", "Transfer dataset") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    const select = container.querySelector<HTMLSelectElement>(".modal select")!;
    // The current owner is not offered as a transfer target.
    expect(Array.from(select.options).map((option) => option.value)).not.toContain("user_admin");

    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLSelectElement.prototype,
        "value",
      )?.set;
      setter?.call(select, "user_regular");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();

    await click(findByText(".modal-actions button", "Transfer dataset"));

    const call = fetchMock.mock.calls.find((entry) => entry[1]?.method === "PATCH");
    expect(call![0]).toBe(`/api/admin/projects/${ownDataset.id}/owner`);
    expect(JSON.parse(String(call![1]!.body))).toEqual({ owner_user_id: "user_regular" });
  });
});

describe("privacy-preserving errors", () => {
  it("returns to the library on 404 without revealing the dataset exists", async () => {
    const fetchMock = routedFetch({
      user: regular,
      projects: [otherDataset],
      onCall: (url) =>
        url === `/api/projects/${otherDataset.id}`
          ? response({ detail: "Not found" }, 404)
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(container.querySelector(".project-card-open"));

    const banner = container.querySelector(".banner.error")!;
    expect(banner.textContent).toContain("no longer available in your workspace");
    expect(banner.textContent).not.toContain("permission");
    expect(banner.textContent).not.toContain("owner");
    // Still on the library, not inside a project workspace.
    expect(container.querySelector(".library-page")).toBeTruthy();
  });

  it("sends the user back to sign-in on 401", async () => {
    const fetchMock = routedFetch({
      user: regular,
      projects: [otherDataset],
      onCall: (url) =>
        url === `/api/projects/${otherDataset.id}`
          ? response({ detail: "Sign in is required" }, 401)
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(container.querySelector(".project-card-open"));

    expect(container.querySelector(".library-page")).toBeNull();
    expect(container.textContent).toContain("Sign in");
  });

  it("explains that admin access is required on 403", async () => {
    const fetchMock = routedFetch({
      user: regular,
      projects: [otherDataset],
      onCall: (url) =>
        url === `/api/projects/${otherDataset.id}`
          ? response({ detail: "Administrator access is required" }, 403)
          : undefined,
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(container.querySelector(".project-card-open"));

    expect(container.querySelector(".banner.error")!.textContent).toContain(
      "Administrator access is required",
    );
  });
});

describe("sign out", () => {
  it("clears the library and cached administrator users", async () => {
    const fetchMock = routedFetch({
      user: administrator,
      projects: [ownDataset, otherDataset],
    });
    vi.stubGlobal("fetch", fetchMock);

    await openLibrary();
    await click(findByText(".scope-toggle button", "All datasets"));
    expect(container.textContent).toContain("Alexandria podcast");

    await click(findByText("button.system-nav", "Sign out"));

    expect(container.textContent).not.toContain("Alexandria podcast");
    expect(container.querySelector(".library-page")).toBeNull();
    expect(findByText("button.system-nav", "GPU status")).toBeUndefined();
  });
});
