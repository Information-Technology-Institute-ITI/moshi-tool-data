from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moshi_data_pipeline.cache import atomic_write_json, input_fingerprint, load_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.output.manifest import approve_review_path, rebuild_manifest
from moshi_data_pipeline.pipeline import ProcessOptions, process_file


def _qc_sources(dataset_root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    values: dict[str, tuple[str, dict[str, Any]]] = {}
    for report_path in sorted((dataset_root / "reports").glob("*_qc.json")):
        key = report_path.stem.removesuffix("_qc")
        report = load_json(report_path)
        for clip in report.get("clips", []):
            clip_id = str(clip.get("clip_id", ""))
            if clip_id:
                values[clip_id] = (key, report)
    return values


def _correction_path(dataset_root: Path, key: str) -> Path:
    return dataset_root / "reports" / "review_corrections" / f"{key}.json"


def _speaker_for_interval(
    start: float, end: float, segments: list[dict[str, Any]]
) -> str | None:
    overlaps: dict[str, float] = {}
    for segment in segments:
        overlap = max(
            0.0,
            min(end, float(segment["end"])) - max(start, float(segment["start"])),
        )
        if overlap:
            speaker = str(segment["speaker"])
            overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap
    return max(overlaps.items(), key=lambda item: (item[1], item[0]))[0] if overlaps else None


def _item_payload(dataset_root: Path, clip_id: str) -> dict[str, Any]:
    sources = _qc_sources(dataset_root)
    if clip_id not in sources:
        raise KeyError(clip_id)
    key, qc_report = sources[clip_id]
    clip = next(value for value in qc_report["clips"] if value["clip_id"] == clip_id)
    transcript_report = load_json(dataset_root / "reports" / f"{key}_transcript.json")
    raw = load_json(Path(transcript_report["raw_transcript_path"]))
    diarization = load_json(dataset_root / "reports" / f"{key}_diarization.json")
    exclusive = diarization.get("exclusive_segments", diarization.get("segments", []))
    correction_file = _correction_path(dataset_root, key)
    correction = load_json(correction_file) if correction_file.exists() else {}
    plan = next(
        value
        for value in load_json(dataset_root / "working" / key / "clip_plans.json")
        if value["clip_id"] == clip_id
    )
    corrected_by_bounds = {
        (round(float(value["start"]), 3), round(float(value["end"]), 3)): value
        for value in correction.get("segments", [])
    }
    speaker_overrides = list(correction.get("speaker_overrides", []))
    segments = []
    for value in raw.get("segments", []):
        start = float(value["start"])
        end = float(value["end"])
        if end <= float(plan["start"]) or start >= float(plan["end"]):
            continue
        corrected = corrected_by_bounds.get((round(start, 3), round(end, 3)), {})
        midpoint = (start + end) / 2
        corrected_speaker = next(
            (
                str(override["speaker"])
                for override in reversed(speaker_overrides)
                if float(override["start"]) <= midpoint < float(override["end"])
            ),
            None,
        )
        segments.append(
            {
                "start": start,
                "end": end,
                "text": corrected.get("text", value.get("text", "")),
                "speaker": corrected_speaker
                or _speaker_for_interval(start, end, exclusive),
                "quality_flags": value.get("quality_flags", []),
            }
        )
    decision = correction.get("decisions", {}).get(str(clip["path"]), {})
    return {
        "clip_id": clip_id,
        "source_key": key,
        "source": qc_report["source"],
        "assistant_speaker": qc_report["assistant_speaker"],
        "status": clip["status"],
        "reasons": clip.get("reasons", []),
        "metrics": clip.get("metrics", {}),
        "clip_start": float(plan["start"]),
        "clip_end": float(plan["end"]),
        "audio_url": f"/audio/{clip_id}",
        "segments": segments,
        "speakers": sorted({value["speaker"] for value in exclusive}),
        "timeline": [
            value
            for value in exclusive
            if float(value["end"]) > float(plan["start"])
            and float(value["start"]) < float(plan["end"])
        ],
        "overlap_intervals": [
            value
            for value in diarization.get("overlap_intervals", [])
            if float(value["end"]) > float(plan["start"])
            and float(value["start"]) < float(plan["end"])
        ],
        "decision": decision,
    }


def _save_review(
    dataset_root: Path,
    clip_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    item = _item_payload(dataset_root, clip_id)
    decision = str(payload.get("decision", "needs_work"))
    if decision not in {"approve", "reject", "needs_work"}:
        raise ValueError("decision must be approve, reject, or needs_work")
    if decision == "approve" and not payload.get("auditioned"):
        raise ValueError("Approval requires confirming that the clip was auditioned")
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("segments must be a list")
    cleaned_segments = []
    for index, value in enumerate(segments):
        try:
            start = float(value["start"])
            end = float(value["end"])
            text = str(value["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid transcript segment {index}") from exc
        if start < 0 or end <= start:
            raise ValueError(f"Invalid transcript segment {index}")
        cleaned_segments.append({"start": start, "end": end, "text": text})
    overrides = payload.get("speaker_overrides", [])
    if not isinstance(overrides, list):
        raise ValueError("speaker_overrides must be a list")
    correction_file = _correction_path(dataset_root, item["source_key"])
    current = load_json(correction_file) if correction_file.exists() else {}
    transcript_report = load_json(
        dataset_root / "reports" / f"{item['source_key']}_transcript.json"
    )
    raw = load_json(Path(transcript_report["raw_transcript_path"]))
    segment_updates = {
        (round(value["start"], 3), round(value["end"], 3)): value
        for value in cleaned_segments
    }
    existing_segments = {
        (round(float(value["start"]), 3), round(float(value["end"]), 3)): value
        for value in current.get("segments", [])
    }
    full_segments = []
    for value in raw.get("segments", []):
        bounds = (
            round(float(value["start"]), 3),
            round(float(value["end"]), 3),
        )
        selected = segment_updates.get(bounds, existing_segments.get(bounds, value))
        text = str(selected.get("text", "")).strip()
        full_segments.append(
            {
                "start": float(value["start"]),
                "end": float(value["end"]),
                "text": text,
            }
        )
    existing_overrides = {
        (round(float(value["start"]), 3), round(float(value["end"]), 3)): value
        for value in current.get("speaker_overrides", [])
    }
    for value in overrides:
        existing_overrides[
            (round(float(value["start"]), 3), round(float(value["end"]), 3))
        ] = value
    path = f"data_stereo/{clip_id}.wav"
    current.update(
        {
            "version": 1,
            "source": item["source"],
            "source_fingerprint": input_fingerprint(Path(item["source"])),
            "assistant_speaker": item["assistant_speaker"],
            "segments": full_segments,
            "speaker_overrides": list(existing_overrides.values()),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    current.setdefault("decisions", {})[path] = {
        "decision": decision,
        "auditioned": bool(payload.get("auditioned")),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_json(correction_file, current)
    approvals_path = dataset_root / "reports" / "manual_approvals.json"
    if decision == "approve":
        approve_review_path(dataset_root, path)
    elif approvals_path.exists():
        approvals = load_json(approvals_path)
        approved = set(approvals.get("approved_paths", []))
        approved.discard(path)
        atomic_write_json(approvals_path, {"approved_paths": sorted(approved)})
    records = rebuild_manifest(dataset_root, include_approved_reviews=True)
    return {
        "saved": True,
        "correction_path": str(correction_file),
        "manifest_records": len(records),
    }


def create_app(dataset_root: Path):
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import FileResponse
    except ImportError as exc:
        raise RuntimeError(
            'The review server requires: pip install -e ".[review]"'
        ) from exc

    dataset_root = dataset_root.resolve()
    static_root = Path(__file__).with_name("static")
    app = FastAPI(title="Moshi Dataset Review", docs_url=None, redoc_url=None)
    jobs: dict[str, dict[str, Any]] = {}
    source_jobs: dict[str, str] = {}

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/api/items")
    def list_items():
        items = []
        for clip_id in _qc_sources(dataset_root):
            item = _item_payload(dataset_root, clip_id)
            items.append(
                {
                    key: item[key]
                    for key in ("clip_id", "source_key", "status", "reasons", "decision")
                }
            )
        return {"items": items}

    @app.get("/api/items/{clip_id}")
    def get_item(clip_id: str):
        try:
            return _item_payload(dataset_root, clip_id)
        except (KeyError, OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/audio/{clip_id}")
    def audio(clip_id: str):
        path = dataset_root / "data_stereo" / f"{clip_id}.wav"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Audio not found")
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/items/{clip_id}/decision")
    def save_item(clip_id: str, payload: dict[str, Any] = Body(...)):
        try:
            return _save_review(dataset_root, clip_id, payload)
        except (KeyError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def rebuild_source(key: str, job_id: str) -> None:
        try:
            sources = _qc_sources(dataset_root)
            _, report = next(value for value in sources.values() if value[0] == key)
            snapshot = dataset_root / "working" / key / "config.snapshot.json"
            config = PipelineConfig.model_validate(load_json(snapshot))
            correction = _correction_path(dataset_root, key)
            summary = process_file(
                Path(report["source"]),
                dataset_root,
                config,
                ProcessOptions(
                    assistant_speaker=str(report["assistant_speaker"]),
                    interactive_speaker=False,
                    resume=True,
                    force_stage="align",
                    manual_transcript=correction,
                    separate_overlap=config.separation.enabled,
                ),
            )
            jobs[job_id] = {"status": "complete", "result": summary.to_dict()}
        except Exception as exc:
            jobs[job_id] = {"status": "failed", "error": str(exc)}
        finally:
            source_jobs.pop(key, None)

    @app.post("/api/sources/{key}/apply")
    def apply_source(key: str):
        correction = _correction_path(dataset_root, key)
        if not correction.exists():
            raise HTTPException(status_code=400, detail="Save corrections first")
        existing = source_jobs.get(key)
        if existing:
            return {"job_id": existing, **jobs[existing]}
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"status": "running"}
        source_jobs[key] = job_id
        threading.Thread(
            target=rebuild_source,
            args=(key, job_id),
            daemon=True,
        ).start()
        return {"job_id": job_id, "status": "running"}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Unknown job")
        return jobs[job_id]

    return app


def serve_review(
    dataset_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    allow_remote: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        raise ValueError("Non-loopback review hosting requires --allow-remote")
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            'The review server requires: pip install -e ".[review]"'
        ) from exc
    uvicorn.run(create_app(dataset_root), host=host, port=port, log_level="info")
