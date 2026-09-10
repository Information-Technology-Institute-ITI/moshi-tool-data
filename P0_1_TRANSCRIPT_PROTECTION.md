# P0.1 Transcript Protection

Status: local protection complete; off-machine replication target pending  
Implemented: 2026-09-09  
Snapshot format: `moshi.transcript-protection/v1`

## Protected records

Each snapshot contains:

- a consistent SQLite online backup of `catalog.sqlite3`, including every saved user annotation
  revision;
- `annotation_revisions.jsonl.gz`, a portable UTF-8 export of every original and corrected saved
  annotation revision;
- every registered `analysis.raw_transcript`, `analysis.aligned_transcript`, and
  `analysis.diarization` artifact;
- `manifest.json`, mapping compact snapshot filenames back to source IDs, producing jobs, roles,
  and workspace-relative restore paths;
- `SHA256SUMS`, covering the database, revision export, artifact copies, and manifest.

Snapshot creation refuses to overwrite an existing destination. A snapshot is marked complete only
when SQLite integrity and foreign-key checks pass and every transcript artifact matches its
registered size and SHA-256.

## First protected snapshot

Location:
`studio_workspace/backups/transcript-protection-20260909T165431Z/`

| Record | Captured |
| --- | ---: |
| Projects | 22 |
| Sources | 54 |
| Saved annotation revisions | 2,472 |
| Raw/aligned/diarization artifacts | 156 |
| Artifact problems | 0 |
| Snapshot bytes | 1,111,070,578 |

An independent verification pass reported `valid: true` with no problems. The snapshot is ignored
by Git because it contains private transcript and account data.

## Commands

Create a new snapshot at an explicit, unused destination:

```powershell
.venv\Scripts\python.exe deployment\protect-transcripts.py create `
  studio_workspace `
  <protected-destination>\transcript-protection-<UTC-timestamp>
```

Verify it independently:

```powershell
.venv\Scripts\python.exe deployment\protect-transcripts.py verify `
  <protected-destination>\transcript-protection-<UTC-timestamp>
```

[`moshi_data_pipeline/studio/transcript_protection.py`](moshi_data_pipeline/studio/transcript_protection.py)
contains the snapshot and verification implementation. The deployment entry point is
[`deployment/protect-transcripts.py`](deployment/protect-transcripts.py).

## Restore procedure

1. Run snapshot verification and stop if any checksum, JSON, SQLite integrity, or foreign-key check
   fails.
2. Stop the web service and every worker that can write to the workspace.
3. Preserve the damaged workspace for diagnosis; do not overwrite the only remaining copy.
4. Restore `catalog.sqlite3` to the configured workspace.
5. Restore each transcript artifact using the manifest's `snapshot_relative_path` to
   `source_relative_path` mapping.
6. Run verification against the restored database and artifact hashes before restarting writers.
7. Open representative source histories and confirm both the initial model text and latest human
   corrections.

Restoration is deliberately documented rather than automated because it overwrites live data and
must require an explicit operator decision.

## Remaining failure-domain protection

The first snapshot is stored on the same workspace disk. It protects against accidental edits,
bad migrations, and individual-file corruption, but not loss of the whole disk or server.

To close that gap, create future snapshots directly on an encrypted destination on another volume
or server, apply a retention policy, and monitor the exit status of both `create` and `verify`.
No private snapshot should be copied into Git or an unencrypted shared folder.

The user explicitly directed P1 to proceed after the verified local snapshot. External replication
therefore remains a visible operational follow-up and does not silently appear as completed.
