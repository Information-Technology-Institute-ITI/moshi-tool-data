# P3 Review Chapters and Long-Source Performance

Status: implementation and automated gate complete  
Implemented: 2026-09-10  
Contract: `studio.review/v1`

## Outcome

Multi-hour sources now open as persisted review chapters without cutting or rewriting media. The
default mode creates chapters no longer than 30 minutes. Reviewers can instead request an exact
chapter count, move with previous/next controls, select a chapter directly, and see its global time
range and segment count. The current chapter is restored from both the URL and browser session.

All stored annotation, activity, exclusion, and word times remain global 24 kHz sample positions.
Chapters are rendering/navigation windows only.

## Persistence and APIs

Additive schema migration 8 creates:

- `review_chapter_sets`, retaining prior configurations while allowing one active set per source;
- `review_chapters`, with contiguous global sample ranges and persisted boundary reasons;
- `chapter_reviews`, keyed by chapter and reviewer with annotation version and review status.

The API supports reading and replacing a source chapter configuration, saving reviewer chapter
status, and reading bounded waveform peak windows. Reconfiguration is transactional: the new set
and all of its chapters are inserted before it becomes the single active set.

## Safe boundary generation

- Maximum-duration mode defaults to 1,800 seconds.
- Count mode returns exactly the requested number when enough safe boundaries exist.
- Silence boundaries within the configured search window are preferred.
- Transcript edges and a bounded safe grid provide fallbacks.
- A candidate is rejected when it falls strictly inside any transcript segment or aligned word.
- Boundary lookup uses sorted ranges and binary search, avoiding quadratic work on long transcripts.
- Each chapter stores why its ending was selected: source edge, silence, segment-safe boundary, or
  manual boundary.

## Bounded browser work

- WaveSurfer receives precomputed peak values and a duration, so waveform drawing does not decode
  the complete canonical WAV in the browser.
- New peak artifacts contain a compact whole-source overview and a detailed level capped at 200,000
  pairs. The peak-window API selects the best available level and aggregates it to the caller's
  requested maximum.
- A compact chapter strip consumes a maximum of 900 detailed pairs and supports direct seeking.
- The main waveform, speaker lanes, exclusion lane, and transcript are all scoped to the active
  chapter. Their visible positions are chapter-relative, while playback and stored annotations keep
  unchanged global source timestamps.
- Only segments intersecting the active chapter enter the transcript view.
- The transcript list measures variable row heights and renders only the visible rows plus six rows
  of overscan on each side. RTL text continues to use automatic direction detection.
- Keyboard selection scrolls the virtual viewport to the selected row.
- Dirty state is explicit. Whole-annotation serialization occurs on edits, undo/redo, and Save—not
  during playhead updates.
- The waveform keeps exact high-frequency playback state locally; the full review screen receives a
  throttled playhead update approximately every 160 ms.

The exact product limit for the maximum number of segments in one chapter remains deliberately
deferred until real-source usability testing, as previously agreed. DOM size is already bounded by
virtualization regardless of that future limit.

## Database safety

Migration 8 was rehearsed on a transactionally consistent copy of the live catalog before it was
applied. Verification results were identical before and after migration:

| Measure | Before | After |
| --- | ---: | ---: |
| Schema version | 7 | 8 |
| Sources | 54 | 54 |
| Annotation revisions | 2,472 | 2,472 |

The pre-migration rehearsal/rollback catalog is stored at
`.runtime/p3-migration-rehearsal.sqlite3`. The previously created transcript-protection snapshot
remains unchanged. External replication of protected backups is still pending.

## Verification

| Check | Result |
| --- | --- |
| Backend full regression suite | 241 passed |
| Focused P3/backend contract suite | 19 passed |
| Frontend suite | 200 passed across 13 files |
| Production build | TypeScript and Vite passed |
| Static analysis | Ruff passed on all P3 backend changes |
| Migration rehearsal | Version 7 to 8 passed with source/revision counts unchanged |

The live catalog is now at schema version 8. It contained zero chapter sets immediately after the
migration; sets are generated lazily for ready sources when they are opened, using the active
annotation revision. The local API smoke test then generated the first set for a Hamda source.
