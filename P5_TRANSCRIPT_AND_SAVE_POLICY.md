# P5 Transcript Versions, Saving, and Final Approval Policy

Status: approved product policy  
Approved: 2026-09-11  
Implementation status: complete and deployed on port 80 (2026-09-11)

## 1. Names and identities

The product has three deliberately separate kinds of state.

### Machine transcript versions

`M1`, `M2`, and later values identify successive machine-produced transcripts for the same source.
The number does not identify a model family. For example, M1 may come from Whisper, M2 from
Cohere, and M3 from a trained Whisper model. Newly produced M records store the exact producer,
model revision, configuration fingerprint, producing job, language, artifacts, and checksums.
Historical backfill preserves immutable artifacts and their checksums but explicitly reports
`historical_unknown` when the original model metadata was never stored; it does not invent
provenance from today's configuration.

An M version is immutable. Creating M2 never overwrites M1. A trained model/package has its own
identity; it receives an M number only when it is run against this source and produces a transcript.

### Corrected versions

Human-corrected server records are called **versions**, such as V1 and V2. Do not rename them
checkpoints. A normal reviewer can create a new version without admin approval.

Each visible version points to one current immutable storage generation. Updating an unapproved
version creates a replacement generation atomically, moves the version pointer to it, and keeps the
previous generation in protected recovery storage for 10 days. This preserves recoverability
without presenting every press of Save as another visible version.

### Local recovery and Undo

Unsaved recovery and Undo remain on the user's device. They do not create server versions and are
not available from another browser or computer.

- Automatic crash protection stores the current unsaved annotation in browser IndexedDB.
- The local Undo/Redo action history remains bounded and separate from server version history.
- No local draft reaches the server until the reviewer presses Save.
- A successful Save clears the applicable crash draft only after the server confirms persistence.
- A draft based on an older server generation cannot overwrite newer server work.

The UI must use **Recover unsaved work**, not **Restore version**, for the crash-recovery action.

## 2. Save-button policy

Use a split Save control.

### Primary action: Save

The main button saves changes into the current unapproved corrected version.

- If no corrected version exists, it creates V1.
- If its normalized content is unchanged, it creates nothing.
- If the version is unapproved and not locked for admin review, it updates that version by creating
  a new internal generation.
- The replaced generation remains recoverable on the server for 10 days.
- Concurrent saves use an expected generation/content fingerprint and never silently overwrite
  another user's work.

### Arrow action: Save as new version

The arrow menu contains **Save as new version**. It creates the next visible version immediately;
admin approval is not required. It is appropriate before a major structural change or whenever the
reviewer wants a permanent named point in the corrected history.

The menu also provides **Version history**. Loading an older version opens it as a working copy; it
does not delete later versions. Saving it either updates an eligible unapproved version or creates a
new version, according to the explicit Save action chosen.

### Locked versions

A version submitted for admin review is frozen while its task is pending. An approved version is
permanently immutable. Editing after approval always starts a new unapproved version. If a reviewer
creates a newer version while an approval task is pending, the older task is marked superseded.

## 3. Whole-source verification and admin approval

Admin approval is for the final corrected version of the whole source, not for ordinary Save or
Save-as-new-version operations.

A reviewer can select **Submit latest version for approval** only when:

- the complete source is 100% human verified;
- every applicable chapter is complete and current;
- no blocking quality item remains;
- there are no unsaved browser changes;
- the latest corrected version is based on the current source structure; and
- required rights and routing checks pass.

Submission creates an admin task referencing the exact version generation and content fingerprint.
The admin reviews that fixed content and can:

- approve it as the final version;
- reject it with a required note; or
- return it for correction with identified segments/issues.

Approval stores the admin identity, decision time, note, version/generation identity, and content
fingerprint. A rejected version can be edited again after the pending lock is released; resubmission
creates a new approval task for the new generation.

## 4. Approved-history retention

After whole-source approval, show an explicit admin retention decision with these choices:

1. **Keep all versions.** Preserve every corrected version payload.
2. **Keep the approved version; archive older versions for 10 days, then remove their payloads
   (recommended).**

The recommended action must first show the affected versions and estimated recoverable space. It
must require a fresh verified database backup and must refuse cleanup when an older version is
referenced by an evaluation, training package, unresolved approval task, or other immutable output.

During the 10-day window, an administrator can recover an archived version. After the window, the
large annotation payload may be removed, but permanent audit metadata remains: source, visible
version, actors, timestamps, origin, change summary, content fingerprint, approval/retention
decision, and deletion time.

Cleanup never removes source media, M1/M2/M3 machine transcripts, the approved corrected version,
evaluation reports, training packages, or their provenance. Existing historical records are not
deleted during the P5 migration.

## 5. Storage policy

Large immutable model and alignment artifacts are stored once per M version and referenced by
corrected versions. They are not repeated in every human Save. Corrected version generations store
the mutable human annotation layer and its provenance.

The implementation may compress generation payloads, but recovery must not depend on an unbounded
delta chain. Content fingerprints are calculated after canonical normalization. Frontend no-change
checks improve responsiveness; the server is authoritative and independently prevents no-op saves.

The 10-day recovery generations are internal safety records, not visible corrected versions. An
expiry job removes only eligible expired generations after checking locks and references.

## 6. Required UI language

Use these labels consistently:

- `Machine transcripts: M1, M2, ...`
- `Current corrected version: Vn`
- `Save`
- `Save as new version`
- `Version history`
- `Automatic crash protection active`
- `Recover unsaved work`
- `Submit latest version for approval`
- `Pending admin approval`
- `Approved final version`

Do not call corrected versions checkpoints. Do not call local crash protection a server save. Do not
use an M number as shorthand for a model name.

## 7. P5 release gate

- M numbers are source-local transcript ordinals and multiple producers can coexist.
- Every M version is independently retrievable and immutable.
- Save updates only an eligible unapproved version; Save as new version needs no admin approval.
- No-op and concurrent saves cannot create duplicate or destructive state.
- Every overwritten generation is recoverable for 10 days.
- Local crash recovery and Undo remain browser-side and create no server version.
- Only a clean, fully verified whole-source version can enter the admin approval queue.
- Pending content is frozen; approved content can never be overwritten.
- Retention cleanup requires backup, reference checks, an explicit admin decision, and a 10-day
  recovery window.
- Selecting Keep all cancels any earlier pending archive decision for the source.
- Historical machine snapshots expose artifact checksums without claiming unavailable model
  provenance, and retrieval rejects a snapshot whose checksum no longer matches.
- Evaluation and training provenance can identify both the selected M version and the exact approved
  corrected version generation.
