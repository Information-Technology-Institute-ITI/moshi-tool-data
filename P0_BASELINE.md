# P0 Baseline Record

Date: 2026-09-09  
Baseline commit: `50206f44c129c8ecfe925d1596208a3601a95dd8`  
Status: complete

This record freezes the safety, contract, media, migration, and performance baseline before P1
changes playback behavior. The P0 additions are scaffolding only; they do not add routes, catalog
tables, or user-visible behavior.

## Environment

| Component | Version |
| --- | --- |
| Windows Python | 3.11.15 |
| pytest | 9.1.1 |
| FastAPI | 0.140.13 |
| Pydantic | 2.13.4 |
| Node.js | 24.14.1 |
| npm | 11.11.0 |
| React | 19.2.8 |
| TypeScript | 5.9.3 |
| Vite | 7.3.6 |
| Vitest | 3.2.7 |
| WaveSurfer | 7.12.11 |
| FFmpeg / ffprobe | 8.1 |

## Verification results

| Check | Result |
| --- | --- |
| Backend suite | 233 passed in 46.85 s |
| Frontend suite | 173 passed across 10 files in 5.65 s |
| Frontend production build | Passed; TypeScript and Vite build completed |
| New Python lint surface | Ruff passed |
| SQLite migration rehearsal | Passed; integrity `ok`, no foreign-key errors, all counts preserved |
| Browser media smoke | Passed in installed headless Chrome; H.264/AAC MP4 reached `canplay` with valid decoded metadata |

Production bundle baseline:

| Asset | Raw | Gzip |
| --- | ---: | ---: |
| HTML | 0.54 kB | 0.34 kB |
| CSS | 29.82 kB | 6.73 kB |
| JavaScript | 333.62 kB | 101.83 kB |

## Deterministic fixtures

[`tests/support/media_fixtures.py`](tests/support/media_fixtures.py) generates all media from
mathematical tones and color frames, so it contains no production speech or private data:

- 24 kHz mono PCM WAV;
- independent left/right stereo PCM WAV with a deterministic central overlap;
- 160 x 90 H.264 video with 24 kHz AAC audio;
- a three-hour logical annotation timeline without a multi-hour binary asset.

[`tests/support/browser_media_smoke.html`](tests/support/browser_media_smoke.html) loads the MP4 in
a real browser. It validates finite positive duration, decoded video dimensions, and `canplay`.
Exact Play/Loop range semantics remain a P1 responsibility.

## Versioned contracts

The matching backend and frontend definitions live in
[`moshi_data_pipeline/studio/product_contracts.py`](moshi_data_pipeline/studio/product_contracts.py)
and [`frontend/src/productContracts.ts`](frontend/src/productContracts.ts).

| Contract family | Version | Defined surface |
| --- | --- | --- |
| Review | `studio.review/v1` | playback range/state, chapters, peak windows, overlap review |
| Evaluation | `studio.evaluation/v1` | immutable model-run summary, scope, WER/CER result and coverage |
| Training | `studio.training/v1` | target profile, validation, package summary |
| Errors | `studio.error/v1` | stable code/message, optional field/context, retryability |

All sample ranges are half-open (`start_sample` inclusive, `end_sample` exclusive) and use the
canonical source sample clock. Routes and persistence introduced in later phases must use these
types or explicitly version a replacement.

## Interaction-performance baseline

Command: `node frontend/scripts/p0-review-baseline.mjs`

The benchmark measures the current whole-document serialization and transcript sort/filter work.
It is a deterministic CPU comparison, not a browser-paint benchmark.

| Case | Segments | Aligned words | Serialized size | Median stringify | Median sort/filter |
| --- | ---: | ---: | ---: | ---: | ---: |
| Short source | 120 | 1,800 | 141,076 bytes | 1.192 ms | 0.020 ms |
| Long source | 7,200 | 108,000 | 9,147,448 bytes | 46.249 ms | 258.832 ms |

The long case makes the P3 target concrete: remove render-time whole-document serialization and
linear `includes` filtering, then bound transcript and waveform work to the chapter/viewport.

## Migration rehearsal and rollback

The representative catalog was `studio_workspace/catalog.sqlite3` (920,973,312 bytes). It was
opened read-only and copied with SQLite's online backup API. On the copy,
[`deployment/rehearse-migrations.py`](deployment/rehearse-migrations.py) cleared only the migration
ledger, replayed migrations 1 through 7, and compared every application table before and after.

Results:

- schema version stayed at 7;
- 22 projects, 54 sources, 2,471 annotation revisions, 52 jobs, and 368 artifacts were preserved;
- all remaining application-table row counts were also identical;
- `PRAGMA integrity_check` returned `ok`;
- `PRAGMA foreign_key_check` returned no rows.

The local detailed report and rehearsal database are under `.runtime/p0-baseline/` and are ignored
by Git because they may contain representative workspace data.

For a deployment rollback:

1. Stop the web service and workers so no writer retains the catalog.
2. Create and retain an immutable timestamped SQLite backup before applying migrations.
3. Apply and verify migrations on a separate copy first.
4. If rollout verification fails, keep the failed catalog for diagnosis and restore the retained
   pre-migration backup to the configured catalog path.
5. Run SQLite integrity and foreign-key checks, compare the recorded table counts, then restart the
   service and workers.

Never use the Git-ignored rehearsal copy as the only production backup.

## Reproduction commands

```powershell
.venv\Scripts\python.exe -m pytest
Set-Location frontend
npm run test
npm run build
Set-Location ..
node frontend\scripts\p0-review-baseline.mjs
.venv\Scripts\python.exe deployment\rehearse-migrations.py `
  studio_workspace\catalog.sqlite3 `
  .runtime\p0-baseline\migration-rehearsal.sqlite3 `
  --report .runtime\p0-baseline\migration-rehearsal.json `
  --replay-current
```

Use a new rehearsal destination each time; the script deliberately refuses to overwrite an
existing database.
