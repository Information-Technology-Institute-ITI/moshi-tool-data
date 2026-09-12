# P6 — Direct overlap editing

Status: complete on 2026-09-12

## Product rules

- An overlap is derived from the intersection of one Speaker A activity and one Speaker B activity.
- Boundary edits alter activity timing, never transcript text. Affected transcript segments become
  unverified and must be reviewed again.
- Removing overlap always requires an explicit meaning: keep A, keep B, or mark neither usable.
- Classification and the training decision are separate review decisions.
- Every overlap change uses the existing browser-local Undo/Redo history. Nothing is written to the
  server until the reviewer presses Save.
- A Save records the resulting overlap state and an attributable audit event. Concurrent edits still
  use the existing annotation-version conflict check.
- Activity changes invalidate derived overlap decisions, recovered overlap artifacts, clip plans, and
  chapter completion tied to the prior annotation version. Unresolved overlap blocks final approval.

## Delivered workflow

- A selectable overlap lane appears above Speaker A and Speaker B.
- The left tool rail provides exact sample-derived second inputs, frame nudges, aligned-word snaps,
  split, adjacent merge, and semantic removal.
- Mixed playback is always available. Isolated A/B audition is enabled only when independent stereo
  channel routing has been verified.
- Reviewers can classify confirmed overlap, false positives, third speaker, noise, or unintelligible
  audio and choose raw, separate, exclude, or needs-work training handling.
- The interface warns when a boundary crosses aligned words and shows how many transcript segments
  will require reverification.

## Persistence

Schema 12 adds materialized overlap-review state and append-only overlap-review events. Old
annotations remain readable because the embedded overlap list defaults to empty. No existing
annotation revision is rewritten.

## Verification

- Frontend overlap-editing fixtures cover classification, exact bounds, semantic removal, split,
  merge, and the no-text-loss invariant.
- Backend persistence fixtures cover saved state and audit events.
- Full-suite and production-build results are recorded in `IMPLEMENTATION_PHASES.md` after release
  validation.
