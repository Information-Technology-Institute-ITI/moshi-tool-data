import { afterEach, describe, expect, it, vi } from "vitest";
import { sampleId, seconds, triggerGpuCheck } from "./api";

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

describe("locally minted ids", () => {
  const realCrypto = globalThis.crypto;

  afterEach(() => {
    Object.defineProperty(globalThis, "crypto", {
      value: realCrypto,
      configurable: true,
      writable: true,
    });
  });

  it("mints a prefixed 32-character hex id", () => {
    expect(sampleId("utterance")).toMatch(/^utterance_[0-9a-f]{32}$/);
  });

  it("works without crypto.randomUUID, which plain HTTP does not provide", () => {
    // Regression: the deployment is served over HTTP, where randomUUID is
    // undefined, so adding or splitting a segment threw and blanked the screen.
    Object.defineProperty(globalThis, "crypto", {
      value: { getRandomValues: realCrypto.getRandomValues.bind(realCrypto) },
      configurable: true,
      writable: true,
    });
    expect(sampleId("activity")).toMatch(/^activity_[0-9a-f]{32}$/);
  });

  it("works with no Web Crypto at all", () => {
    Object.defineProperty(globalThis, "crypto", {
      value: undefined,
      configurable: true,
      writable: true,
    });
    expect(sampleId("activity")).toMatch(/^activity_[0-9a-f]{32}$/);
  });

  it("does not repeat itself", () => {
    const ids = new Set(Array.from({ length: 500 }, () => sampleId("u")));
    expect(ids.size).toBe(500);
  });
});
