from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from moshi_data_pipeline.audio.channels import render_stereo
from moshi_data_pipeline.audio.io import write_pcm16
from moshi_data_pipeline.audio.validation import validate_clip, validate_qc_payload
from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.models import SpeakerSegment, Word
from moshi_data_pipeline.output.manifest import rebuild_manifest
from moshi_data_pipeline.output.moshi_json import build_moshi_payload
from moshi_data_pipeline.output.reports import rebuild_rejected_report
from moshi_data_pipeline.speakers.overlap import interval_duration, overlap_intervals


def _tone(samples: int, sample_rate: int, frequency: float, amplitude: float) -> np.ndarray:
    time = np.arange(samples, dtype=np.float64) / sample_rate
    return (amplitude * np.sin(2 * math.pi * frequency * time)).astype(np.float32)


def generate_synthetic_dataset(root: Path) -> Path:
    root = root.resolve()
    data_stereo = root / "data_stereo"
    reports = root / "reports"
    working = root / "working" / "synthetic"
    for directory in (data_stereo, reports, working):
        directory.mkdir(parents=True, exist_ok=True)
    config = PipelineConfig()
    sample_rate = config.audio.sample_rate
    duration = 12.0
    samples = round(duration * sample_rate)
    mono = np.zeros(samples, dtype=np.float32)
    segments = [
        SpeakerSegment(0.50, 2.50, "SPEAKER_00"),
        SpeakerSegment(3.00, 4.50, "SPEAKER_01"),
        SpeakerSegment(5.00, 7.00, "SPEAKER_00"),
        SpeakerSegment(7.50, 9.10, "SPEAKER_01"),
        SpeakerSegment(9.00, 10.00, "SPEAKER_00"),
        SpeakerSegment(10.50, 11.50, "SPEAKER_01"),
    ]
    assistant_tone = _tone(samples, sample_rate, 330.0, 0.22)
    user_tone = _tone(samples, sample_rate, 550.0, 0.18)
    for segment in segments:
        first = round(segment.start * sample_rate)
        last = round(segment.end * sample_rate)
        mono[first:last] += (
            assistant_tone[first:last] if segment.speaker == "SPEAKER_00" else user_tone[first:last]
        )
    mono = np.clip(mono, -0.9, 0.9)
    source_wav = working / "source_mono.wav"
    write_pcm16(source_wav, mono[:, None], sample_rate)
    rendered = render_stereo(
        mono,
        sample_rate,
        0.0,
        duration,
        segments,
        "SPEAKER_00",
        config.audio.fade_ms,
    )
    wav_path = data_stereo / "conversation_001.wav"
    json_path = data_stereo / "conversation_001.json"
    write_pcm16(wav_path, rendered.stereo, sample_rate)
    words = [
        Word("إزّيك", 0.70, 1.20, "SPEAKER_00", 0.96, "إزّيك"),
        Word("عامل", 1.30, 1.75, "SPEAKER_00", 0.94, "عامل"),
        Word("إيه", 1.85, 2.25, "SPEAKER_00", 0.93, "إيه"),
        Word("AI", 5.20, 5.55, "SPEAKER_00", 0.98, "AI"),
        Word("يعني", 9.30, 9.75, "SPEAKER_00", 0.91, "يعني"),
        Word("okay", 3.20, 3.60, "SPEAKER_01", 0.97, "okay"),
    ]
    payload, transcript = build_moshi_payload(
        words,
        "SPEAKER_00",
        0.0,
        duration,
        config.normalization,
    )
    atomic_write_json(json_path, payload)
    overlaps = overlap_intervals(segments)
    overlap_ratio = interval_duration(overlaps) / duration
    qc = validate_clip(
        wav_path,
        json_path,
        config,
        assistant_mask=rendered.assistant_mask,
        user_mask=rendered.user_mask,
        overlap_ratio=overlap_ratio,
    )
    qc.clip_id = "conversation_001"
    qc.path = "data_stereo/conversation_001.wav"
    atomic_write_json(
        reports / "synthetic_diarization.json",
        {
            "speakers": ["SPEAKER_00", "SPEAKER_01"],
            "segments": [segment.to_dict() for segment in segments],
            "overlap_intervals": [{"start": start, "end": end} for start, end in overlaps],
            "note": "Synthetic diarization labels; labels are not source separation.",
        },
    )
    atomic_write_json(
        reports / "synthetic_transcript.json",
        {
            "language": "ar",
            "clips": [{"clip_id": "conversation_001", **transcript}],
            "unaligned_words": [],
        },
    )
    qc_report = {
        "source": "synthetic://alternating-tones",
        "assistant_speaker": "SPEAKER_00",
        "status_counts": {
            "PASS": int(qc.status.value == "PASS"),
            "REVIEW": int(qc.status.value == "REVIEW"),
            "REJECT": int(qc.status.value == "REJECT"),
        },
        "global_unaligned_words": 0,
        "uncertain_word_assignments": 0,
        "clips": [qc.to_dict()],
    }
    validate_qc_payload(qc_report)
    atomic_write_json(reports / "synthetic_qc.json", qc_report)
    rebuild_rejected_report(root)
    rebuild_manifest(root)
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic sample Moshi dataset")
    parser.add_argument("--output", type=Path, default=Path("sample_dataset"))
    args = parser.parse_args()
    print(generate_synthetic_dataset(args.output))


if __name__ == "__main__":
    main()
