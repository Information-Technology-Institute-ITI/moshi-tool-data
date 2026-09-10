# P2 Review Layout, Commands, and Keyboard Access

Status: implementation complete; awaiting local UI acceptance  
Implemented: 2026-09-09  
Playback contract: `studio.review/v1`

## Outcome

The review screen now has a stable interaction shell built around three responsibilities:

- A compact sticky player card owns video, waveform, transport, timecode, and speaker lanes.
- A grouped left tool rail owns save/history, selected-segment editing, speaker-region creation,
  overlap controls, and exclusion editing.
- A separate segments card sits directly below the player and remains the primary scrollable review
  surface. On narrow screens, the rail becomes an off-canvas drawer so it cannot displace or cover
  the transcript while closed.

The P1 playback controller remains the only media transport. P2 routes keyboard transport commands
through the player's imperative adapter instead of installing a second global playback listener.

The accepted layout refinement uses a non-overlapping viewport workspace on desktop: the compact
player occupies the top region and the segments card fills the remaining space with its own scroll.
At wide desktop sizes, video and the waveform controls sit side by side. Explicit Pause controls are
available in both the main transport and the left rail.

Speaker timeline lanes span the complete player-card width beneath that media row. The video is
slightly enlarged while the transcript segments card continues to own the remaining workspace height.

## Component boundaries

- `ReviewPlayerCard.tsx` supplies the sticky, non-obstructive media container.
- `ReviewToolRail.tsx` owns grouped workspace controls and responsive drawer affordances.
- `TranscriptPanel.tsx` owns transcript navigation and mounts the selected-segment inspector into the
  rail without changing its timing-validation or prepare-before-save behavior.
- `WaveformEditor.tsx` keeps transport controls beside the player and mounts annotation/exclusion
  tools into the rail. It exposes one command surface for play/pause, seeking, and region creation.
- `CommandPalette.tsx` supplies a searchable command dialog and shortcut reference.
- `reviewCommands.ts` is the single registry for ids, labels, descriptions, groups, default bindings,
  event normalization, and protected keyboard contexts.

## Default keyboard commands

| Action | Binding |
| --- | --- |
| Play or pause | `Space` |
| Seek one second | `Left` / `Right` |
| Seek one video frame | `,` / `.` |
| Previous / next visible segment | `K` / `J` |
| Play / loop selected segment | `P` / `Shift+P` |
| Add segment at playhead | `N` |
| Start region; finish as A or B | `[` then `A` or `B` |
| Save | `Ctrl/Cmd+S` |
| Undo / redo | `Ctrl/Cmd+Z` / `Ctrl/Cmd+Shift+Z` |
| Command palette | `Ctrl/Cmd+K` |
| Shortcut help | `?` |

With no current selection, `J` starts at the first visible transcript segment and `K` starts at the
last. Segment navigation respects the active time filter.

## Keyboard safety and layering

- Review shortcuts do not dispatch from inputs, textareas, selects, buttons, links,
  content-editable elements, or dialogs.
- Disabled commands still suppress unsafe browser defaults such as opening the browser Save dialog,
  but do not mutate review state.
- The command dialog is above the sticky player, supports search and Escape, and exposes only enabled
  actions for execution.
- Desktop keeps the rail visible. At 900 px and below, the closed rail is both translated and hidden
  from keyboard focus; a backdrop and close control dismiss the open drawer.
- The sticky player has a viewport-bounded scrolling area and a lower stacking layer than dialogs.
  At phone width it returns to normal document flow so the transcript is never trapped below it.
- The player is capped to less than half the desktop viewport. The transcript has its own bordered
  card directly below it, so media and segments remain visually distinct while reviewing.

## Deferred decision

The server-save criteria, revision frequency, checkpoint grouping, and revision-history presentation
will be discussed after the current interface updates are finished. P2 does not delete, merge, or
otherwise change any stored annotation revision.

The maximum number of segments displayed in one review chapter/list window is also deferred to P3,
where it will be chosen from real long-source usability measurements alongside the 30-minute chapter
limit and transcript virtualization.

## Verification

| Check | Result |
| --- | --- |
| Frontend tests | 198 passed across 12 files |
| Command registry tests | Bindings, shifted punctuation, uniqueness, and protected targets covered |
| Review integration | Keyboard start/step, input safety, palette/help, Ctrl/Cmd+S, drawer, and inspector portal covered |
| Player component | Rail portal and imperative command surface covered |
| Production build | TypeScript and Vite passed |
| Python regression suite | 235 passed |

P2 changes only frontend composition and interaction behavior. It requires no database migration and
does not change annotation persistence or transcript-protection formats.
