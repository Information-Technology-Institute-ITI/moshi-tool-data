# Working notes for Claude

Orientation for an agent working in this repository. `README.md` covers the
product, `REPOSITORY_STRUCTURE.md` the layout, `PLAN.md` the original design.
This file holds what none of those record: the traps, the commands that actually
work here, and where the current work stands.

## What this is

A two-machine system for turning two-person Arabic podcasts into a Moshi
fine-tuning dataset.

- **m8i web host** — FastAPI plus a React review UI. Owns the SQLite catalog,
  the workspace, jobs and artifacts.
- **g4dn GPU host** — WhisperX and Pyannote. Owns only a processing cache and
  temporary execution state.

They must **never share a database or workspace**. The web host pushes jobs to
the GPU host and the GPU host reports back through `/internal/v1/` endpoints.

## Traps that have cost real time

**The React bundle is committed to git.** The Python service serves a pre-built
bundle from `moshi_data_pipeline/studio/static/assets/`. A frontend change is
invisible on the deployed site until the bundle is rebuilt *and committed*.
Always finish frontend work with `vite build`, then commit the new
`index-*.js` / `index-*.css` and the updated `index.html`.

**`core.autocrlf=true` with no `.gitattributes`.** `git status` reports
byte-identical bundles as modified. Use `git diff --quiet` or a sha256 compare
to decide whether something really changed; never trust `git status` for these.

**Two sample-rate systems.** Annotation timestamps are **samples at 24 000 Hz**.
`aligned_words` entries carry `start` / `end` in **seconds** and both are
nullable. Mixing them silently produces plausible, wrong offsets.

**Canonical audio, not the original.** Timestamps are made against the canonical
24 kHz mono conversion at `paths.canonical_audio(source_id)`. Anything that cuts
audio must use that file, not the upload.

**Secure-context browser APIs are unavailable.** The deployment is served over
plain HTTP, so `crypto.randomUUID` and friends are `undefined` there but work on
`127.0.0.1`. This class of bug cannot be caught locally — check availability
before using any secure-context API. `sampleId` in `frontend/src/api.ts` is the
worked example.

**Local worker versus GPU dispatch take different paths.** `worker.py` runs jobs
in-process and never compares fingerprints. GPU dispatch goes through
lease → heartbeat → complete, which does. A bug that only bites in production
may well live in the second path only.

**`require_sign_in` decides whether authorization runs at all.** Without it a
local-admin principal answers every request and no admin route is gated. Any
test asserting a 401/403 must build the app with
`auth_settings=AuthSettings(..., require_sign_in=True)`, or it passes
vacuously. Production sets `MOSHI_REQUIRE_SIGN_IN=1`.

**`GET /api/sources/{id}/annotations` returns an envelope**
(`{annotation, revisions}`), not a bare annotation.
`GET /api/sources/{id}/annotations/{version}` *does* return a bare one.

## Commands

Everything below is installed inside the repo and gitignored, so the whole
working set can be deleted with `.venv`, `.runtime`, `frontend/node_modules`
and `local_data`.

```bash
# Backend
./.venv/Scripts/python.exe -m pytest tests/test_studio_api.py -q
./.venv/Scripts/python.exe -m ruff check .

# Frontend (from frontend/)
./node_modules/.bin/tsc --noEmit -p tsconfig.json
./node_modules/.bin/vitest run
./node_modules/.bin/vite build      # writes into studio/static — commit the result

# Local demo server on http://127.0.0.1:8099
./.venv/Scripts/python.exe local_data/seed_demo.py   # reseeds the workspace
./.venv/Scripts/python.exe local_data/run_demo.py
```

`local_data/` is gitignored and holds local-only helpers: `seed_demo.py`
(two accounts, a dataset, real 24 kHz audio, an annotation exercising the
split-by-turns and overlap cases), `run_demo.py`, `seed_users.py`, `smoke.sh`.
Sign in as `editor@example.test` or `admin@example.test`, password
`TestPassword123!`.

**Bare `pytest` does not work.** Fifteen test modules import numpy or
soundfile, which this web-only venv deliberately lacks; they fail at collection.
Run the web-service modules by name, or add `--continue-on-collection-errors`.
`tests/test_gpu_dispatcher.py` contains a threaded test with a 3-second wait
that flakes when the machine is loaded — do not run a full backend suite and a
frontend build at the same time.

**Shell.** PowerShell aliases `curl` to `Invoke-WebRequest`, so use `curl.exe`;
`bash` resolves to WSL, so use `C:\Program Files\Git\bin\bash.exe`. Signed-in
API calls need an `Origin` header matching the host or they are refused.

## Conventions

- Every behaviour change gets a test, and a bug fix gets a test **verified to
  fail against the old code** — revert the fix, watch it fail, restore. Several
  tests here previously passed against broken code because they encoded an
  assumption rather than the real contract.
- Pure, testable logic lives in `frontend/src/transcript.ts`; React components
  stay thin. Backend equivalents go in their own module
  (`dataset_export.py`, `normalization.py`, `activity.py`).
- Comments explain *why*, in prose. No decorative headers, no restating the code.
- Commit messages are prose, in the user's voice, describing behaviour and
  reasoning. **Never add Claude co-author or "generated with" trailers.**
- Never push, merge, or open a PR without being asked.

## Domain model

- A **dataset** is a `Project`; it holds many **sources** (audio files).
- `annotation.activities` (speaker A/B rectangles on the timeline) and
  `annotation.transcript` (the segment list) are **separate lists**. A structural
  transcript edit must sync the lanes — `syncActivities` in `transcript.ts`.
- Saving uses optimistic concurrency: `expected_version` against immutable
  `annotation_revisions` rows. A 409 means someone else saved first.
- Transcript segments **may overlap**; nothing rejects that. It is how
  overlapped speech is represented, one segment per speaker.

## Current work

Branch **`plan-num-2`**, off `plan-num-1`. Implements the markdown plans in
`../moshi-plans/` — 01, 02, 07, 08, 09 are done; **plan 11 is deliberately
excluded**.

Branch lineage: `main` → `version_3` = `coherex-try-1` → `deploy_v1` →
`deploy-v1-without-terraform` / `WhisperX-Dep` → splits into `WhisperX-Cleaned`
(GPU) and `Web-Service-Cleaned` = `refine-ui-ux` (web) → `plan-num-1` →
`plan-num-2`.

The two "Cleaned" branches are **deployment artifacts, not feature branches**.
Merging them would put CUDA and torch on the web host. Kept in sync by hand.
`WhisperX-Cleaned` does **not** contain the React app or the web service's
job-orchestration modules, so web-side changes need no port — check with
`git cat-file -e origin/WhisperX-Cleaned:<path>` before claiming otherwise.

Deployment is `git pull` plus a service restart on the web host. No build step
(the bundle is committed), no new dependencies, nothing on the GPU host.

### Decisions worth not relitigating

- Processing routes were removed from the web service so no website request can
  reach SpeechBrain. `POST /api/sources/{id}/initialize` is the only remaining
  entry point.
- Segments are divided to match speaker-lane rectangles **once when a source
  opens**, not continuously — otherwise it would undo a join the reviewer just
  made. It converges, so reopening changes nothing.
- Typing in a segment's text box is **held**, not autosaved. It is sent on blur,
  on moving to another segment, on explicit save, and on navigation. Every other
  edit is one deliberate act and still autosaves on a 1.2 s debounce.
- Quality flags are read-only readouts of what the pipeline produced. The
  reviewer cannot edit them.
