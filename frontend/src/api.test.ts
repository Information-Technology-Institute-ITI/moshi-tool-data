import { afterEach, describe, expect, it, vi } from "vitest";
import { seconds, triggerGpuCheck } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("time formatting", () => {
  it("uses the canonical 24 kHz sample clock", () => {
    expect(seconds(24_000)).toBe("1.00");
    expect(seconds(36_000)).toBe("1.50");
  });
});

describe("GPU API errors", () => {
  it("preserves Retry-After without sending browser identity", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      statusText: "Too Many Requests",
      headers: { get: (name: string) => name.toLocaleLowerCase() === "retry-after" ? "90" : null },
      json: async () => ({ detail: "Manual functional-check cooldown is active." }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(triggerGpuCheck()).rejects.toMatchObject({
      status: 429,
      retryAfterSeconds: 90,
      message: "Manual functional-check cooldown is active.",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/system/gpu/checks",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: undefined,
      }),
    );
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.stringify(options.headers)).not.toMatch(/user|identity/i);
  });
});
