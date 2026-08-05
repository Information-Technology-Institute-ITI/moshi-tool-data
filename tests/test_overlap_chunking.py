import numpy as np

from moshi_data_pipeline.config import SeparationConfig
from moshi_data_pipeline.speakers.separation import SpeechBrainOverlapSeparator


def test_long_overlap_uses_crossfaded_chunks_without_gaps() -> None:
    sample_rate = 100
    time = np.arange(3200) / sample_rate
    mixture = (0.15 * np.sin(2 * np.pi * 3 * time)).astype(np.float32)
    separator = object.__new__(SpeechBrainOverlapSeparator)
    separator.config = SeparationConfig(
        max_window_seconds=5.0,
        context_seconds=1.0,
        chunk_crossfade_seconds=0.5,
    )
    separator.overlaps = [(1.0, 31.0)]
    separator._separate = lambda audio, _rate: [audio * 0.6, audio * 0.4]
    separator._assign_stems = lambda stems, _rate: (stems[0], stems[1], {})

    result = separator.recover_clip(mixture, sample_rate, 0.0, 32.0)

    assert result.used
    assert len(result.chunk_metrics) > 10
    assert result.mask[100:3100].all()
    assert not result.mask[:100].any()
    assert not result.mask[3100:].any()
    reconstructed = result.assistant + result.user
    assert np.max(np.abs(reconstructed[result.mask] - mixture[result.mask])) < 1e-6
    assert max(
        abs(float(reconstructed[index] - reconstructed[index - 1]))
        for index in range(101, 3100)
    ) < 0.04
