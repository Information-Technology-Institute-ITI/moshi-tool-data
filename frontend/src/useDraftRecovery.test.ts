import { describe, expect, it } from "vitest";
import { draftMatchesRevision, type RecoveryDraft } from "./useDraftRecovery";

describe("draft recovery revision guard", () => {
  const draft = { baseRevision: 8 } as RecoveryDraft;

  it("allows restore only against the exact server base revision", () => {
    expect(draftMatchesRevision(draft, 8)).toBe(true);
    expect(draftMatchesRevision(draft, 9)).toBe(false);
  });
});
