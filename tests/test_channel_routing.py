import numpy as np
import pytest

from moshi_data_pipeline.audio.channels import render_independent_stereo
from moshi_data_pipeline.audio.routing import (
    analyze_channel_audio,
    infer_speaker_channel_mapping,
)
from moshi_data_pipeline.config import ChannelRoutingConfig
from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.studio.domain import AnnotationDocument


def _isolated_stereo(sample_rate: int = 1000) -> np.ndarray:
    time = np.arange(4000) / sample_rate
    audio = np.zeros((4000, 2), dtype=np.float32)
    audio[:2000, 0] = 0.25 * np.sin(2 * np.pi * 61 * time[:2000])
    audio[:2000, 1] = 0.001 * np.sin(2 * np.pi * 97 * time[:2000])
    audio[2000:, 0] = 0.001 * np.sin(2 * np.pi * 61 * time[2000:])
    audio[2000:, 1] = 0.25 * np.sin(2 * np.pi * 97 * time[2000:])
    return audio


def test_channel_analysis_recommends_independent_stereo() -> None:
    report = analyze_channel_audio(
        _isolated_stereo(),
        1000,
        ChannelRoutingConfig(),
    )
    assert report["routing_candidate"] is True
    assert report["recommended_mode"] == "independent_stereo"
    assert report["requires_human_confirmation"] is True


def test_channel_analysis_rejects_dual_mono() -> None:
    signal = np.linspace(-0.2, 0.2, 4000, dtype=np.float32)
    report = analyze_channel_audio(
        np.column_stack((signal, signal)),
        1000,
        ChannelRoutingConfig(),
    )
    assert report["dual_mono"] is True
    assert report["recommended_mode"] == "mono"


def test_channel_analysis_can_be_disabled_by_policy() -> None:
    report = analyze_channel_audio(
        _isolated_stereo(),
        1000,
        ChannelRoutingConfig(mode="mono"),
    )
    assert report["routing_candidate"] is False
    assert report["reason"] == "channel_first_routing_disabled_by_configuration"


def test_verified_independent_routing_requires_complete_mapping() -> None:
    with pytest.raises(ValueError, match="complete A/B channel map"):
        AnnotationDocument(
            source_id="source",
            channel_routing_mode="independent_stereo",
            channel_routing_verified=True,
        )


def test_speaker_channel_mapping_and_rendering() -> None:
    audio = _isolated_stereo()
    segments = [
        SpeakerSegment(0.0, 2.0, "A"),
        SpeakerSegment(2.0, 4.0, "B"),
    ]
    mapping, evidence = infer_speaker_channel_mapping(
        audio,
        1000,
        segments,
        ChannelRoutingConfig(),
    )
    assert mapping == {"A": 0, "B": 1}
    assert min(evidence["speaker_channel_margin_db"].values()) > 6.0

    rendered = render_independent_stereo(
        audio,
        1000,
        0.0,
        4.0,
        segments,
        "A",
        mapping,
        fade_ms=0,
    )
    assert rendered.routing_method == "verified_independent_stereo"
    assert np.allclose(rendered.stereo[:2000, 0], audio[:2000, 0])
    assert np.allclose(rendered.stereo[:2000, 1], 0.0)
    assert np.allclose(rendered.stereo[2000:, 0], 0.0)
    assert np.allclose(rendered.stereo[2000:, 1], audio[2000:, 1])
