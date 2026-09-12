# Moshi Dataset Studio — Executable Phases

Status: P0 through P7 implementation complete; external backup replication remains pending
Specification: [FINAL_PRODUCT_IMPLEMENTATION_PLAN.md](FINAL_PRODUCT_IMPLEMENTATION_PLAN.md)

## How to execute this program

Implement this work phase by phase, not as one task. Each phase is a bounded epic with its own
tests and release gate. A phase may use several pull requests, but the application must remain
usable after every merged task.

Rules for every phase:

- begin from a green baseline;
- do not combine unrelated refactoring with behavior changes;
- add tests with each behavior change;
- preserve existing source data and annotation revisions;
- use additive numbered database migrations;
- keep unfinished UI behind a disabled feature flag;
- update typed frontend and backend contracts together;
- record any deliberate deviation in the master plan before implementation;
- do not start the next phase until the current phase gate passes.

## Phase dependency map

```text
P0 Baseline
 └─ P0.1 Transcript protection
     └─ P1 Playback
         └─ P2 Layout and commands
             └─ P3 Chapters and long-source performance
                 └─ P4 Review productivity
                    └─ P5 Machine transcripts, versions, and approval
                         └─ P6 Overlap editing
                             ├─ P7 Admin evaluation
                             └─ P8 Admin training preparation
                                  └─ P9 Hardening and rollout
```

P7 and the non-overlap portions of P8 may be developed in parallel only after P6 data contracts
are stable. P9 always runs last.

## P0 — Baseline, contracts, and fixtures

Goal: establish reproducible safety and performance baselines before changing product behavior.

Status: **complete (2026-09-09)**  
Evidence: [P0_BASELINE.md](P0_BASELINE.md)

### Tasks

- **P0-T1 — Baseline verification**
  - Run the complete frontend and backend suites.
  - Record test counts, build results, and relevant environment versions.
  - Record current short-source load and interaction measurements.

- **P0-T2 — Media fixtures**
  - Add small mono audio, video-with-audio, independent stereo, overlap, and long-timeline fixtures.
  - Ensure fixtures contain no private production material.

- **P0-T3 — Contract definitions**
  - Define playback, chapter, peak-window, model-run, overlap-review, evaluation, and training
    package request/response types.
  - Document schema versions and error envelopes.

- **P0-T4 — Migration rehearsal**
  - Back up a representative SQLite workspace.
  - Exercise migrations on the copy and compare row/artifact counts.
  - Document restore-based rollback.

### Deliverable

A green baseline, reusable media fixtures, approved contracts, and a rehearsed migration process.

### Gate

- [x] Full baseline suite passes.
- [x] Fixtures exercise real browser media behavior.
- [x] No implementation phase depends on an undefined API or schema.

## P0.1 — Transcript durability and recovery

Goal: protect the original Whisper output and every explicitly saved human correction before more
product behavior changes.

Status: **local snapshot complete (2026-09-09); external replication pending**  
Evidence: [P0_1_TRANSCRIPT_PROTECTION.md](P0_1_TRANSCRIPT_PROTECTION.md)

### Tasks

- **P0.1-T1 — Consistent transcript snapshot**
  - Back up the live SQLite catalog with the online backup API.
  - Export every annotation revision as portable UTF-8 JSONL.
  - Copy raw, aligned, and diarization artifacts using catalog ownership records.

- **P0.1-T2 — Integrity and immutability controls**
  - Verify SQLite integrity and foreign keys.
  - Verify artifact size and SHA-256 against the catalog.
  - Produce a checksummed manifest and refuse destination overwrite.

- **P0.1-T3 — Recovery and failure domain**
  - Document the restore sequence.
  - Retain the first verified local snapshot.
  - Configure scheduled retention on encrypted storage outside the workspace disk.

### Gate

- [x] Every saved annotation revision is present in both SQLite and portable JSONL form.
- [x] Every registered original/aligned transcript artifact is present and checksum-valid.
- [x] Verification detects tampering and snapshot creation refuses overwrite.
- [ ] A verified copy exists in a separate storage failure domain.

## P1 — Playback correctness and synchronization

Goal: make playback trustworthy before moving or extending the interface.

Status: **complete (2026-09-09)**  
Evidence: [P1_PLAYBACK.md](P1_PLAYBACK.md)

### Tasks

- **P1-T1 — Playback controller**
  - Introduce the shared playback state machine and `once`/`loop` range contract.
  - Keep playback state outside transcript rendering.

- **P1-T2 — Segment Play and Loop**
  - Make Play seek, start, and pause at the segment end.
  - Make Loop repeat the exact selected range.
  - Define pause, stop, seek-outside-range, and replace-range behavior.

- **P1-T3 — One audible media source**
  - Use canonical audio as master.
  - Mute and synchronize video.
  - Remove competing native playback behavior.

- **P1-T4 — Playback regression tests**
  - Add unit, component, and real-browser tests with the media fixtures.
  - Cover loading, errors, repeated selection, speed, frame seek, and drift correction.

### Deliverable

A single synchronized player API used by waveform, video, transcript, and future commands.

### Gate

- [x] Play and Loop meet exact range semantics.
- [x] No doubled audio.
- [x] Audio/video drift stays within the chosen 120 ms tolerance.
- [x] Existing annotation tests remain green.

## P2 — Review layout, commands, and keyboard access

Goal: establish the final interaction shell before adding more review tools.

Status: **complete (2026-09-11)**  
Evidence: [P2_REVIEW_WORKSPACE.md](P2_REVIEW_WORKSPACE.md)

### Tasks

- **P2-T1 — Review component extraction**
  - Extract the player, rail, transcript list, and inspector from the monolithic application.
  - Preserve behavior during extraction.

- **P2-T2 — Sticky player card**
  - Place video, waveform, transport, timecode, and active range in a non-obstructive sticky card.
  - Add responsive behavior and a mobile tool drawer.

- **P2-T3 — Left tool rail**
  - Move save, history, selected-segment, speaker, timing, overlap, and exclusion tools into grouped
    sections.
  - Keep transport controls in the player.

- **P2-T4 — Command registry**
  - Centralize commands, enabled predicates, labels, and default bindings.
  - Implement context-aware shortcut dispatch.

- **P2-T5 — Command palette and help**
  - Add the command palette and searchable shortcut guide.
  - Preserve standard keyboard behavior in inputs, textareas, selects, buttons, and dialogs.

### Deliverable

The final responsive review shell with accessible mouse and keyboard operation.

### Gate

- [x] Keyboard-only consecutive-segment review works.
- [x] Sticky content never hides the transcript or dialogs.
- [x] Shortcuts never fire while typing or using native controls.
- [x] The transcript remains scrollable at normal zoom and the desktop tool rail has a safe,
  user-adjustable width.

The previously deferred Save/version decision is now resolved by
[P5_TRANSCRIPT_AND_SAVE_POLICY.md](P5_TRANSCRIPT_AND_SAVE_POLICY.md). P2 itself still leaves existing
revisions untouched.

## P3 — Review chapters and long-source performance

Goal: make multi-hour sources practical without modifying their media or global timestamps.

Status: implementation and automated gate complete on 2026-09-10; local UI acceptance remains available.

Deferred UX decision for this phase: determine the maximum number of segments shown in one chapter
or list window, using real-source testing before fixing the default. This limit must prevent difficult
segment selection without changing global timestamps or deleting segments.

### Tasks

- [x] **P3-T1 — Chapter persistence**
  - Add chapter-set, chapter, and chapter-review migrations and catalog methods.
  - Add maximum-duration and count-based configuration APIs.

- [x] **P3-T2 — Boundary generation**
  - Generate a default maximum-30-minute chapter set.
  - Snap boundaries to silence or transcript boundaries without cutting aligned words.
  - Persist boundary reasons.

- [x] **P3-T3 — Chapter navigation**
  - Add previous/next, direct selection, time range, counts, and URL/session restoration.
  - Keep annotation times global.

- [x] **P3-T4 — Peak-backed waveform**
  - Consume existing precomputed peaks for the overview.
  - Add multi-resolution peak windows for detailed chapter zoom.
  - Avoid full-WAV decoding for waveform drawing.

- [x] **P3-T5 — Transcript virtualization**
  - Render only the current chapter and virtual viewport.
  - Preserve selection, variable row heights, RTL text, and keyboard navigation.

- [x] **P3-T6 — State performance**
  - Replace render-time whole-annotation serialization with explicit change tracking.
  - Isolate high-frequency playhead updates from the full transcript tree.

### Deliverable

Stable chapter navigation and bounded browser work for long sources.

### Gate

- [x] Default chapters never exceed 30 minutes.
- [x] Count mode produces the requested count when valid.
- [x] Boundaries preserve words and segments.
- [x] Multi-hour source load does not scale DOM or waveform decode with full duration.

Implementation and verification details: [P3_REVIEW_CHAPTERS.md](P3_REVIEW_CHAPTERS.md).

## P4 — Verification, discovery, resilience, and channel audition

Goal: turn the editor into an efficient repeatable review workflow.

Status: **implementation and automated gate complete on 2026-09-10; local UI acceptance remains available.**
Evidence: [P4_REVIEW_WORKFLOW.md](P4_REVIEW_WORKFLOW.md)

### Tasks

- [x] **P4-T1 — Human verification**
  - Add verify/unverify and Verify then next.
  - Clear verification after relevant text, speaker, timing, split, join, or overlap changes.

- [x] **P4-T2 — Completion progress**
  - Calculate eligible, verified, blocked, and completed counts for chapter and source.
  - Tie completion to an annotation revision and mark it stale after edits.

- [x] **P4-T3 — Search and filters**
  - Add text, speaker, flag, verification, alignment, overlap, empty-text, edited, and chapter filters.

- [x] **P4-T4 — Quality queue**
  - Surface existing priority data and navigate directly to the correct chapter and segment.
  - Distinguish local working-state counts from saved server counts.

- [x] **P4-T5 — Auto-follow**
  - Follow the playing segment until manual scrolling suspends it.
  - Add Return to playhead.

- [x] **P4-T6 — Local draft recovery**
  - Add user/source/base-revision IndexedDB drafts.
  - Add restore, inspect, discard, conflict, purge, and deployment-disable behavior.

- [x] **P4-T7 — Channel audition**
  - Add Mixed, Left, Right, Speaker A, and Speaker B modes when verified channel data exists.

### Deliverable

A measurable, searchable, recoverable, keyboard-efficient review workflow.

### Gate

- [x] Completion and verification remain correct across save/reload.
- [x] Draft recovery cannot overwrite newer server work.
- [x] Queue and auto-follow open the correct chapter and segment.
- [x] Audition mode does not alter stored routing.

## P5 — Immutable machine transcripts, corrected versions, and approval

Goal: create trustworthy transcript provenance, compact safe saving, and whole-source final
approval for evaluation and training.

Status: **complete (2026-09-11)**  
Policy: [P5_TRANSCRIPT_AND_SAVE_POLICY.md](P5_TRANSCRIPT_AND_SAVE_POLICY.md)

### Tasks

- [x] **P5-T1 — Machine transcript schema**
  - Add source-local M1/M2/M3 transcript ordinals and annotation-actor migrations.
  - Record producer/model metadata while keeping the M number independent of model family.
  - Reference checksum-verified raw, aligned, and diarization artifacts.

- [x] **P5-T2 — Historical backfill**
  - Backfill from registered analysis artifacts.
  - Mark missing raw output unavailable without inference or reconstruction, and mark unavailable
    historical model/config metadata as `historical_unknown` rather than borrowing current settings.

- [x] **P5-T3 — Machine transcript APIs**
  - Add typed retrieval and availability endpoints with source-local M labels.
  - Enforce server-side admin authorization and artifact ownership.

- [x] **P5-T4 — Corrected version and Save contract**
  - Add visible corrected versions backed by immutable internal generations.
  - Make Save update the eligible current unapproved version and make the split-button action Save
    as new version; creating a version requires no admin approval.
  - Add authoritative no-op fingerprints, optimistic generation conflicts, actor/origin metadata,
    and generated change summaries.

- [x] **P5-T5 — Recovery and local Undo**
  - Retain replaced server generations for 10 days without showing them as corrected versions.
  - Keep crash recovery and bounded Undo/Redo browser-side; neither creates a server version.

- [x] **P5-T6 — Whole-source admin approval**
  - Allow only a clean, fully verified, blocker-free latest version to be submitted.
  - Freeze pending content and add admin approve/reject/return tasks with actor and fingerprint.
  - Lock approved final versions permanently; later edits start a new unapproved version.

- [x] **P5-T7 — Approved-history retention**
  - After approval, offer Keep all or the recommended verified-backup/archive-for-10-days policy.
  - Refuse payload cleanup while any evaluation, training package, approval, or immutable output
    references the version; retain permanent audit metadata after eligible payload cleanup.
  - Allow Keep all to cancel a waiting archive decision safely.

- [x] **P5-T8 — Editable-model-text deprecation**
  - Retain segment `model_text` for compatibility and inline hints.
  - Remove it from authoritative evaluation logic.

### Deliverable

Immutable, independently retrievable M transcripts; attributable corrected versions with compact,
recoverable saving; and an approved final whole-source version.

### Gate

- [x] Split/join/edit operations cannot change any stored M transcript.
- [x] M labels identify successive transcripts for a source, not model families.
- [x] Backfill is idempotent.
- [x] Historical missing-artifact states are explicit.
- [x] Non-admin machine-transcript artifact access is rejected.
- [x] Save and Save as new version follow the approved split-button contract.
- [x] Overwritten generations remain recoverable for 10 days and local Undo remains available.
- [x] Admin approval is required only for whole-source final approval, not for creating a new version.

### Completion evidence

- Schema 9 was rehearsed twice; provenance-hardening schema 10 and retention-audit schema 11 were
  each rehearsed again against the production-size catalog before deployment.
- Every rehearsal and the deployed migration preserved all 2,502 annotation revisions and their
  combined annotation-payload SHA-256
  `df3b625d389035725d52c36758a6ed11e77f76732dd2580921e57affd63847e5`; SQLite integrity and
  foreign-key checks passed.
- The deployed backfill created 2,502 corrected-version mappings and 55 immutable M1 snapshots
  for the 55 sources with registered machine artifacts; the two sources without machine output did
  not receive a fabricated M version.
- Future aligned-word arrays are content-addressed and stored once, then transparently rehydrated.
- Backend: 250 tests passed. Frontend: 221 tests passed. Production frontend build passed.
- Approved versions are immutable and retention cleanup cannot break dependent provenance.

## P6 — Direct overlap editing

Goal: let reviewers correct overlap deliberately while preserving text and invalidating stale work.

Status: **complete (2026-09-12)**
Evidence: [P6_OVERLAP_REVIEW.md](P6_OVERLAP_REVIEW.md)

### Tasks

- [x] **P6-T1 — Overlap review schema**
  - Add classification, training decision, contributor, reviewer, artifact, and stale-state records.

- [x] **P6-T2 — Derived overlap lane**
  - Render selectable overlap above the speaker lanes and highlight both contributors.

- [x] **P6-T3 — Boundary editing**
  - Add drag, exact input, frame/word nudge, extend, shorten, split, and merge.
  - Preview transcript consequences and word-boundary warnings.

- [x] **P6-T4 — Semantic removal**
  - Require Speaker A only, Speaker B only, or Neither usable.
  - Keep every action undoable before Save.

- [x] **P6-T5 — Classification and audition**
  - Add confirmed, false positive, third speaker, noise, and unintelligible classifications.
  - Add raw/separate/exclude/needs-work decisions and available A/B audition.

- [x] **P6-T6 — Dependency invalidation**
  - Mark recovery, QC, chapter completion, evaluation cache, and training dependencies stale after
    contributing activity changes.

### Deliverable

A complete overlap correction workflow with explicit semantics and audit history.

### Gate

- [x] Reviewers can increase, reduce, split, merge, or remove overlap.
- [x] No overlap action silently loses transcript text.
- [x] Stale recoveries or decisions cannot reach approval or a future training package.
- [x] Existing local Undo/Redo and server revision conflicts cover all operations.

### Completion evidence

- Schema 12 was rehearsed together with schema 13 against the production-size catalog before
  deployment. All 2,502 annotation revisions and their combined payload digest were preserved.
- Overlap edits use the existing `edit()` history path; browser Undo/Redo behavior was not replaced.
- Backend persistence tests and five frontend overlap-operation tests pass.

## P7 — Admin WER/CER evaluation

Goal: compare immutable Whisper output with selected verified corrections accurately and visibly.

Status: **complete (2026-09-12)**
Evidence: [P7_ADMIN_EVALUATION.md](P7_ADMIN_EVALUATION.md)

### Tasks

- [x] **P7-T1 — Metric engine**
  - Implement word/character edit operations and micro aggregation.
  - Report insertions, deletions, substitutions, denominators, and coverage.

- [x] **P7-T2 — Temporal comparison**
  - Align by time rather than mutable segment IDs.
  - Define and record overlap deduplication/exclusion policy.

- [x] **P7-T3 — Arabic normalization**
  - Add versioned strict and normalized policies with Arabic and mixed-language tests.

- [x] **P7-T4 — Evaluation APIs**
  - Add preview, immutable report, history, and download endpoints under admin authorization.

- [x] **P7-T5 — Evaluation page**
  - Add metrics, filters, coverage, original/corrected diff, and range playback.

### Deliverable

An admin-only, reproducible quality evaluation page and downloadable report.

### Gate

- [x] Metrics match independent fixtures.
- [x] Every percentage includes its denominator and evaluated coverage.
- [x] Diff remains correct after transcript split/join.
- [x] Normal users cannot access the page or APIs.

### Completion evidence

- Schema 13 was rehearsed and deployed with SQLite integrity `ok` and no foreign-key errors.
- The deployed catalog remains at 2,502 annotation revisions with payload SHA-256
  `df3b625d389035725d52c36758a6ed11e77f76732dd2580921e57affd63847e5`.
- The final regression run passed 257 backend tests and 226 frontend tests; lint, TypeScript checks,
  and the production build passed.
- Port 80 serves the final frontend asset and `/api/health` reports `ok`; unauthenticated access to
  the evaluation API returns HTTP 401.

## P8 — Admin training preparation and packages

Goal: generate validated, target-specific, immutable training datasets.

### Tasks

- **P8-T1 — Profile and package schema**
  - Add reusable training profiles, immutable package versions, source revision maps, and artifacts.

- **P8-T2 — Shared validation**
  - Implement rights, bounds, text, verification, quality, overlap, routing, duration, staleness, and
    leakage gates.

- **P8-T3 — Whisper adapter**
  - Generate short audio/text samples and target manifests with provenance and QC.

- **P8-T4 — Moshi adapter**
  - Harden existing conversation audio, role, alignment, normalization, skipped-word, and JSONL
    generation.

- **P8-T5 — Split and preview**
  - Preview representative and edge-case samples.
  - Enforce deterministic source-level train/evaluation separation.

- **P8-T6 — Durable generation**
  - Generate packages through a durable job with progress, retry, stale-input rejection, atomic
    publication, checksums, and cleanup.

- **P8-T7 — Training page**
  - Implement Select → Target → Configure → Validate → Preview → Generate → Download.
  - Add package history and immutable version details.

### Deliverable

Downloadable Whisper and Moshi training packages with validation, QC, checksums, and provenance.

### Gate

- Preview records match generated records.
- Unsafe or incomplete data is blocked.
- No source leaks between train and evaluation.
- Package schemas and media validate independently.
- Normal users cannot access any training administration API.

## P9 — Hardening, deployment, and rollout

Goal: release safely without losing compatibility or recoverability.

### Tasks

- **P9-T1 — Full regression and accessibility**
  - Run frontend/backend/E2E suites, keyboard audit, screen-reader checks, RTL, responsive, and
    reduced-motion checks.

- **P9-T2 — Deployment media behavior**
  - Verify FastAPI/Nginx ranges, caching, large downloads, peak windows, and channel media.

- **P9-T3 — Failure recovery**
  - Rehearse migrations, stale inputs, worker interruption, package retry, partial artifact cleanup,
    and database restore.

- **P9-T4 — Security and privacy**
  - Audit admin endpoints, ownership, artifact paths, local drafts, rights gates, logs, and download
    caching.

- **P9-T5 — Observability and flags**
  - Add structured events, counters, operational dashboards, and independent feature flags.

- **P9-T6 — Staged release**
  - Enable playback, layout, chapters, productivity, provenance, overlap, evaluation, and training
    features in that order.

### Deliverable

A monitored, reversible production rollout with migrated historical data.

### Gate

- Complete definition of done in the master plan is satisfied.
- No severity-one accessibility or security findings.
- Old sources and exports remain readable.
- Disabling a feature does not require reversing migrated data.

## Recommended first implementation task

Start with **P0-T1 Baseline verification**, then complete P0 before changing playback. The first
user-visible implementation task is **P1-T1 Playback controller**, followed immediately by the
Play/Loop behavior and its tests.

Do not start with the visual layout. Fixing the media state contract first prevents the new sticky
player, shortcuts, chapters, overlap audition, and evaluation diff player from each inventing a
different playback path.
