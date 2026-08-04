# Moshi data pipeline

This project provides a local, single-user website for turning two-person mixed
podcast videos into a manually verified dataset for the official
[Kyutai Moshi fine-tuning repository](https://github.com/kyutai-labs/moshi-finetune).
The older command-line pipeline remains available, but source annotation is now
the authority for v2 clipping, stereo rendering, review, and immutable export.
This is a separate project: it does not modify a Moshi checkout.

The format was verified against `kyutai-labs/moshi-finetune` commit
`2acc879fe7c48f885a18f6cc9548bccb2674d87b`. That repository expects:

- A stereo WAV, with Moshi/assistant audio on the left and user audio on the right.
- A JSONL row such as `{"path":"data_stereo/a.wav","duration":24.5}`.
- A same-stem JSON object containing `alignments`, where the training interleaver
  recognizes `SPEAKER_MAIN`.

The nearby `nu-dialogue/moshi-finetune` fork uses a different preprocessing format.
This pipeline intentionally targets Kyutai's official format.

## Safety and data rights

Successfully downloading or processing a video or podcast does **not** establish
permission to use it for model training. Check copyright, contractual, privacy,
voice/biometric, and data-protection obligations for every source and jurisdiction.
Do not train on material without the necessary rights and participant consent.

The default policy rejects serious overlap and admits only `PASS` clips to
`train.jsonl`. `REVIEW` clips need explicit human approval. Original media is never
deleted.

## System requirements

- Windows 11
- Python 3.10 or 3.11 (3.11 is recommended)
- FFmpeg and ffprobe in `PATH`
- 16 GB RAM
- NVIDIA GPU with a current driver; 6 GB VRAM is usable but tight
- Internet access for the first model download
- A Hugging Face account/token and accepted pyannote model conditions

The core audio/QC/test suite is CPU-only. WhisperX and pyannote are an optional
`ml` dependency set, overlap recovery is in `separation`, and the local review
page is in `review`. The pinned and API-checked versions are WhisperX 3.8.6,
faster-whisper 1.2.1, pyannote.audio 4.0.7, PyTorch/torchaudio 2.8.0, SoundFile
0.14.0, SpeechBrain 1.1.0, FastAPI 0.140.13, and NumPy 2.2.6. WhisperX 3.8.6 maps Arabic alignment to
`jonatasgrosman/wav2vec2-large-xlsr-53-arabic`; model quality and availability are
external dependencies, so unaligned words are reported, never timestamped by guess.

## Windows installation

Create and activate an isolated environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Install the PyTorch 2.8 CUDA 12.6 wheels using the command documented by PyTorch:

```powershell
python -m pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 `
  --index-url https://download.pytorch.org/whl/cu126
```

Then install the package, remaining ML dependencies, and development tools:

```powershell
python -m pip install -e ".[ml,separation,review,dev]"
```

The selected PyTorch wheel includes the matching CUDA runtime libraries and cuDNN;
a compatible NVIDIA driver is still required. Installing a full CUDA toolkit is
usually unnecessary for wheel-based inference. If another CUDA wheel is needed,
use the official [PyTorch selector](https://pytorch.org/get-started/locally/) and
keep `torch`, `torchaudio`, and `torchvision` on matching versions. Do not let a
later package install replace the verified PyTorch build.

faster-whisper uses CTranslate2 rather than PyTorch for transcription. Its current
GPU runtime separately requires CUDA 12 cuBLAS and cuDNN 9 DLLs to be discoverable
through Windows `PATH`. The PyTorch import succeeding does not prove CTranslate2
can find those DLLs. Follow the
[faster-whisper GPU requirements](https://github.com/SYSTRAN/faster-whisper#gpu),
restart the terminal after changing `PATH`, and smoke-test both runtimes:

```powershell
python -c "import torch; print('torch CUDA:', torch.cuda.is_available())"
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cuda', compute_type='int8'); print('CTranslate2 CUDA: OK')"
```

For CPU-only setup, install the matching CPU wheels first, then the project:

```powershell
python -m pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 `
  --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e ".[ml,dev]"
```

Verify Python, FFmpeg, and CUDA:

```powershell
python --version
ffmpeg -version
ffprobe -version
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
python -c "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

### Hugging Face and pyannote

1. Open the
   [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1)
   model page and accept its user conditions.
2. Create a Hugging Face access token with permission to download the model.
3. Set it for the current PowerShell process:

```powershell
$env:HF_TOKEN = "hf_..."
```

Or copy `.env.example` to `.env` and fill in `HF_TOKEN`. The project loads only
that key from the ignored workspace `.env` when the process environment does not
already define it. Tokens are never accepted on the command line and logs show
only `<configured>`.

pyannote.audio 4 uses the `token=` API and `community-1`; older instructions using
`use_auth_token=` or `speaker-diarization-3.1` do not describe the pinned runtime.

## Configuration

Copy `config.example.yaml` and edit it. CLI options override YAML values:

```powershell
Copy-Item config.example.yaml config.yaml
```

The legacy CLI defaults use a relaxed, higher-yield policy:

- One file at a time; batch mode is sequential.
- Balanced `large-v3`, Arabic, `compute_type=float16`, 20-second chunks, batch size 1.
- `device=auto`: CUDA when `torch.cuda.is_available()`, otherwise CPU.
- Exactly two requested diarization speakers.
- 20–50 second clips, 40-second target, at least one second and 2% per speaker.
- Maximum 20% overlap and 80% silence.
- Raw overlap up to 5% passes, 5–20% requires review, and larger overlap is rejected
  unless optional separation confidently recovers it.
- PCM-16, 24 kHz, two-channel output with 10 ms mask-boundary fades.

The v2 Studio keeps the same QC thresholds but uses the product-level 20–100
second hard clip range and requires an explicit planning mode.

If `large-v3` runs out of VRAM, the pipeline fails and recommends an explicit
`--model medium` retry. It never silently changes a model because that would make
the result irreproducible.

## Commands

### Dataset Studio v2

Start the website and its one persistent local GPU worker:

```powershell
python -m moshi_data_pipeline web --workspace "studio_workspace"
```

Then visit `http://127.0.0.1:8765`. The workspace contains the SQLite catalog
and immutable originals, proxies, canonical 24 kHz audio, recovered stems, clip
artifacts, and versioned exports. Keep the terminal open while using the studio;
unfinished jobs are returned to the queue after a restart.

The guided workflow is:

1. Create a dataset project and upload one or more podcast videos.
2. Choose manual or assisted initialization for each source.
3. Mark independent Speaker A/B activity and exclusions, select the Moshi speaker,
   choose clean reference turns for stable speaker identity, edit the RTL transcript,
   and save a versioned annotation.
4. Work through the prioritized transcript queue. Generate second-pass candidates
   only for unresolved high-risk utterances, realign corrections, and verify them
   against the audio.
5. Recover short overlap regions, optionally transcribe the isolated stems for
   comparison, and explicitly approve or reject every recovery.
6. Choose clip count, target duration, or manual boundaries and refine the proposal.
7. Generate, listen to, and approve every final stereo clip.
8. Record source origin and rights, validate the project, and create an immutable
   export.

Human-verified utterances form a reusable golden regression set. The accuracy
dashboard reports model character error, speaker correction rate, Moshi alignment
coverage, unresolved review count, and overlap approval coverage. Configure
`transcription.review_model` to a different locally cached Whisper model for an
independent second opinion; otherwise the main model runs with the stricter review
decode.

The clip planner has no automatic default. Every export clip must be 20–100
seconds, contain both speakers and an exchange, and pass the configured
silence/speaker-balance checks. Annotation or overlap-decision changes invalidate
downstream clip approvals.

The site binds to loopback by default. Remote binding requires both a non-loopback
`--host` and the explicit `--allow-remote` flag. Interactive API documentation is
available at `http://127.0.0.1:8765/api/docs`.

### Legacy command-line workflow

Process one file:

```powershell
python -m moshi_data_pipeline process `
  --input "raw\episode_001.mp4" `
  --output "data" `
  --quality-profile balanced `
  --language ar `
  --assistant-speaker SPEAKER_00
```

Attempt safe, overlap-only recovery. Every clip using recovered overlap remains
`REVIEW` and requires human approval:

```powershell
python -m moshi_data_pipeline process `
  --input "raw\episode_001.mp4" `
  --output data `
  --quality-profile balanced `
  --separate-overlap `
  --interactive-speaker `
  --resume
```

Process a directory sequentially:

```powershell
python -m moshi_data_pipeline batch `
  --input-dir "raw" `
  --output "data" `
  --language ar `
  --speaker-mapping "speaker_mapping.json" `
  --resume
```

Inspect or extract without loading ML models:

```powershell
python -m moshi_data_pipeline inspect --input "raw\episode_001.mp4"
python -m moshi_data_pipeline extract `
  --input "raw\episode_001.mp4" `
  --output-wav "data\working\episode_001\source_mono.wav"
```

Force one stage and every downstream stage to run again:

```powershell
python -m moshi_data_pipeline process `
  --input "raw\episode_001.mp4" `
  --output data `
  --assistant-speaker SPEAKER_00 `
  --resume `
  --force-stage transcribe
```

List stage names:

```powershell
python -m moshi_data_pipeline stages
```

Open the legacy, output-only review page:

```powershell
python -m moshi_data_pipeline review --output data
```

Then visit `http://127.0.0.1:8765`. The page plays the stereo clip, mutes either
channel, renders the speaker/overlap timeline, edits Arabic text in RTL, relabels
speakers, records approvals, and can rebuild alignment and downstream artifacts.

Compare balanced `int8` and `float16` decoding against a manually corrected
gold file. Benchmark media is extracted into an isolated temporary directory,
so production output and caches are not changed:

```powershell
python -m moshi_data_pipeline benchmark `
  --input "raw\episode_001.mp4" `
  --gold "gold\episode_001.json" `
  --config "config.example.yaml" `
  --output-json "benchmark.json"
```

The report promotes `float16` only when it improves normalized CER by at least
5%, stays at or below 2× realtime, and remains below 5.5 GiB peak GPU memory.

`--keep-working-files` is accepted for workflows that add optional diagnostic
intermediates. Required stage caches are always kept because deleting them would
break reliable resume behavior.

## Pipeline stages

1. `inspect` uses ffprobe and FFmpeg during a full decode, including a stereo
   difference measurement that identifies dual-mono inputs.
2. `extract` writes one mono, 24 kHz PCM-16 working WAV. It does not trim silence.
3. `transcribe` runs balanced WhisperX/faster-whisper decoding, detects suspicious
   repetitions/rates/confidence, retries only suspect spans, and saves both decodes.
4. `align` accepts automatic or manually corrected segment text, produces real word
   timestamps, and flags low-confidence Latin-script alignment.
5. `diarize` uses pyannote Community-1 directly. It preserves overlap-aware turns
   for overlap handling and exclusive turns for word assignment, with safe
   same-speaker micro-turn merging.
6. `select-speaker` uses an explicit label, JSON mapping, or an interactive choice.
   `SPEAKER_00` is never assumed to be the host.
7. `segment` scores valid turn boundaries by overlap, duration, and speaker balance.
8. `render-stereo` reads only the source interval needed for one clip. It writes
   assistant intervals left, other-speaker intervals right, and preserves silence
   and source timing.
9. `generate-json` emits only left/assistant words with clip-relative timestamps.
10. `validate` checks the WAV, schema, timestamps, per-channel silence, speaker
    balance, clipping, leakage, unresolved hallucinations, uncertain assignments,
    separation coverage, alignment, and low-confidence ratios.
11. `manifest` atomically and deterministically rebuilds `train.jsonl`.

Each completed stage records an input fingerprint (path, size, nanosecond mtime,
and SHA-256 of file edges), a configuration fingerprint, and expected outputs in
`working/<episode>/state.json`. `--resume` skips only matching, complete outputs.
`--force-stage` invalidates that stage and all stages after it.

The raw, alignment, and diarization models are never intended to coexist on the
GPU. Each backend uses lazy imports, deletes its model, runs garbage collection,
and empties the CUDA allocator cache before the next stage.

## Assistant selection

Explicit:

```powershell
--assistant-speaker SPEAKER_01
```

Mapping file:

```json
{
  "episode_001": "SPEAKER_00",
  "episode_002": "SPEAKER_01"
}
```

If neither is supplied in an interactive terminal, the pipeline prints up to five
sample time ranges per detected speaker and prompts for the label. Listen to those
ranges in the source/working audio before selecting. A non-interactive job without
a mapping fails clearly.

## Output

Studio exports are immutable and versioned:

```text
studio_workspace/
├── catalog.sqlite3
├── originals/                         # immutable uploaded media
├── sources/<source-id>/
│   ├── canonical.wav
│   ├── proxy.mp4
│   ├── peaks.json
│   ├── recovery/annotation_v<revision>_<id>/
│   └── clips/annotation_v<revision>_<id>/
└── exports/<dataset-name>_v<version>/
    ├── train.jsonl
    ├── eval.jsonl
    ├── data_stereo/
    │   ├── <clip-id>.wav
    │   └── <clip-id>.json
    ├── qc_summary.json
    ├── provenance.json
    ├── config.snapshot.json
    ├── golden_regression.jsonl
    └── reproducibility.json
```

`reproducibility.json` records the complete configuration fingerprint, model
identifiers and discoverable cached revisions, pinned dependency versions,
runtime versions, and SHA-256 hashes for exported artifacts.

Train/evaluation assignment is deterministic at whole-source level. A project
with fewer than two approved sources exports training data but leaves `eval.jsonl`
empty and reports that a leakage-free evaluation split is not yet possible.

The legacy CLI output layout is:

```text
data/
├── train.jsonl
├── data_stereo/
│   ├── conversation_001.wav
│   └── conversation_001.json
├── reports/
│   ├── episode_001_diarization.json
│   ├── episode_001_transcript.json
│   ├── episode_001_qc.json
│   ├── episode_001_performance.json
│   ├── episode_001_inspection.json
│   ├── review_corrections/
│   ├── rejected_clips.jsonl
│   └── logs/episode_001.log
└── working/
    └── episode_001/
        ├── source_mono.wav
        ├── raw_transcript.json
        ├── aligned_transcript.json
        ├── config.snapshot.json
        └── state.json
```

A sidecar follows the official contract:

```json
{
  "alignments": [
    ["ازيك", [0.52, 0.81], "SPEAKER_MAIN"],
    ["عامل", [0.84, 1.11], "SPEAKER_MAIN"]
  ]
}
```

All times are finite clip-relative seconds, ordered, positive-duration, and bounded
by the real WAV length. Right/user-channel text is excluded. Individual sidecars
are UTF-8, pretty-printed, and validated with
`moshi_data_pipeline/schemas/moshi_alignment.schema.json`.

## Diarization and optional overlap recovery

Diarization estimates **who spoke when**. The source is still a mono mixture. A
time mask can route clean single-speaker intervals, but it cannot recover two
independent voices from overlap. By default, the pipeline removes mixed overlap
from both channels.

`--separate-overlap` enables SpeechBrain SepFormer only on overlap windows, with
one second of context and a 12-second maximum inference window. ECAPA speaker
embeddings match each separated stem to clean enrollment speech. Ambiguous or
failed identity matches fall back to omitted overlap without failing the file.
Coverage below 95% is rejected only above the 20% overlap ceiling, and every
successfully recovered clip is always `REVIEW`.

## QC statuses and manual review

- `PASS`: all mandatory checks pass and no warning threshold is crossed.
- `REVIEW`: usable-looking artifact with a warning such as modest overlap, clipping,
  low-confidence alignment, or suspected leakage.
- `REJECT`: malformed format, invalid timestamps, silent channel, serious clipping,
  serious overlap, excess silence, missing speaker, or another hard failure.

Only `PASS` appears in `train.jsonl`. Inspect REVIEW audio, sidecar, diarization,
transcript normalization, and QC metrics together. If it is genuinely suitable:

```powershell
python -m moshi_data_pipeline approve-review `
  --output data `
  --path "data_stereo/conversation_007.wav"
```

This records the approval separately. Rebuild later with:

```powershell
python -m moshi_data_pipeline rebuild-manifest `
  --output data `
  --include-approved-reviews
```

Manifest duration always comes from WAV sample count, paths are root-relative with
`/`, duplicates are removed, ordering is stable, and replacement is atomic.

## Egyptian Arabic behavior and limitations

Default normalization uses Unicode NFKC, removes Arabic diacritics, normalizes Alef
variants to bare Alef, and normalizes whitespace. Alef Maqsura to Ya is opt-in.
Ta Marbuta is never changed to Ha. Egyptian vocabulary is not mapped to MSA,
English technical words survive, and repeated conversational words are preserved.
The report stores original and normalized forms.

Whisper and the Arabic wav2vec2 alignment model may disagree on Egyptian spelling,
English code-switches, named entities, laughter, fillers, or dialectal phonemes.
Alignment can omit words that its character dictionary cannot represent. Diarization
labels are file-local and may swap identities between episodes. Music detection is
reported as not run because no detector with reliable behavior is bundled; music
must be reviewed manually. Energy-based leakage checks are heuristics, not proof of
speaker isolation.

## Synthetic example and tests

Generate a complete 12-second sample with alternating tones, silence, and a short
overlap:

```powershell
python -m moshi_data_pipeline.synthetic --output sample_dataset
```

Run unit and integration tests plus static checks:

```powershell
python -m pytest
python -m ruff check .
Set-Location web
pnpm test
pnpm run build
```

The Python suite covers the existing pipeline plus source-region validation,
derived overlap/silence, sample-accurate boundaries, clip feasibility, optimistic
annotation conflicts, durable job recovery, cleanup safety, source-level splitting,
unresolved-overlap muting, stereo routing, and the Moshi export contract. Frontend
tests and the TypeScript production build do not download ML models.

## Source layout

```text
moshi_data_pipeline/
├── audio/          # FFmpeg, SoundFile, masks, metrics, validation
├── transcription/  # WhisperX transcription/alignment and normalization
├── speakers/       # diarization, assignment, overlap, separator interface
├── segmentation/   # turn-window creation and rejection policy
├── output/         # official JSON, manifest, reports
├── review/         # loopback FastAPI server and offline browser UI
├── studio/         # v2 catalog, API, durable worker, planning, export, React build
├── schemas/        # JSON Schema documents
├── cache.py
├── cli.py
├── config.py
├── models.py
├── pipeline.py
└── synthetic.py
```

The editable React/TypeScript source lives in `web/`; its production build is
packaged under `moshi_data_pipeline/studio/static/` and served by FastAPI.
