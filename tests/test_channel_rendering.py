import numpy as np

from moshi_data_pipeline.audio.channels import render_stereo
from moshi_data_pipeline.models import SpeakerSegment


def test_channels_preserve_timeline_and_omit_overlap() -> None:
    sample_rate = 1000
    mono = np.ones(5000, dtype=np.float32) * 0.2
    segments = [
        SpeakerSegment(0.5, 1.5, "A"),
        SpeakerSegment(2.0, 3.0, "B"),
        SpeakerSegment(2.8, 3.2, "A"),
    ]
    result = render_stereo(mono, sample_rate, 0.0, 5.0, segments, "A", fade_ms=0)
    assert result.stereo.shape == (5000, 2)
    assert np.allclose(result.stereo[600:1400, 0], 0.2)
    assert np.allclose(result.stereo[600:1400, 1], 0.0)
    assert np.allclose(result.stereo[2100:2700, 1], 0.2)
    assert np.allclose(result.stereo[2100:2700, 0], 0.0)
    assert np.allclose(result.stereo[2800:3000], 0.0)
    assert np.allclose(result.stereo[1600:1900], 0.0)
