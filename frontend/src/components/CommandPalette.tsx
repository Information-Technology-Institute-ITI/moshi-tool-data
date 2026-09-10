import { useEffect, useRef } from "react";
import { shortcutLabel, type ReviewCommand } from "../reviewCommands";

export default function CommandPalette({
  open,
  mode,
  commands,
  onClose,
}: {
  open: boolean;
  mode: "palette" | "help";
  commands: ReviewCommand[];
  onClose: () => void;
}) {
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => dialog.current?.focus());
  }, [open, mode]);
  if (!open) return null;
  const firstEnabled = commands.find((command) => command.enabled);
  return (
    <div className="modal-backdrop command-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialog}
        className="modal command-palette"
        role="dialog"
        tabIndex={-1}
        aria-modal="true"
        aria-labelledby="command-palette-title"
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
          if (event.key === "Enter" && firstEnabled) {
            event.preventDefault();
            onClose();
            firstEnabled.run();
          }
        }}
      >
        <header className="command-heading">
          <div>
            <span className="eyebrow">Review workspace</span>
            <h2 id="command-palette-title">
              {mode === "help" ? "Keyboard shortcuts" : "Command palette"}
            </h2>
          </div>
          <button type="button" aria-label="Close command palette" onClick={onClose}>×</button>
        </header>
        <p className="command-scroll-hint">Scroll to see all available commands.</p>
        <div className="command-list" tabIndex={0} aria-label="Available review commands">
          {commands.map((command) => (
            <button
              type="button"
              key={command.id}
              disabled={!command.enabled}
              onClick={() => {
                onClose();
                command.run();
              }}
            >
              <span>
                <strong>{command.label}</strong>
                <small>{command.description}</small>
              </span>
              <span className="command-shortcuts">
                {command.shortcuts.map((shortcut) => (
                  <kbd key={shortcut}>{shortcutLabel(shortcut)}</kbd>
                ))}
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
