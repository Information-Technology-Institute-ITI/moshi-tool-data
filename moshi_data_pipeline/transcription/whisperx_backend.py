from __future__ import annotations

import gc
import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from moshi_data_pipeline.config import TranscriptionConfig
from moshi_data_pipeline.exceptions import DependencyError, ModelStageError
from moshi_data_pipeline.transcription.quality import (
    analyze_segment,
    normalized_disagreement,
    quality_rank,
    quality_summary,
)

LOGGER = logging.getLogger(__name__)


def _aggregate_average_log_probability(
    segments: list[dict[str, Any]],
) -> float | None:
    weighted_total = 0.0
    total_duration = 0.0
    for segment in segments:
        value = segment.get("avg_logprob")
        if value is None:
            continue
        try:
            probability = float(value)
            duration = max(
                0.001,
                float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)),
            )
        except (TypeError, ValueError):
            continue
        weighted_total += probability * duration
        total_duration += duration
    return weighted_total / total_duration if total_duration else None


def _remove_speechbrain_deprecated_aliases() -> None:
    """Avoid a SpeechBrain 1.1 lazy-import bug triggered by inspect on Windows."""
    aliases = (
        "speechbrain.pretrained",
        "speechbrain.k2_integration",
        "speechbrain.wordemb",
        "speechbrain.lobes.models.huggingface_transformers",
        "speechbrain.lobes.models.spacy",
        "speechbrain.lobes.models.flair",
        "speechbrain.nnet.loss.transducer_loss",
    )
    for name in aliases:
        module = sys.modules.get(name)
        if (
            module is not None
            and type(module).__module__ == "speechbrain.utils.importutils"
            and type(module).__name__ in {"LazyModule", "DeprecatedModuleRedirect"}
        ):
            sys.modules.pop(name, None)


def _import_runtime():
    try:
        import torch
        import whisperx
    except ImportError as exc:
        raise DependencyError(
            "WhisperX stage dependencies are not installed. "
            'Install the ML extra with: pip install -e ".[ml]"'
        ) from exc
    with suppress(ImportError):
        # Preload the optional package so its aliases are created and can be
        # removed before pyannote/torch inspect the process module table.
        import speechbrain  # noqa: F401
    _remove_speechbrain_deprecated_aliases()
    return whisperx, torch


def resolve_device(requested: str) -> str:
    _, torch = _import_runtime()
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        LOGGER.warning("CUDA was requested but is unavailable; using CPU fallback")
        return "cpu"
    if requested not in {"cuda", "cpu"}:
        raise ModelStageError(f"Unsupported device {requested!r}; choose auto, cuda, or cpu")
    return requested


def release_model() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except (ImportError, RuntimeError):
        pass


class WhisperXTranscriber:
    def transcribe(self, audio_path: Path, config: TranscriptionConfig) -> dict[str, Any]:
        whisperx, _ = _import_runtime()
        device = resolve_device(config.device)
        model = None
        try:
            LOGGER.info(
                "Loading WhisperX model=%s language=%s device=%s compute_type=%s batch_size=%d",
                config.model,
                config.language,
                device,
                config.compute_type,
                config.batch_size,
            )
            model = whisperx.load_model(
                config.model,
                device,
                compute_type=config.compute_type,
                language=config.language,
                vad_method="pyannote",
                asr_options={
                    "beam_size": config.beam_size,
                    "repetition_penalty": config.repetition_penalty,
                    "no_repeat_ngram_size": config.no_repeat_ngram_size,
                    "hallucination_silence_threshold": config.hallucination_silence_threshold,
                    "condition_on_previous_text": False,
                },
            )
            # Pyannote imports SpeechBrain while constructing its VAD model, so
            # the deprecated aliases do not exist until after load_model.
            _remove_speechbrain_deprecated_aliases()
            result = model.transcribe(
                str(audio_path),
                batch_size=config.batch_size,
                language=config.language,
                chunk_size=config.chunk_size,
                print_progress=True,
            )
            audio = None
            for segment in result.get("segments", []):
                flags = analyze_segment(segment, config)
                if not flags:
                    segment["quality_flags"] = []
                    continue
                if audio is None:
                    audio = whisperx.load_audio(str(audio_path))
                sample_rate = 16_000
                start = max(0, round(float(segment["start"]) * sample_rate))
                end = min(len(audio), round(float(segment["end"]) * sample_rate))
                retry_text = ""
                retry_segments: list[dict[str, Any]] = []
                if end > start:
                    retry = model.transcribe(
                        audio[start:end],
                        batch_size=1,
                        language=config.language,
                        chunk_size=config.retry_chunk_size,
                        print_progress=False,
                    )
                    retry_segments = list(retry.get("segments", []))
                    retry_text = " ".join(
                        str(value.get("text", "")).strip()
                        for value in retry_segments
                    ).strip()
                disagreement = normalized_disagreement(str(segment.get("text", "")), retry_text)
                retry_avg_logprob = _aggregate_average_log_probability(retry_segments)
                retry_candidate = {
                    "text": retry_text,
                    "avg_logprob": retry_avg_logprob,
                    "quality_flags": analyze_segment(
                        {
                            "text": retry_text,
                            "start": segment["start"],
                            "end": segment["end"],
                            "avg_logprob": retry_avg_logprob,
                        },
                        config,
                    ),
                }
                segment["selected_decode"] = "initial"
                if retry_text and quality_rank(
                    retry_candidate["quality_flags"], retry_avg_logprob
                ) < quality_rank(flags, segment.get("avg_logprob")):
                    segment["original_text"] = segment.get("text", "")
                    segment["text"] = retry_text
                    if retry_avg_logprob is not None:
                        segment["avg_logprob"] = retry_avg_logprob
                    flags = list(retry_candidate["quality_flags"])
                    segment["selected_decode"] = "retry"
                if disagreement > config.decode_disagreement_ratio:
                    flags.append("decode_disagreement")
                segment["decode_disagreement"] = disagreement
                segment["retry_candidate"] = retry_candidate
                segment["quality_flags"] = sorted(set(flags))
            result["requested_language"] = config.language
            result["model"] = config.model
            result["device"] = device
            result["compute_type"] = config.compute_type
            result["quality_profile"] = config.quality_profile
            result["quality"] = quality_summary(result.get("segments", []))
            return result
        except RuntimeError as exc:
            message = str(exc)
            if "out of memory" in message.lower() or "cuda" in message.lower():
                raise ModelStageError(
                    f"WhisperX transcription failed on {device}: {message}. "
                    "For a reproducible lower-memory retry, explicitly use --model medium; "
                    "the pipeline will not change models silently."
                ) from exc
            raise ModelStageError(f"WhisperX transcription failed: {message}") from exc
        except Exception as exc:
            raise ModelStageError(f"WhisperX transcription failed: {exc}") from exc
        finally:
            del model
            release_model()
