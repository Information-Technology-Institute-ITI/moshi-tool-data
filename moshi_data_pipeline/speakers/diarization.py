from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from moshi_data_pipeline.audio.io import read_audio
from moshi_data_pipeline.config import DiarizationConfig, TranscriptionConfig
from moshi_data_pipeline.exceptions import DependencyError, ModelStageError
from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.speakers.overlap import overlap_intervals
from moshi_data_pipeline.transcription.whisperx_backend import release_model, resolve_device

LOGGER = logging.getLogger(__name__)


def _annotation_segments(annotation: Any) -> list[SpeakerSegment]:
    values = [
        SpeakerSegment(float(turn.start), float(turn.end), str(speaker))
        for turn, _, speaker in annotation.itertracks(yield_label=True)
        if float(turn.end) > float(turn.start)
    ]
    return sorted(values, key=lambda item: (item.start, item.end, item.speaker))


def _merge_adjacent(segments: list[SpeakerSegment], maximum_gap: float = 1e-3) -> list[SpeakerSegment]:
    merged: list[SpeakerSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].speaker == segment.speaker
            and segment.start <= merged[-1].end + maximum_gap
        ):
            previous = merged[-1]
            merged[-1] = SpeakerSegment(previous.start, max(previous.end, segment.end), segment.speaker)
        else:
            merged.append(segment)
    return merged


def smooth_exclusive_segments(
    segments: list[SpeakerSegment], short_turn_seconds: float
) -> tuple[list[SpeakerSegment], list[SpeakerSegment]]:
    values = list(segments)
    ambiguous: list[SpeakerSegment] = []
    for index in range(1, len(values) - 1):
        current = values[index]
        if current.end - current.start >= short_turn_seconds:
            continue
        previous = values[index - 1]
        following = values[index + 1]
        if previous.speaker == following.speaker and previous.speaker != current.speaker:
            values[index] = SpeakerSegment(current.start, current.end, previous.speaker)
        else:
            ambiguous.append(current)
    return _merge_adjacent(values), ambiguous


class WhisperXDiarizer:
    def diarize(
        self,
        audio_path: Path,
        config: DiarizationConfig,
        transcription_config: TranscriptionConfig,
    ) -> tuple[list[SpeakerSegment], dict[str, Any]]:
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise ModelStageError(
                "HF_TOKEN is not configured. Accept the pyannote model conditions and set "
                "HF_TOKEN in the environment; secrets are never read from CLI arguments."
            )
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DependencyError(
                "WhisperX/pyannote diarization dependencies are missing; install the ML extra"
            ) from exc
        device = resolve_device(transcription_config.device)
        pipeline = None
        try:
            LOGGER.info(
                "Loading diarization model=%s device=%s token=<configured>",
                config.model,
                device,
            )
            pipeline = Pipeline.from_pretrained(config.model, token=token).to(
                torch.device(device)
            )
            audio, sample_rate = read_audio(audio_path)
            waveform = torch.from_numpy(audio.T)
            arguments: dict[str, int] = {}
            if config.min_speakers == config.max_speakers:
                arguments["num_speakers"] = config.min_speakers
            else:
                arguments["min_speakers"] = config.min_speakers
                arguments["max_speakers"] = config.max_speakers
            output = pipeline(
                {"waveform": waveform, "sample_rate": sample_rate},
                **arguments,
            )
            overlap_segments = _annotation_segments(output.speaker_diarization)
            exclusive_annotation = getattr(
                output, "exclusive_speaker_diarization", output.speaker_diarization
            )
            exclusive_segments, ambiguous_short_turns = smooth_exclusive_segments(
                _annotation_segments(exclusive_annotation), config.short_turn_seconds
            )
            speakers = sorted({segment.speaker for segment in exclusive_segments})
            overlaps = overlap_intervals(overlap_segments)
            report = {
                "model": config.model,
                "segments": [segment.to_dict() for segment in overlap_segments],
                "exclusive_segments": [
                    segment.to_dict() for segment in exclusive_segments
                ],
                "speakers": speakers,
                "speaker_count": len(speakers),
                "more_than_two_speakers": len(speakers) > 2,
                "short_turns": [
                    segment.to_dict()
                    for segment in exclusive_segments
                    if segment.end - segment.start < config.short_turn_seconds
                ],
                "ambiguous_short_turns": [
                    segment.to_dict() for segment in ambiguous_short_turns
                ],
                "overlap_intervals": [{"start": start, "end": end} for start, end in overlaps],
                "word_assignment_timeline": "exclusive_speaker_diarization",
                "note": (
                    "Diarization labels who spoke when. It does not isolate voices, and "
                    "mixed overlapping audio cannot be separated by these labels."
                ),
            }
            return exclusive_segments, report
        except Exception as exc:
            safe_message = str(exc).replace(token, "<redacted>")
            raise ModelStageError(f"Speaker diarization failed: {safe_message}") from exc
        finally:
            del pipeline
            release_model()
