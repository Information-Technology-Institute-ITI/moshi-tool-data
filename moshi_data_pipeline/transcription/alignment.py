from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from moshi_data_pipeline.config import AlignmentConfig, TranscriptionConfig
from moshi_data_pipeline.exceptions import DependencyError, ModelStageError, UnsupportedFeatureError
from moshi_data_pipeline.models import Word
from moshi_data_pipeline.transcription.quality import latin_script_word
from moshi_data_pipeline.transcription.whisperx_backend import release_model, resolve_device

LOGGER = logging.getLogger(__name__)


class AlignmentBackend(Protocol):
    def align(
        self,
        audio_path: Path,
        transcription: dict[str, Any],
        transcription_config: TranscriptionConfig,
        alignment_config: AlignmentConfig,
        manually_corrected_segments: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[Word]]:
        """Return the raw aligned result and a flat word list."""


def words_from_result(result: dict[str, Any]) -> list[Word]:
    words: list[Word] = []
    for segment in result.get("segments", []):
        for item in segment.get("words", []):
            words.append(
                Word(
                    word=str(item.get("word", "")).strip(),
                    start=float(item["start"]) if item.get("start") is not None else None,
                    end=float(item["end"]) if item.get("end") is not None else None,
                    speaker=item.get("speaker"),
                    score=float(item["score"]) if item.get("score") is not None else None,
                    original=str(item.get("word", "")).strip(),
                )
            )
    return words


class WhisperXAlignmentBackend:
    def align(
        self,
        audio_path: Path,
        transcription: dict[str, Any],
        transcription_config: TranscriptionConfig,
        alignment_config: AlignmentConfig,
        manually_corrected_segments: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[Word]]:
        corrected_segments = None
        if manually_corrected_segments is not None:
            payload = manually_corrected_segments
            if isinstance(payload, dict):
                payload = payload.get("segments")
            if not isinstance(payload, list):
                raise UnsupportedFeatureError(
                    "Corrected transcript must be a segment list or an object containing segments"
                )
            corrected_segments = []
            for index, value in enumerate(payload):
                if not isinstance(value, dict):
                    raise UnsupportedFeatureError(
                        f"Corrected transcript segment {index} must be an object"
                    )
                try:
                    start = float(value["start"])
                    end = float(value["end"])
                    text = str(value["text"]).strip()
                except (KeyError, TypeError, ValueError) as exc:
                    raise UnsupportedFeatureError(
                        f"Corrected transcript segment {index} needs start, end, and text"
                    ) from exc
                if start < 0 or end <= start:
                    raise UnsupportedFeatureError(
                        f"Corrected transcript segment {index} has invalid timestamps"
                    )
                if text:
                    corrected_segments.append({"start": start, "end": end, "text": text})
        try:
            import whisperx
        except ImportError as exc:
            raise DependencyError(
                "WhisperX alignment dependencies are missing; install the ML extra"
            ) from exc
        device = resolve_device(transcription_config.device)
        model = None
        try:
            language = str(transcription.get("language") or transcription_config.language)
            LOGGER.info("Loading WhisperX alignment model for language=%s on %s", language, device)
            model, metadata = whisperx.load_align_model(
                language_code=language,
                device=device,
                model_name=alignment_config.model,
            )
            result = whisperx.align(
                corrected_segments
                if corrected_segments is not None
                else transcription.get("segments", []),
                model,
                metadata,
                str(audio_path),
                device,
                return_char_alignments=False,
                print_progress=True,
            )
            result["language"] = language
            result["alignment_model"] = alignment_config.model or "whisperx-default"
            words = words_from_result(result)
            result["low_confidence_latin_words"] = [
                word.to_dict()
                for word in words
                if latin_script_word(word.word)
                and (word.score is None or word.score < alignment_config.low_confidence_score)
            ]
            result["manually_corrected"] = corrected_segments is not None
            return result, words
        except Exception as exc:
            raise ModelStageError(
                f"WhisperX word alignment failed: {exc}. "
                "Words without real timestamps are never fabricated."
            ) from exc
        finally:
            del model
            release_model()
