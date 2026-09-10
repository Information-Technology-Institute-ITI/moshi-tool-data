export type ReviewCommandId =
  | "save"
  | "undo"
  | "redo"
  | "toggle_playback"
  | "seek_back"
  | "seek_forward"
  | "frame_back"
  | "frame_forward"
  | "previous_segment"
  | "next_segment"
  | "play_segment"
  | "loop_segment"
  | "add_segment"
  | "selection_start"
  | "selection_speaker_a"
  | "selection_speaker_b"
  | "open_palette"
  | "open_shortcuts";

export type ReviewCommandDefinition = {
  id: ReviewCommandId;
  label: string;
  description: string;
  group: "Playback" | "Segments" | "Editing" | "Workspace";
  shortcuts: string[];
};

export type ReviewCommand = ReviewCommandDefinition & {
  enabled: boolean;
  run: () => void;
};

export const REVIEW_COMMANDS: readonly ReviewCommandDefinition[] = [
  { id: "toggle_playback", label: "Play or pause", description: "Toggle the shared media player.", group: "Playback", shortcuts: ["Space"] },
  { id: "seek_back", label: "Seek back 1 second", description: "Move the playhead backward.", group: "Playback", shortcuts: ["ArrowLeft"] },
  { id: "seek_forward", label: "Seek forward 1 second", description: "Move the playhead forward.", group: "Playback", shortcuts: ["ArrowRight"] },
  { id: "frame_back", label: "Previous frame", description: "Move backward by one video frame.", group: "Playback", shortcuts: [","] },
  { id: "frame_forward", label: "Next frame", description: "Move forward by one video frame.", group: "Playback", shortcuts: ["."] },
  { id: "previous_segment", label: "Previous segment", description: "Select and play the previous visible segment.", group: "Segments", shortcuts: ["K"] },
  { id: "next_segment", label: "Next segment", description: "Select and play the next visible segment.", group: "Segments", shortcuts: ["J"] },
  { id: "play_segment", label: "Play selected segment", description: "Play the selected segment once.", group: "Segments", shortcuts: ["P"] },
  { id: "loop_segment", label: "Loop selected segment", description: "Loop the selected segment until stopped.", group: "Segments", shortcuts: ["Shift+P"] },
  { id: "add_segment", label: "Add segment at playhead", description: "Create a two-second segment at the playhead.", group: "Segments", shortcuts: ["N"] },
  { id: "selection_start", label: "Set region start", description: "Start a timeline region at the playhead.", group: "Editing", shortcuts: ["["] },
  { id: "selection_speaker_a", label: "Finish region as speaker A", description: "Finish the pending region for speaker A.", group: "Editing", shortcuts: ["A"] },
  { id: "selection_speaker_b", label: "Finish region as speaker B", description: "Finish the pending region for speaker B.", group: "Editing", shortcuts: ["B"] },
  { id: "save", label: "Save annotation", description: "Create an explicit server revision.", group: "Workspace", shortcuts: ["Mod+S"] },
  { id: "undo", label: "Undo", description: "Undo the last local edit.", group: "Workspace", shortcuts: ["Mod+Z"] },
  { id: "redo", label: "Redo", description: "Redo the last undone edit.", group: "Workspace", shortcuts: ["Mod+Shift+Z", "Mod+Y"] },
  { id: "open_palette", label: "Open command palette", description: "Search every available review action.", group: "Workspace", shortcuts: ["Mod+K"] },
  { id: "open_shortcuts", label: "Keyboard shortcut help", description: "Show the complete keyboard reference.", group: "Workspace", shortcuts: ["?"] },
] as const;

export function isShortcutBlockedTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return !!target.closest(
    "input, textarea, select, button, a[href], [contenteditable='true'], [role='dialog']",
  );
}

function eventShortcut(event: KeyboardEvent): string {
  const parts: string[] = [];
  if (event.ctrlKey || event.metaKey) parts.push("Mod");
  if (event.altKey) parts.push("Alt");
  let key = event.code === "Space" ? "Space" : event.key;
  // Printable shifted punctuation already encodes the modifier in `key`.
  // Keeping another "Shift" here would make the real ? key miss its binding.
  if (event.shiftKey && key !== "?") parts.push("Shift");
  if (key.length === 1 && /[a-z]/i.test(key)) key = key.toUpperCase();
  parts.push(key);
  return parts.join("+");
}

export function commandIdForKeyboardEvent(
  event: KeyboardEvent,
): ReviewCommandId | null {
  if (event.defaultPrevented || isShortcutBlockedTarget(event.target)) return null;
  const shortcut = eventShortcut(event);
  return REVIEW_COMMANDS.find((command) => command.shortcuts.includes(shortcut))?.id ?? null;
}

export function shortcutLabel(shortcut: string): string {
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
  return shortcut.replace("Mod", modifier).replaceAll("+", " + ");
}
