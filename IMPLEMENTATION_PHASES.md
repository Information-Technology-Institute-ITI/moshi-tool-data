# Moshi Dataset Studio — Executable Phases

Status: P0 and P1 complete; P2 implementation awaiting local UI acceptance; external backup replication remains pending
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
                     └─ P5 Immutable model transcript
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

Status: **implementation complete; awaiting local UI acceptance (2026-09-09)**  
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

Deferred until the interface updates are finished: server-save criteria, revision frequency,
checkpoint grouping, and how dense revision history should be presented. Existing revisions remain
untouched.

## P3 — Review chapters and long-source performance

Goal: make multi-hour sources practical without modifying their media or global timestamps.

Deferred UX decision for this phase: determine the maximum number of segments shown in one chapter
or list window, using real-source testing before fixing the default. This limit must prevent difficult
segment selection without changing global timestamps or deleting segments.

### Tasks

- **P3-T1 — Chapter persistence**
  - Add chapter-set, chapter, and chapter-review migrations and catalog methods.
  - Add maximum-duration and count-based configuration APIs.

- **P3-T2 — Boundary generation**
  - Generate a default maximum-30-minute chapter set.
  - Snap boundaries to silence or transcript boundaries without cutting aligned words.
  - Persist boundary reasons.

- **P3-T3 — Chapter navigation**
  - Add previous/next, direct selection, time range, counts, and URL/session restoration.
  - Keep annotation times global.

- **P3-T4 — Peak-backed waveform**
  - Consume existing precomputed peaks for the overview.
  - Add multi-resolution peak windows for detailed chapter zoom.
  - Avoid full-WAV decoding for waveform drawing.

- **P3-T5 — Transcript virtualization**
  - Render only the current chapter and virtual viewport.
  - Preserve selection, variable row heights, RTL text, and keyboard navigation.

- **P3-T6 — State performance**
  - Replace render-time whole-annotation serialization with explicit change tracking.
  - Isolate high-frequency playhead updates from the full transcript tree.

### Deliverable

Stable chapter navigation and bounded browser work for long sources.

### Gate

- Default chapters never exceed 30 minutes.
- Count mode produces the requested count when valid.
- Boundaries preserve words and segments.
- Multi-hour source load does not scale DOM or waveform decode with full duration.

## P4 — Verification, discovery, resilience, and channel audition

Goal: turn the editor into an efficient repeatable review workflow.

### Tasks

- **P4-T1 — Human verification**
  - Add verify/unverify and Verify then next.
  - Clear verification after relevant text, speaker, timing, split, join, or overlap changes.

- **P4-T2 — Completion progress**
  - Calculate eligible, verified, blocked, and completed counts for chapter and source.
  - Tie completion to an annotation revision and mark it stale after edits.

- **P4-T3 — Search and filters**
  - Add text, speaker, flag, verification, alignment, overlap, empty-text, edited, and chapter filters.

- **P4-T4 — Quality queue**
  - Surface existing priority data and navigate directly to the correct chapter and segment.
  - Distinguish local working-state counts from saved server counts.

- **P4-T5 — Auto-follow**
  - Follow the playing segment until manual scrolling suspends it.
  - Add Return to playhead.

- **P4-T6 — Local draft recovery**
  - Add user/source/base-revision IndexedDB drafts.
  - Add restore, inspect, discard, conflict, purge, and deployment-disable behavior.

- **P4-T7 — Channel audition**
  - Add Mixed, Left, Right, Speaker A, and Speaker B modes when verified channel data exists.

### Deliverable

A measurable, searchable, recoverable, keyboard-efficient review workflow.

### Gate

- Completion and verification remain correct across save/reload.
- Draft recovery cannot overwrite newer server work.
- Queue and auto-follow open the correct chapter and segment.
- Audition mode does not alter stored routing.

## P5 — Immutable original model transcript

Goal: create a trustworthy provenance foundation for evaluation and training.

### Tasks

- **P5-T1 — Model-run schema**
  - Add model-run and annotation-actor migrations.
  - Reference checksum-verified raw, aligned, and diarization artifacts.

- **P5-T2 — Historical backfill**
  - Backfill from registered analysis artifacts.
  - Mark missing raw output unavailable without inference or reconstruction.

- **P5-T3 — Model-run APIs**
  - Add typed admin retrieval and availability endpoints.
  - Enforce server-side admin authorization and artifact ownership.

- **P5-T4 — Editable-model-text deprecation**
  - Retain segment `model_text` for compatibility and inline hints.
  - Remove it from authoritative evaluation logic.

### Deliverable

Immutable, independently retrievable model runs and attributable corrected revisions.

### Gate

- Split/join/edit operations cannot change the stored model hypothesis.
- Backfill is idempotent.
- Historical missing-artifact states are explicit.
- Non-admin model-run access is rejected.

## P6 — Direct overlap editing

Goal: let reviewers correct overlap deliberately while preserving text and invalidating stale work.

### Tasks

- **P6-T1 — Overlap review schema**
  - Add classification, training decision, contributor, reviewer, artifact, and stale-state records.

- **P6-T2 — Derived overlap lane**
  - Render selectable overlap above the speaker lanes and highlight both contributors.

- **P6-T3 — Boundary editing**
  - Add drag, exact input, frame/word nudge, extend, shorten, split, and merge.
  - Preview transcript consequences and word-boundary warnings.

- **P6-T4 — Semantic removal**
  - Require Speaker A only, Speaker B only, or Neither usable.
  - Keep every action undoable before Save.

- **P6-T5 — Classification and audition**
  - Add confirmed, false positive, third speaker, noise, and unintelligible classifications.
  - Add raw/separate/exclude/needs-work decisions and available A/B audition.

- **P6-T6 — Dependency invalidation**
  - Mark recovery, QC, chapter completion, evaluation cache, and training dependencies stale after
    contributing activity changes.

### Deliverable

A complete overlap correction workflow with explicit semantics and audit history.

### Gate

- Reviewers can increase, reduce, split, merge, or remove overlap.
- No overlap action silently loses transcript text.
- Stale recoveries or decisions cannot reach a training package.
- Undo/redo and revision conflicts cover all operations.

## P7 — Admin WER/CER evaluation

Goal: compare immutable Whisper output with selected verified corrections accurately and visibly.

### Tasks

- **P7-T1 — Metric engine**
  - Implement word/character edit operations and micro aggregation.
  - Report insertions, deletions, substitutions, denominators, and coverage.

- **P7-T2 — Temporal comparison**
  - Align by time rather than mutable segment IDs.
  - Define and record overlap deduplication/exclusion policy.

- **P7-T3 — Arabic normalization**
  - Add versioned strict and normalized policies with Arabic and mixed-language tests.

- **P7-T4 — Evaluation APIs**
  - Add preview, immutable report, history, and download endpoints under admin authorization.

- **P7-T5 — Evaluation page**
  - Add metrics, filters, coverage, original/corrected diff, and range playback.

### Deliverable

An admin-only, reproducible quality evaluation page and downloadable report.

### Gate

- Metrics match independent fixtures.
- Every percentage includes its denominator and evaluated coverage.
- Diff remains correct after transcript split/join.
- Normal users cannot access the page or APIs.

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
