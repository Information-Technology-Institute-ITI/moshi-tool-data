import sys
from types import SimpleNamespace

from moshi_data_pipeline.benchmark import benchmark_dataset, benchmark_profiles
from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.pipeline import media_key


def test_benchmark_reports_text_and_speaker_accuracy(tmp_path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"sample")
    root = tmp_path / "data"
    key = media_key(input_path)
    working = root / "working" / key
    reports = root / "reports"
    working.mkdir(parents=True)
    reports.mkdir(parents=True)
    atomic_write_json(
        working / "assigned_words.json",
        [
            {"word": "اهلا", "original": "اهلا", "start": 0.0, "end": 0.5, "speaker": "A"},
            {"word": "بك", "original": "بك", "start": 0.5, "end": 1.0, "speaker": "B"},
        ],
    )
    atomic_write_json(
        reports / f"{key}_qc.json",
        {"clips": [], "transcription_quality": {"unresolved_hallucination_count": 0}},
    )
    atomic_write_json(
        reports / f"{key}_performance.json",
        {"realtime_factor": 1.0, "peak_gpu_memory_bytes": 1},
    )
    gold = tmp_path / "gold.json"
    atomic_write_json(
        gold,
        {
            "segments": [
                {"start": 0.0, "end": 0.5, "speaker": "A", "text": "اهلا"},
                {"start": 0.5, "end": 1.0, "speaker": "B", "text": "بك"},
            ]
        },
    )
    result = benchmark_dataset(input_path, gold, root)
    assert result["word_error_rate"] == 0.0
    assert result["character_error_rate"] == 0.0
    assert result["speaker_assignment_error_rate"] == 0.0


def test_profile_benchmark_promotes_better_float16_without_dataset_writes(
    tmp_path, monkeypatch
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"sample")
    gold = tmp_path / "gold.json"
    atomic_write_json(
        gold,
        {"segments": [{"start": 0.0, "end": 1.0, "speaker": "A", "text": "hello world"}]},
    )

    class FakeTranscriber:
        def transcribe(self, _path, config):
            text = "hello world" if config.compute_type == "float16" else "hello word"
            return {"segments": [{"text": text}], "quality": {"suspect_segment_count": 0}}

    fake_cuda = SimpleNamespace(
        is_available=lambda: False,
        reset_peak_memory_stats=lambda: None,
        max_memory_allocated=lambda: 0,
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=fake_cuda))
    monkeypatch.setattr(
        "moshi_data_pipeline.benchmark.inspect_media",
        lambda _path: {"duration": 10.0},
    )
    monkeypatch.setattr(
        "moshi_data_pipeline.benchmark.extract_working_wav",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "moshi_data_pipeline.benchmark.WhisperXTranscriber",
        FakeTranscriber,
    )

    result = benchmark_profiles(input_path, gold, PipelineConfig())

    assert result["profiles"]["int8"]["status"] == "ok"
    assert result["profiles"]["float16"]["character_error_rate"] == 0.0
    assert result["float16_promotion_eligible"] is True
