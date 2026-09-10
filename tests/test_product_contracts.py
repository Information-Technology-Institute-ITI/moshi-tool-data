from __future__ import annotations

import pytest
from pydantic import ValidationError

from moshi_data_pipeline.studio.product_contracts import (
    ApiErrorDetail,
    ApiErrorEnvelope,
    PlaybackRange,
    PlaybackState,
    ReviewChapter,
    ReviewChapterConfig,
    ReviewChapterSet,
    TrainingProfile,
    WaveformPeakWindow,
)


def test_playback_and_error_contracts_are_strict() -> None:
    playback = PlaybackState(
        status="playing",
        current_sample=10,
        active_range=PlaybackRange(start_sample=10, end_sample=20, behavior="once"),
    )
    assert playback.active_range is not None
    assert playback.active_range.behavior == "once"

    envelope = ApiErrorEnvelope(
        error=ApiErrorDetail(code="conflict", message="Revision changed", retryable=True)
    )
    assert envelope.contract_version == "studio.error/v1"
    with pytest.raises(ValidationError):
        PlaybackState(status="playing", current_sample=-1)


def test_review_chapter_defaults_to_thirty_minutes() -> None:
    config = ReviewChapterConfig()
    assert config.mode == "max_duration"
    assert config.max_duration_seconds == 30 * 60


def test_count_and_manual_chapter_modes_require_valid_values() -> None:
    with pytest.raises(ValidationError):
        ReviewChapterConfig(mode="count")
    with pytest.raises(ValidationError):
        ReviewChapterConfig(mode="manual", manual_boundaries_samples=[0, 10, 10])


def test_chapter_set_requires_contiguous_ranges_and_ordinals() -> None:
    config = ReviewChapterConfig()
    first = ReviewChapter(
        id="chapter_1",
        ordinal=1,
        start_sample=0,
        end_sample=100,
        boundary_reason="source_edge",
    )
    second = ReviewChapter(
        id="chapter_2",
        ordinal=2,
        start_sample=100,
        end_sample=200,
        boundary_reason="segment",
    )
    chapter_set = ReviewChapterSet(
        id="set_1",
        source_id="source_1",
        annotation_version=2,
        config=config,
        chapters=[first, second],
    )
    assert chapter_set.chapters[-1].end_sample == 200

    with pytest.raises(ValidationError):
        ReviewChapterSet(
            id="set_1",
            source_id="source_1",
            annotation_version=2,
            config=config,
            chapters=[first, second.model_copy(update={"start_sample": 101})],
        )


def test_peak_window_rejects_invalid_amplitudes_and_source_overrun() -> None:
    valid = WaveformPeakWindow(
        source_id="source_1",
        start_sample=0,
        end_sample=100,
        sample_rate=24_000,
        duration_samples=200,
        samples_per_point=10,
        points=[(-0.25, 0.5)],
    )
    assert valid.points == [(-0.25, 0.5)]
    with pytest.raises(ValidationError):
        WaveformPeakWindow(
            source_id="source_1",
            start_sample=0,
            end_sample=201,
            sample_rate=24_000,
            duration_samples=200,
            samples_per_point=10,
            points=[(-0.25, 0.5)],
        )


def test_training_profiles_are_target_specific() -> None:
    whisper = TrainingProfile(name="Whisper Arabic", target="whisper")
    assert whisper.max_duration_seconds == 30
    with pytest.raises(ValidationError):
        TrainingProfile(
            name="Invalid Moshi profile",
            target="moshi",
            manifest_format="huggingface",
        )
