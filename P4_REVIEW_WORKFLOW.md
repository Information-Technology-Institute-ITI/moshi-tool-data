# P4 Review Workflow

P4 is implemented and its automated gate completed on 2026-09-10. It builds on P3 chapters; it
does not split, re-encode, or duplicate the source media.

## Reviewer workflow

- Each transcript segment can be verified, unverified, or verified and advanced to the next
  visible segment.
- Text, speaker, timing, split, join, and overlap-context changes invalidate verification. The
  backend repeats this check so a stale or modified client cannot preserve an invalid approval.
- The left rail shows eligible, verified, blocked, and percentage values for the active chapter
  and source. A chapter can be completed only from a clean saved revision.
- Completion belongs to the reviewer and annotation revision. When a newer revision creates a
  replacement chapter set, the previous completion is retained and shown as stale.

## Discovery and quality review

- Reviewers can combine transcript-text, speaker, quality-flag, verification, alignment,
  overlap, empty-text, and edited/original filters. The P3 chapter selector provides the chapter
  scope.
- The quality queue uses the same weights and ordering as the backend. It separately labels the
  current working-copy count and the last saved server count.
- Opening a queue item selects its chapter, seeks to its global timestamp, and selects the exact
  segment.
- Playback follows the active transcript segment. Mouse-wheel or touch scrolling suspends that
  behavior until **Return to playhead** is selected.

## Crash recovery and data safety

- Unsaved working copies are stored in IndexedDB by user, source, and base revision after a short
  debounce. This does not invoke server Save or create annotation revisions.
- A recovery copy can be inspected, restored, or discarded. Restore is disabled when its base
  revision differs from the current server revision, preventing it from overwriting newer work.
- A successful explicit Save purges drafts for that source. Sign-out purges that user's drafts.
- Deployments can disable browser drafts with `MOSHI_DISABLE_LOCAL_DRAFTS=1`.

## Channel audition

- Mixed, Left, Right, Speaker A, and Speaker B audition modes appear only when an independent
  stereo artifact exists and its speaker/channel mapping has been verified.
- Auditioning uses the stereo channel artifact through an in-browser audio graph. It changes only
  monitoring gain; annotation routing and the canonical media are never modified or saved.
- Canonical audio remains the playback master when verified channel data is unavailable.

## Verification evidence

- Backend: 243 pytest checks passed.
- Frontend: 219 Vitest checks passed, including exclusive chapter assignment, short-recording
  guidance, P4 workflow interactions, and transcript mutation rules.
- Static analysis: Ruff on changed backend files.
- Production bundle: TypeScript checks and Vite build.

The Save/version discussion is now resolved by
[P5_TRANSCRIPT_AND_SAVE_POLICY.md](P5_TRANSCRIPT_AND_SAVE_POLICY.md). P4 continues to preserve the
current explicit Save behavior until the P5 migration and UI are implemented.
