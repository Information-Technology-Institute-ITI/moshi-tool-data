import { describe, expect, it } from "vitest";
import { seconds } from "./api";

describe("time formatting", () => {
  it("uses the canonical 24 kHz sample clock", () => {
    expect(seconds(24_000)).toBe("1.00");
    expect(seconds(36_000)).toBe("1.50");
  });
});
