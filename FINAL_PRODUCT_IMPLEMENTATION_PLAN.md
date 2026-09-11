# Moshi Dataset Studio — Final Product Implementation Plan

Status: approved planning baseline  
Date: 2026-09-09  
Scope: review workflow, long-source navigation, evaluation, overlap correction, and training-data preparation

Execution companion: [IMPLEMENTATION_PHASES.md](IMPLEMENTATION_PHASES.md)

## 1. Outcome

Turn the existing Studio into a focused annotation and dataset-preparation product for Whisper
and Moshi workflows.

The completed product will:

- keep the uploaded source and original model outputs immutable;
- store reviewer corrections as separate, versioned annotations;
- make long recordings practical through stable review chapters of at most 30 minutes, or a
  user-selected chapter count;
- provide one synchronized, sticky audio/video player with correct segment Play and Loop;
- place editing tools in a persistent left rail and support context-aware keyboard commands;
- let reviewers directly correct speaker activity and derived overlap;
- track human verification and review completion;
- let administrators compare any immutable M transcript with verified corrections using WER, CER,
  and related quality metrics;
- let administrators validate, preview, and generate immutable, target-specific Whisper or
  Moshi training packages;
- preserve source-level train/evaluation isolation, provenance, checksums, and reproducibility.

## 2. Product terminology

The UI and code must use these terms consistently:

- **Source**: one uploaded audio or video recording.
- **Review chapter**: a navigation and rendering window over a source. It does not cut or
  re-encode media. Default maximum duration: 30 minutes.
- **Transcript segment**: one editable utterance with speaker, time range, and text.
- **Activity region**: a time range during which one speaker is active.
- **Overlap**: a range derived from the intersection of two speakers' activity regions.
- **Training sample**: a short model-ready audio/text example generated after review. It is not a
  review chapter.
- **Machine transcript version**: an immutable machine-produced transcript identified as M1, M2,
  and so on for one source. The M number identifies a new transcript, not a model family.
- **Corrected transcript**: the latest or selected human-corrected version, identified separately
  from M versions.
- **Training package**: an immutable versioned export for one target profile.

This distinction is mandatory. In particular, a 30-minute review chapter must never be described
as a training sample or exported dataset clip.

## 3. Product principles and fixed decisions

1. **Non-destructive editing**  
   Editing changes annotation data only. The original upload, canonical audio, video proxy, raw
   model transcript, and original diarization remain unchanged.

2. **One authoritative playback clock**  
   Canonical audio is the playback master. Video is muted and follows the audio clock. This avoids
   doubled audio and permits mixed, left, right, Speaker A, and Speaker B audition modes.

3. **Explicit server save and version choice remain**  
   Local crash recovery and Undo remain browser-side and create no server version. The primary Save
   updates the eligible current unapproved corrected version through an immutable internal
   generation; the split-button action Save as new version creates the next visible version without
   admin approval. Replaced generations remain recoverable for 10 days.

4. **Review chapters are persisted**  
   Automatically chosen chapter boundaries are snapped once and stored. They remain stable across
   sessions and reviewers so progress is meaningful. Regeneration is an explicit action.

5. **Overlap is derived, not duplicated**  
   Speaker activity regions remain the timing source of truth. Overlap review decisions and
   recovery artifacts reference a derived overlap and become stale when its contributing activity
   boundaries change.

6. **Evaluation uses immutable machine-transcript output**  
   WER and CER never use the editable segment-level `model_text` field as the authoritative model
   transcript. They compare a selected M transcript with an exact corrected-version generation.

7. **Official metrics use micro-averaging**  
   Corpus WER/CER are total edit distance divided by total reference words/characters. Macro
   averages may be shown as diagnostics but are not the headline score.

8. **Training preparation is admin-only and target-specific**  
   Whisper and Moshi use separate validation rules and export adapters. A generic archive is not
   labelled training-ready.

9. **No silent reprocessing**  
   Player, evaluation, overlap editing, and export actions never rerun Whisper, alignment,
   diarization, or separation. Any future reprocessing action must be explicit and separately
   authorized.

10. **Server-side authorization is authoritative**  
    Hiding an admin page in React is not a security boundary. Every evaluation, model-artifact,
    training-profile, preview, and package endpoint requires an active administrator principal.

## 4. Current baseline to preserve

The implementation must retain these existing strengths:

- 24 kHz sample-based annotation timing;
- immutable source media and canonical artifacts;
- optimistic annotation revision conflicts;
- explicit Save, unsaved navigation protection, and undo/redo;
- assisted or manual initialization with a single successful preparation pass;
- project ownership and non-disclosing 404 behavior;
- durable jobs, worker protocol, artifact checksums, and SSE progress;
- deterministic source-level train/evaluation assignment in the existing exporter;
- existing raw transcript, aligned transcript, diarization, waveform peaks, channel audio,
  annotation revisions, quality queue data, and dataset archive behavior;
- all currently passing frontend and backend tests.

Known baseline defects and gaps addressed by this plan:

- segment Play seeks but does not start playback;
- non-looping segment playback has no stop-at-end behavior;
- WaveSurfer audio and audible video can play simultaneously;
- native video controls and WaveSurfer do not share a single state machine;
- global shortcuts interfere with non-text controls and have no central registry;
- the video is not a separate sticky card;
- tools are scattered between the rail, transport, waveform, and inspector;
- the browser does not consume the precomputed peaks URL;
- the full transcript and source-wide regions are mounted for long sources;
- dirty detection serializes the complete annotation on renders;
- the original `model_text` relationship breaks after transcript splits and joins;
- current CER is a mean of per-segment rates and no WER admin view exists;
- quality, verification, candidates, channel routing, and older training-export capabilities are
  only partially exposed;
- overlap resizing is technically possible but obscure and semantically incomplete.

## 5. Target information architecture

### Standard user pages

1. Intro and authentication
2. Project library
3. Project workspace and source upload
4. Source annotation workspace

### Administrator pages

1. Evaluation
2. Training preparation
3. GPU status

### Source annotation workspace

The desktop layout is:

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Global navigation                                                   │
├──────────────────┬──────────────────────────────────────────────────┤
│ Sticky tool rail │ Sticky player card                               │
│                  │ Video / waveform / transport / chapter controls  │
│ Chapter nav      ├──────────────────────────────────────────────────┤
│ Save / undo      │ Search, filters, and review queue                 │
│ Segment tools    ├──────────────────────────────────────────────────┤
│ Overlap tools    │ Virtualized transcript and segment inspector     │
│ Revision history │                                                  │
└──────────────────┴──────────────────────────────────────────────────┘
```

On narrow screens, the player remains sticky below global navigation, the tool rail becomes a
drawer, and the transcript remains the main scrolling surface. Sticky elements must never cover
the selected transcript entry.

## 6. Architecture changes

### 6.1 Frontend decomposition

Split the current monolithic review implementation into bounded modules:

```text
frontend/src/review/
  StudioReviewPage.tsx
  ReviewToolRail.tsx
  PlayerCard.tsx
  usePlaybackController.ts
  ChapterNavigator.tsx
  useReviewChapters.ts
  TranscriptVirtualList.tsx
  SegmentInspector.tsx
  OverlapEditor.tsx
  ReviewFilters.tsx
  ReviewQueue.tsx
  useDraftRecovery.ts
  commands.ts
  CommandPalette.tsx

frontend/src/admin/evaluation/
  EvaluationPage.tsx
  TranscriptComparison.tsx
  MetricSummary.tsx
  EvaluationFilters.tsx

frontend/src/admin/training/
  TrainingPreparationPage.tsx
  TargetProfileForm.tsx
  TrainingValidation.tsx
  SamplePreview.tsx
  PackageHistory.tsx
```

Keep pure transcript transformations in `transcript.ts` initially, then split only when a module
has a stable contract. Do not combine component extraction with behavioral rewrites in one large
commit.

### 6.2 Playback controller

Create one playback state machine shared by the player card, transcript, command system, and
overlap editor.

Core state:

```ts
type PlaybackRange = {
  startSample: number;
  endSample: number;
  behavior: "once" | "loop";
};

type PlaybackState = {
  status: "loading" | "paused" | "playing" | "ended" | "error";
  currentSample: number;
  activeRange: PlaybackRange | null;
  rate: number;
  auditionMode: "mixed" | "left" | "right" | "speaker_a" | "speaker_b";
};
```

Rules:

- Play segment seeks to the start, begins playback, pauses at the end, and clears the range.
- Loop segment seeks to the start and repeats at the end.
- Changing segment replaces the active range atomically.
- Manual seeking outside a range clears that range unless the user explicitly keeps Loop.
- Pause preserves the range; Stop clears it.
- Video is always muted and corrected to the audio clock when drift exceeds a small threshold.
- Native video playback controls are replaced by the shared transport; fullscreen and picture in
  picture may remain available.
- Playback errors are visible and recoverable.
- Every playback command is invocable from a button, keyboard binding, and command palette.

### 6.3 Review chapter service

Chapter creation modes:

- `max_duration`, default `1800` seconds;
- `count`, with a positive requested count;
- `manual`, reserved for a later release but supported by the storage contract.

Automatic boundaries:

1. Start from the requested equal-duration positions.
2. Search within a configurable window for a silence boundary.
3. If none is suitable, use the nearest transcript-segment boundary.
4. Never split an aligned word or a transcript segment.
5. Keep every chapter within the source bounds and the configured maximum.
6. Record why each boundary was selected.

Persisted chapter fields:

- stable chapter ID and ordinal;
- start and end samples;
- boundary reason;
- generation mode and settings;
- annotation version used to generate boundaries;
- creation and update timestamps.

Chapter completion is separate from chapter geometry. A completion record contains reviewer,
annotation version, completed time, verified segment count, total eligible segment count, and any
waiver note. Editing a completed chapter marks its completion stale until it is reviewed again.

### 6.4 Long-source rendering

- Fetch and display only transcript segments intersecting the current chapter.
- Include an adjacent segment only when it crosses a chapter boundary; automatic boundaries should
  make this exceptional.
- Use a dynamic-height virtual transcript list so DOM size does not grow with source length.
- Auto-follow the active segment only when the reviewer has not manually scrolled away. Show a
  `Return to playhead` action while follow is suspended.
- Use the existing peaks artifact for the source overview.
- Introduce a backward-compatible multi-resolution peaks v2 artifact for detailed chapter views.
- Pass peaks and duration into WaveSurfer so it does not decode the full canonical WAV merely to
  draw the waveform.
- Load high-resolution peak windows on demand as zoom changes.
- Keep all stored annotation times on the global source timeline; chapter-local display time is
  derived only for presentation.

### 6.5 Command system

Define commands once with ID, label, default binding, category, enabled predicate, and handler.
Buttons and the command palette consume the same registry.

Initial defaults:

| Command | Binding |
|---|---|
| Play/pause | `Space` or `K` |
| Seek backward/forward 1 second | `Left` / `Right` |
| Seek backward/forward 5 seconds | `Shift+Left` / `Shift+Right` |
| Previous/next frame | `,` / `.` |
| Previous/next transcript segment | `P` / `N` |
| Play selected segment once | `Enter` |
| Loop selected segment | `Shift+Enter` |
| Save annotation | `Ctrl/Cmd+S` |
| Undo | `Ctrl/Cmd+Z` |
| Redo | `Ctrl/Cmd+Shift+Z` and `Ctrl+Y` |
| Cancel selection or close top modal | `Escape` |
| Open command palette | `Ctrl/Cmd+K` |
| Show shortcut guide | `?` |

Shortcut handling must be suspended while typing, selecting text, using a select control,
interacting with content-editable content, or when an incompatible modal owns keyboard focus.
Buttons must retain standard Space and Enter activation semantics.

### 6.6 Local draft recovery

- Store annotation drafts in IndexedDB, never media.
- Key drafts by authenticated user, source, and base annotation version.
- Debounce writes after local edits.
- Purge a draft after successful Save, explicit discard, source deletion, or sign-out.
- On return, offer Recover unsaved work, Inspect, or Discard.
- If the server revision advanced, use the existing conflict flow rather than overwriting.
- Allow deployments to disable browser draft storage for sensitive environments.
- Never present local recovery as a saved revision.

## 7. Data model and migrations

All database changes are additive numbered migrations. Before rollout, back up SQLite and record
pre/post row counts. A migration must be safe to rerun through the existing migration registry.

### 7.1 Machine transcript versions

Add `model_runs`:

- `id`
- `source_id`
- `source_transcript_ordinal` (displayed as M1, M2, and so on)
- `initialization_job_id`
- `model_name`
- `model_revision`
- `language`
- `config_fingerprint`
- `raw_transcript_artifact_id`
- `aligned_transcript_artifact_id`
- `diarization_artifact_id`
- `created_at`

The artifact references point to immutable, checksum-verified registry entries. The M ordinal means
"the nth machine transcript for this source" and is independent of provider or model family. For
example, Whisper, Cohere, and a trained Whisper model may produce M1, M2, and M3 respectively. The
trained-model/package identity is recorded as producer metadata; it receives an M ordinal only after
it is run against this source.

Backfill existing sources from active `analysis.raw_transcript`, `analysis.aligned_transcript`, and
`analysis.diarization` artifacts. If the raw artifact is missing, mark comparison unavailable; do
not reconstruct it from edited `model_text` and do not rerun the model.

The current `model_text` and `model_speaker` fields remain for compatibility and inline reviewer
hints, but are deprecated as evaluation authority.

### 7.2 Corrected versions, save generations, and attribution

Add visible corrected-version records and immutable internal storage generations. A corrected
version has a source-local V ordinal, current-generation pointer, state (`unapproved`, `pending`,
`approved`, or `rejected`), creator, timestamps, and optional approval identity. Each generation
records canonical content, content fingerprint, parent generation, actor, origin, change summary,
and expiry eligibility.

The primary Save creates a replacement generation inside the eligible current unapproved version;
the previous generation remains recoverable for 10 days. **Save as new version** creates the next V
ordinal and does not require admin approval. Server-side fingerprint comparison makes no-op Save
idempotent, and optimistic generation matching prevents concurrent overwrite.

Backfilled annotation revisions retain their existing visible version numbers and a null actor when
the historical principal is unknowable. Existing payloads are preserved during migration. Heavy
immutable model/alignment data moves behind M-version artifact references instead of being repeated
in each future corrected generation.

### 7.2.1 Whole-source approval and retention

Submitting for admin approval is permitted only for the clean latest corrected version after the
entire source is verified, every applicable chapter is current and complete, no blocker remains,
and required rights/routing checks pass. Submission freezes the exact generation and creates an
admin task. Admins may approve, reject with a note, or return it for correction. Approved content is
permanently immutable; later work starts a new unapproved version.

After approval, an admin chooses either Keep all versions or the recommended Keep approved version
and archive older payloads for 10 days. Cleanup requires a fresh verified backup, an explicit impact
preview, and reference checks. It never removes source media, M transcripts, the approved payload,
or content referenced by evaluation/training/approval records. Audit metadata and fingerprints
remain after an eligible older payload is removed.

### 7.3 Review chapters

Add:

- `review_chapter_sets`
- `review_chapters`
- `chapter_reviews`

Only one chapter set is active per source. Regeneration creates a new set, preserves old sets for
audit, and invalidates completion rather than deleting history.

### 7.4 Overlap review records

Add `overlap_reviews` with:

- stable review ID;
- source and annotation version;
- contributing Speaker A and Speaker B activity IDs;
- derived start and end samples at review time;
- classification: `confirmed`, `false_positive`, `third_speaker`, `noise`, `unintelligible`;
- training decision: `raw`, `separate`, `exclude`, `needs_work`;
- note and reviewer;
- recovery artifact references;
- state: `current` or `stale`;
- created and updated timestamps.

Changing either contributing activity invalidates the matching overlap review and any derived
recovery or training decision. It does not delete the historical record.

### 7.5 Evaluation snapshots

Add `evaluation_reports`:

- project/source scope;
- model run ID;
- annotation revision map;
- verification scope and overlap policy;
- normalization policy name and version;
- raw and normalized metric JSON;
- coverage JSON;
- optional exported report artifact ID;
- creator and creation time.

Metrics may be calculated on demand for preview, but a downloadable or referenced report is an
immutable snapshot.

### 7.6 Training profiles and packages

Extend or replace the dormant export records with explicit target semantics:

- `training_profiles`: named reusable admin configurations;
- `training_packages`: immutable package versions and status;
- `training_package_sources`: annotation revision and model run provenance per source;
- artifact registry entries for manifests, media, reports, and final bundle.

Required package fields include target (`whisper` or `moshi`), profile snapshot, source revision
map, deterministic split seed/policy, validation report, checksum manifest, creator, and timestamps.

Existing exports remain readable. Do not reinterpret an old export as a new training package.

## 8. API plan

Exact response envelopes must be described with typed frontend interfaces and tested before UI
implementation.

### Review APIs

- `PUT /api/sources/{source_id}/corrected-versions/current` (primary Save)
- `POST /api/sources/{source_id}/corrected-versions` (Save as new version)
- `GET /api/sources/{source_id}/corrected-versions`
- `GET /api/sources/{source_id}/corrected-versions/{version}`
- `POST /api/sources/{source_id}/corrected-versions/{version}/submit-approval`
- `GET /api/sources/{source_id}/chapters`
- `PUT /api/sources/{source_id}/chapters/config`
- `POST /api/sources/{source_id}/chapters/regenerate`
- `PUT /api/sources/{source_id}/chapters/{chapter_id}/review`
- `GET /api/sources/{source_id}/transcript?start_sample=&end_sample=&...filters`
- `GET /api/sources/{source_id}/peaks?start_sample=&end_sample=&resolution=`

Chapter regeneration requires explicit confirmation when completion records exist. Source
ownership rules apply to all review APIs.

Primary Save accepts the expected current generation and content fingerprint. It is idempotent for
unchanged content, cannot update pending or approved content, and retains the replaced generation
for 10 days. Creating a new corrected version does not require admin approval.

### Overlap APIs

Activity-boundary edits continue through the versioned annotation save. Add metadata endpoints:

- `GET /api/sources/{source_id}/overlaps`
- `PUT /api/sources/{source_id}/overlaps/{overlap_review_id}`

Responses identify stale decisions and missing recovery artifacts. No overlap endpoint starts
separation implicitly.

### Admin evaluation APIs

- `GET /api/admin/model-runs`
- `GET /api/admin/approval-tasks`
- `GET /api/admin/approval-tasks/{task_id}`
- `POST /api/admin/approval-tasks/{task_id}/approve`
- `POST /api/admin/approval-tasks/{task_id}/reject`
- `POST /api/admin/sources/{source_id}/version-retention`
- `GET /api/admin/sources/{source_id}/comparison`
- `POST /api/admin/evaluations/preview`
- `POST /api/admin/evaluations`
- `GET /api/admin/evaluations/{report_id}`
- `GET /api/admin/evaluations/{report_id}/download`

Approval endpoints operate on the frozen generation submitted after whole-source verification.
Retention cleanup requires a verified backup, impact preview, reference checks, and the approved
10-day archive window. The comparison endpoint accepts M transcript, exact corrected-version
generation, chapter/range, speaker, verified scope, normalization policy, and overlap policy.

### Admin training APIs

- `GET/POST/PUT /api/admin/training-profiles`
- `POST /api/admin/training/validate`
- `POST /api/admin/training/preview`
- `POST /api/admin/training/packages`
- `GET /api/admin/training/packages`
- `GET /api/admin/training/packages/{package_id}`
- `GET /api/admin/training/packages/{package_id}/download`

Validation and preview do not mutate annotations. Package generation is a durable job and produces
an immutable artifact. Repeating generation creates a new version.

### Media behavior

- Verify byte-range behavior through FastAPI and Nginx for canonical audio, channel audio, and
  video proxies.
- Add cache validators for immutable media and peak artifacts.
- Never expose arbitrary artifact paths.
- Channel audition is available only when preserved-channel media exists and its mapping is
  verified.

## 9. Review features

### 9.1 Left tool rail

Sections:

1. Source and chapter navigation
2. Save state, Save, Undo, Redo
3. Selected-segment actions
4. Speaker and timing tools
5. Overlap and exclusion tools
6. Filters and quality queue
7. Revision history
8. Source deletion, visually isolated at the bottom

Transport controls stay in the player card because they directly manipulate playback.

### 9.2 Sticky player card

Contains:

- video or audio artwork area;
- chapter title and global time range;
- previous/next chapter;
- compact source overview and detailed chapter waveform;
- play/pause, segment Play/Loop, frame step, seek, speed, and zoom;
- global and chapter-local timecode;
- audition mode selector;
- active segment and speaker indicator;
- loop-clear and return-to-playhead actions.

The card is sticky within the main column, not a fixed overlay.

### 9.3 Verification and completion

- Add a Human verified checkbox to the segment inspector.
- Support Verify and move next for keyboard-first review.
- Display verified/eligible percentage per chapter and source.
- Completion requires every eligible segment verified and no blocking quality or overlap item,
  unless an authorized waiver with note exists.
- Editing text, speaker, or timing clears that segment's verification.
- Splitting creates unverified children; joining is verified only if both inputs were verified and
  the reviewer made no further changes.

### 9.4 Search, filters, and queue

Search and filter by:

- transcript text;
- speaker;
- quality flag;
- verified/unverified;
- alignment status;
- overlap involvement;
- empty text;
- manually edited/model unchanged;
- chapter.

The quality queue consumes the existing server priority data. Selecting a queue item opens its
chapter, seeks the player, selects the segment, and scrolls it into view. Queue counts update after
local edits without waiting for Save, while server totals remain labelled as saved-state totals.

### 9.5 Channel audition

When independent stereo is available and verified, offer:

- Mixed
- Left
- Right
- Speaker A
- Speaker B

The selector must make the current routing map visible. Changing audition mode never changes the
saved routing map. Editing the routing map remains an explicit annotation action.

## 10. Direct overlap editing

### 10.1 Selection and visualization

- Draw derived overlap as a distinct selectable layer above the speaker lanes.
- Selecting it opens the overlap editor and highlights both contributing activity regions.
- Show start, end, duration, involved transcript segments, aligned words, current decision, and
  staleness.

### 10.2 Supported operations

- drag either overlap edge;
- nudge by frame, 10 ms, 100 ms, or aligned-word boundary;
- enter exact start/end values;
- extend or shorten one speaker activity;
- optionally adjust the associated transcript segment;
- split an overlap;
- merge adjacent compatible overlaps;
- copy aligned words into a missing speaker segment;
- classify and add a note;
- choose raw, separate, exclude, or needs-work for training;
- audition mixed, Speaker A, and Speaker B audio when available.

### 10.3 Remove-overlap semantics

`Remove overlap` must never be a single destructive action. It opens a choice:

1. Speaker A only — shorten/remove Speaker B activity in the overlap.
2. Speaker B only — shorten/remove Speaker A activity in the overlap.
3. Neither usable — create an exclusion for the range.

The confirmation preview states which activities and transcript bounds will change. Every action
is undoable until Save.

### 10.4 Validation and invalidation

- Bounds stay inside the source and start remains before end.
- Warn before cutting an aligned word.
- Prevent accidental same-speaker region conflicts.
- Text is never silently discarded.
- Changed overlap invalidates recovery, audition approval, clip QC, chapter completion, and
  training packages that depend on its previous range.
- Saving uses the normal optimistic annotation-version contract.

## 11. Admin evaluation

### 11.1 Comparison source of truth

- Hypothesis: immutable raw/aligned model run.
- Reference: selected corrected annotation revision.
- Default scope: human-verified content only.
- Every result displays evaluated duration/words/characters and total eligible coverage.
- Segment IDs are not used to align original and corrected transcripts.

### 11.2 Alignment strategy

1. Select reference ranges from verified segments or completed chapters.
2. Select model words/segments by temporal intersection with those ranges.
3. Deduplicate original model word identities when overlap creates two corrected speaker views.
4. Preserve speaker-specific streams for speaker metrics.
5. Concatenate in stable time order for source and corpus metrics.
6. Produce edit operations for the side-by-side diff.

The report must record the overlap policy so a metric cannot be interpreted without knowing
whether duplicate overlapped speech was included, deduplicated, or excluded.

### 11.3 Metrics

Headline metrics:

- raw WER and CER;
- normalized WER and CER;
- word/character substitutions, insertions, and deletions;
- verified coverage;
- speaker correction rate;
- boundary-change count and absolute shift distribution.

Breakdowns:

- project, source, chapter, speaker, reviewer, quality flag, model revision, and configuration;
- micro aggregate and optional macro diagnostic;
- evaluation sample count and reference denominator beside every percentage.

### 11.4 Arabic normalization

Implement a versioned policy with separately toggled operations:

- Unicode normalization;
- whitespace normalization;
- punctuation handling;
- diacritic removal;
- tatweel removal;
- Alef/Ya/Ta Marbuta policy;
- Arabic/Latin digit policy;
- case folding for non-Arabic text.

Store strict and normalized results. Never discard the original strings in reports. Unit tests use
Arabic, mixed Arabic/English, punctuation, digits, combining marks, empty reference, and RTL text.

### 11.5 Evaluation UI

- original model text left, corrected text right;
- insertions/deletions/substitutions highlighted accessibly, not by color alone;
- click a difference to play its range;
- filters for model run, revision, verification, source, chapter, speaker, and flag;
- aggregate cards and a detailed table;
- JSON and CSV report download;
- clear unavailable state for historical sources without a raw artifact.

## 12. Admin training preparation

### 12.1 Workflow

```text
Select data → Choose target → Configure → Validate → Preview → Generate → Download
```

No package can skip validation. Preview and validation are read-only. Generate records the exact
validated revision map and refuses if any source changed before the job is accepted.

### 12.2 Shared validation gates

Block on:

- missing or unconfirmed rights;
- missing canonical audio;
- invalid or out-of-source bounds;
- empty required transcript text;
- missing speaker role required by the target;
- unverified included content when the profile requires verification;
- unresolved blocking quality flags;
- stale or unresolved overlap decisions;
- missing/invalid channel mapping for stereo output;
- sample duration outside profile bounds;
- excessive silence/noise/overlap under the selected policy;
- an annotation revision changed after validation;
- train/evaluation source leakage.

Warnings require acknowledgement and are stored in the package report.

### 12.3 Whisper target adapter

Configuration includes:

- sample duration range and padding;
- language/task metadata;
- normalization choice;
- include both speakers or selected roles;
- raw overlap, exclude overlap, or approved separated overlap;
- output manifest format.

Output includes short audio samples, corrected text, source and segment provenance, duration,
language, train/evaluation assignment, JSONL/CSV manifests, checksums, and QC summary. A profile may
optionally emit a Hugging Face-compatible manifest.

### 12.4 Moshi target adapter

Configuration includes:

- mono/stereo and channel routing;
- assistant/main speaker mapping;
- word normalization policy;
- alignment requirements;
- overlap policy and separation requirements;
- clip duration constraints.

Reuse and harden the existing Moshi alignment payload builder. Output includes conversation audio,
alignment sidecars, role mapping, original and normalized words, skipped-word report,
train/evaluation JSONL, checksums, provenance, and QC summary.

### 12.5 Dataset splitting

- Split by source, never by segment or chapter.
- Use a deterministic seed/policy recorded in the package.
- Prevent a source from appearing in both train and evaluation sets.
- Show source, hours, samples, speakers, and language distribution before generation.
- Allow a deliberate admin override only with a recorded reason; never silently relax leakage
  prevention.

### 12.6 Preview

Before generation, show a deterministic sample of proposed items plus explicitly selected edge
cases:

- shortest and longest;
- overlap included/excluded/separated;
- lowest alignment confidence;
- most silence/noise;
- mixed-language text;
- boundary near a spoken word.

The admin can audition audio and inspect the exact manifest record and sidecar that would be
written.

### 12.7 Package immutability and provenance

Every generated package contains:

- target and package format version;
- project and source IDs;
- source hashes;
- annotation revision map;
- model run IDs/revisions;
- target-profile snapshot;
- train/evaluation split manifest;
- validation and QC reports;
- normalization policy/version;
- application build and worker protocol versions;
- file checksums and reproducibility metadata.

Generating again creates a new package version. Existing packages are never overwritten.

## 13. Delivery phases

Each phase is independently releasable and must leave the existing workflow functional.

### Phase 0 — Baseline and contracts

- Capture current frontend/backend test results and representative source fixtures.
- Add a small real media fixture with audio and video.
- Define typed playback, chapter, evaluation, overlap, and training contracts.
- Add database backup/migration rehearsal.
- Record performance measurements for a short and a synthetic long source.

Exit criteria:

- baseline tests reproducible;
- no uncommitted schema assumptions;
- migration rollback uses database backup restore, not destructive reverse SQL.

### Phase 0.1 — Transcript durability

- Create a consistent SQLite snapshot containing every saved annotation revision.
- Export all annotation revisions to portable UTF-8 JSONL.
- Preserve registered raw Whisper, aligned transcript, and diarization artifacts.
- Generate a restore-path manifest and SHA-256 inventory.
- Refuse overwrite and verify every completed snapshot independently.
- Replicate snapshots to encrypted storage outside the workspace failure domain.

Exit criteria:

- every saved human revision is recoverable independently of the application UI;
- original and aligned model artifacts match their catalog checksums;
- tampering or incomplete artifacts make verification fail;
- at least one verified snapshot exists outside the live workspace disk.

### Phase 1 — Playback correctness

- Implement the central playback controller.
- Fix Play once and Loop semantics.
- Use canonical audio as master and mute/synchronize video.
- Add range replacement, stop-at-end, seek, pause, and error handling.
- Add component and real-browser media tests.

Exit criteria:

- Play starts and stops at the selected bounds;
- Loop repeats only the selected range;
- no doubled audio;
- audio/video drift remains within the agreed tolerance;
- existing annotation behavior unchanged.

### Phase 2 — Commands and review layout

- Add the command registry, shortcut guide, and command palette.
- Make shortcuts context-aware and accessible.
- Extract the review page modules.
- Implement the sticky player card and left tool rail.
- Add responsive drawer behavior.

Exit criteria:

- every command works through UI and keyboard;
- typing and native control behavior are not intercepted;
- sticky content never obscures the transcript;
- keyboard-only review of consecutive segments is possible.

### Phase 3 — Chapters and long-source performance

- Add chapter migrations and APIs.
- Generate maximum-duration and count-based chapters.
- Add chapter navigation and saved progress.
- Consume precomputed peaks and add peaks v2 windows.
- Virtualize transcript rows and chapter activity regions.
- Implement auto-follow with manual-scroll suspension.
- Replace full-document dirty serialization with an explicit revision/change counter or reducer
  state.

Exit criteria:

- default chapters do not exceed 30 minutes;
- boundaries do not cut transcript segments or aligned words;
- global timestamps remain unchanged;
- a multi-hour source opens without full waveform decode or source-wide DOM growth;
- chapter switching preserves edits, selection, and playback state predictably.

### Phase 4 — Review productivity and resilience

- Add human verification and automatic invalidation after edits.
- Add chapter/source completion percentage.
- Add search and filters.
- Surface the priority quality queue.
- Add IndexedDB draft recovery.
- Add channel audition modes.

Exit criteria:

- verification state survives Save and revision reload;
- progress denominators are visible and correct;
- recovered drafts never overwrite a newer server revision;
- queue navigation opens the correct chapter and segment;
- channel mode is unavailable until routing is verified.

### Phase 5 — Machine transcripts, corrected versions, and final approval

- Add source-local M1/M2/M3 transcript ordinals with producer/model metadata and immutable artifact
  references.
- Backfill existing raw artifacts without reprocessing.
- Add visible corrected versions backed by immutable internal generations and actor/change metadata.
- Implement split Save: primary Save updates the eligible unapproved version; Save as new version
  creates the next version without admin approval.
- Keep replaced generations recoverable for 10 days while crash recovery and Undo remain local.
- Add whole-source submission and admin approve/reject/return tasks; lock approved content.
- Add protected approved-history retention with backup, reference checks, impact preview, and the
  10-day recovery window.
- Deprecate editable segment `model_text` as evaluation authority.
- Add admin authorization tests for machine-transcript artifacts and approval/retention operations.

Exit criteria:

- split/join edits do not change any M transcript;
- normal users cannot access raw machine-transcript artifact endpoints;
- historical sources with missing artifacts show an honest unavailable state;
- existing annotations and exports remain readable;
- Save is idempotent, overwritten generations are recoverable for 10 days, and local Undo remains;
- creating a corrected version needs no admin approval;
- only a fully verified clean latest version can be submitted, and approved content is immutable;
- cleanup cannot delete referenced provenance or any M transcript.

### Phase 6 — Direct overlap editing

- Add overlap review migration and stale-state rules.
- Add the derived overlap lane and editor.
- Add resize, nudge, split, merge, classification, notes, and removal choices.
- Add mixed/A/B audition where artifacts permit.
- Invalidate dependent completion, recovery, QC, and packages after boundary edits.

Exit criteria:

- overlap can be increased, reduced, or semantically removed;
- no action silently deletes text;
- changed boundaries invalidate stale derived results;
- undo/redo and revision conflict behavior cover all overlap actions.

### Phase 7 — Admin evaluation

- Implement temporal comparison independent of segment IDs.
- Implement strict and normalized micro WER/CER.
- Build admin-only Evaluation APIs and UI.
- Add immutable downloadable reports.

Exit criteria:

- metric totals match independently checked fixtures;
- report includes coverage and denominator;
- normal users cannot access admin evaluation data or raw artifact endpoints;
- historical sources with missing artifacts show an honest unavailable state.

### Phase 8 — Admin training preparation

- Add profile/package migrations and admin APIs.
- Adapt the dormant exporter into Whisper and Moshi target adapters.
- Add validation, preview, deterministic split, generation, progress, history, and download.
- Generate immutable checksummed packages with provenance.
- Add end-to-end package validation fixtures.

Exit criteria:

- non-admin access is rejected server-side;
- validation blocks unsafe or incomplete data;
- preview record matches generated record;
- no source leaks across train/evaluation sets;
- rebuilding with identical inputs is reproducible or differences are fully explained by recorded
  build metadata;
- package files pass target-specific schema and media checks.

### Phase 9 — Hardening and rollout

- Run accessibility, RTL, responsive, security, migration, load, and recovery testing.
- Test Nginx range requests and cache behavior in deployment.
- Rehearse package generation failure/retry and stale-input rejection.
- Add operational dashboards and structured audit events.
- Roll out behind feature flags in phase order.

Exit criteria:

- no severity-one accessibility or security findings;
- old sources and exports remain readable;
- failed jobs leave no active partial package;
- rollback disables features without reverting migrated data.

## 14. Test plan

### Frontend unit/component tests

- playback range reducer and end-boundary behavior;
- Play, Loop, pause, seek, and replacement;
- audio/video synchronization and muted video;
- command enablement and focus exclusions;
- chapter generation display and navigation;
- virtual list selection and auto-follow;
- verification invalidation;
- filters, search, and quality queue;
- draft restore/discard/conflict;
- overlap operation previews;
- admin route visibility and loading/error states.

### Backend tests

- every migration from a representative existing database;
- model-run artifact immutability and backfill;
- chapter boundary invariants;
- chapter completion staleness;
- overlap derivation and dependent invalidation;
- WER/CER operations, micro aggregation, coverage, and normalization versions;
- admin authorization for every new endpoint;
- training validation gates;
- deterministic source-level split;
- package checksums, immutability, retry, and cleanup;
- artifact path and ownership protection.

### End-to-end tests

- upload → initialize → review → save → verify → complete chapter;
- select segment → Play once → stop at exact end;
- loop segment while video follows;
- close browser with edits → recover unsaved work → Save;
- modify overlap → stale prior recovery → re-review;
- admin compares raw model run to corrected revision;
- admin validates, previews, generates, and downloads both target profiles;
- normal user cannot reach or call admin workflows.

### Media fixtures

Include:

- audio-only mono;
- video with audible source track;
- independent stereo speakers;
- silence and long source boundary cases;
- overlapped speech;
- Arabic and mixed Arabic/English transcript;
- unaligned and low-confidence words.

## 15. Performance and accessibility budgets

Performance goals:

- opening a long source must not decode the complete canonical WAV to draw its waveform;
- transcript DOM size remains bounded by the virtual viewport;
- chapter navigation responds immediately from cached metadata and shows a loading state for media;
- typing does not serialize the entire annotation on every keystroke;
- playback time updates do not rerender the full transcript list;
- package generation never blocks the web request process.

Accessibility requirements:

- complete keyboard operation;
- visible focus and no shortcut theft from controls;
- dialog focus trapping and restoration;
- diff operations conveyed by text/icon in addition to color;
- screen-reader labels for waveform, ranges, speakers, verification, and progress;
- correct RTL text presentation without reversing time or transport controls;
- reduced-motion support;
- responsive zoom without hidden actions.

## 16. Security, privacy, and audit

- Require admin authorization inside every admin endpoint.
- Scope all normal review resources to the source owner unless an explicit admin path is used.
- Record actor, source, revision, target, and outcome for evaluation reports and training packages.
- Never return filesystem paths to the browser.
- Validate all requested ranges and artifact IDs server-side.
- Keep local drafts user-scoped and purge them on sign-out.
- Make browser draft storage deployer-configurable.
- Require confirmed rights before training package generation.
- Preserve checksums for raw model artifacts and generated packages.
- Treat downloaded packages as sensitive data and avoid public caching.

## 17. Observability

Add structured events and counters for:

- player load/playback errors and media drift corrections;
- chapter generation and regeneration;
- draft recovery offered/restored/discarded;
- annotation conflict rate;
- verification and completion changes;
- overlap edits and stale recoveries;
- evaluation duration and evaluated coverage;
- training validation failures by rule;
- package generation duration, size, retry, and failure class.

Do not log transcript text, passwords, tokens, or raw media URLs containing secrets.

## 18. Rollout and compatibility

- Gate chapters, new player, evaluation, overlap editor, and training preparation independently.
- Deploy additive migrations before enabling each feature.
- Keep current source detail fields while new endpoints are introduced.
- Read old annotations without requiring new fields.
- Backfill model runs and chapter sets lazily in bounded batches.
- Do not rerun processing for missing historical artifacts.
- Preserve existing generic dataset download while training packages mature.
- A package or evaluation captures revision IDs at creation; later edits mark the source newer but
  never mutate the artifact.
- Rollback turns off UI/routes while leaving additive tables intact.

## 19. Definition of done

The program is complete when:

- segment Play and Loop are correct and covered by real media tests;
- one audible playback source keeps video, waveform, transcript, and shortcuts synchronized;
- review tools use the left rail and the player remains visible in a non-obstructive sticky card;
- sources can be navigated by persisted maximum-30-minute or count-based chapters;
- long recordings use peak-backed waveform and transcript virtualization;
- reviewers can search, filter, follow playback, recover drafts, verify segments, and complete
  chapters;
- reviewers can increase, reduce, classify, split, merge, or semantically remove overlap;
- M1/M2/M3 machine transcripts and corrected versions remain independently retrievable and
  auditable;
- administrators can view accurate strict/normalized WER and CER with coverage and diff details;
- administrators can validate and preview target-specific training data;
- immutable Whisper and Moshi training packages include leakage-safe splits, QC, checksums, and
  provenance;
- server-side authorization, migrations, accessibility, and failure recovery pass their test
  matrices;
- existing source review, ownership, export download, and worker behavior have no regression.

## 20. Recommended execution order

1. Baseline contracts and fixtures.
2. Correct Play/Loop and unify playback.
3. Add playback/synchronization tests.
4. Add context-aware commands and command palette.
5. Rebuild the review layout with the sticky player card and left tool rail.
6. Add persisted 30-minute/count-based review chapters.
7. Use precomputed peaks, peaks v2, and virtual transcript rendering.
8. Add verification, completion, search, filters, quality queue, draft recovery, and channel
   audition.
9. Establish immutable M transcripts, corrected-version generations, split Save, and final approval.
10. Add direct overlap editing and stale-dependency handling.
11. Add admin WER/CER evaluation.
12. Add admin training validation, preview, package generation, and history.
13. Complete hardening, staged rollout, and operational monitoring.
