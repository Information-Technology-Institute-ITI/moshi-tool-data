// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import {
  commandIdForKeyboardEvent,
  isShortcutBlockedTarget,
  REVIEW_COMMANDS,
} from "./reviewCommands";

function resolve(init: KeyboardEventInit, target: HTMLElement = document.body) {
  let result = null as ReturnType<typeof commandIdForKeyboardEvent>;
  const listener = (event: KeyboardEvent) => {
    result = commandIdForKeyboardEvent(event);
  };
  target.addEventListener("keydown", listener, { once: true });
  target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init }));
  return result;
}

describe("review command registry", () => {
  it("has unique command ids and at least one binding per command", () => {
    expect(new Set(REVIEW_COMMANDS.map((command) => command.id)).size)
      .toBe(REVIEW_COMMANDS.length);
    expect(REVIEW_COMMANDS.every((command) => command.shortcuts.length > 0)).toBe(true);
  });

  it("normalizes the main review bindings", () => {
    expect(resolve({ code: "Space", key: " " })).toBe("toggle_playback");
    expect(resolve({ key: "j" })).toBe("next_segment");
    expect(resolve({ key: "P", shiftKey: true })).toBe("loop_segment");
    expect(resolve({ key: "?", shiftKey: true })).toBe("open_shortcuts");
    expect(resolve({ key: "s", ctrlKey: true })).toBe("save");
  });

  it("does not intercept typing or native controls", () => {
    for (const element of [
      document.createElement("input"),
      document.createElement("textarea"),
      document.createElement("select"),
      document.createElement("button"),
    ]) {
      document.body.append(element);
      expect(isShortcutBlockedTarget(element)).toBe(true);
      expect(resolve({ key: "j" }, element)).toBeNull();
      element.remove();
    }
  });

  it("does not dispatch commands from an open dialog", () => {
    const dialog = document.createElement("section");
    dialog.setAttribute("role", "dialog");
    const child = document.createElement("div");
    dialog.append(child);
    document.body.append(dialog);
    expect(resolve({ code: "Space", key: " " }, child)).toBeNull();
    dialog.remove();
  });
});
