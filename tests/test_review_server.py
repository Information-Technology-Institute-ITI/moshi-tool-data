from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.review.server import _item_payload, _save_review, create_app


def test_review_corrections_persist_empty_hallucination_removal(tmp_path) -> None:
    root = tmp_path / "data"
    reports = root / "reports"
    working = root / "working" / "sample"
    reports.mkdir(parents=True)
    working.mkdir(parents=True)
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    raw = working / "raw_transcript.json"
    atomic_write_json(
        raw,
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 10.0,
                    "text": "repeated text",
                    "quality_flags": ["repeated_ngram"],
                }
            ]
        },
    )
    atomic_write_json(
        reports / "sample_qc.json",
        {
            "source": str(source),
            "assistant_speaker": "A",
            "clips": [
                {
                    "clip_id": "conversation_001",
                    "path": "data_stereo/conversation_001.wav",
                    "status": "REVIEW",
                    "reasons": ["suspect_transcript_segments_present"],
                    "metrics": {},
                }
            ],
        },
    )
    atomic_write_json(
        reports / "sample_transcript.json",
        {"raw_transcript_path": str(raw)},
    )
    atomic_write_json(
        reports / "sample_diarization.json",
        {
            "exclusive_segments": [
                {"start": 0.0, "end": 10.0, "speaker": "A"},
            ],
            "overlap_intervals": [],
        },
    )
    atomic_write_json(
        working / "clip_plans.json",
        [{"clip_id": "conversation_001", "start": 0.0, "end": 10.0}],
    )
    item = _item_payload(root, "conversation_001")
    assert item["segments"][0]["quality_flags"] == ["repeated_ngram"]
    _save_review(
        root,
        "conversation_001",
        {
            "decision": "needs_work",
            "auditioned": False,
            "segments": [{"start": 0.0, "end": 10.0, "text": ""}],
            "speaker_overrides": [],
        },
    )
    correction = (
        root / "reports" / "review_corrections" / "sample.json"
    ).read_text(encoding="utf-8")
    assert '"text": ""' in correction
    app = create_app(root)
    assert any(route.path == "/api/items" for route in app.routes)
