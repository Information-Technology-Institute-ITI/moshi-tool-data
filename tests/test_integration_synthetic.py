import json

import soundfile as sf

from moshi_data_pipeline.synthetic import generate_synthetic_dataset


def test_synthetic_dataset_end_to_end(tmp_path) -> None:
    root = generate_synthetic_dataset(tmp_path / "sample")
    audio, sample_rate = sf.read(root / "data_stereo" / "conversation_001.wav")
    assert sample_rate == 24_000
    assert audio.ndim == 2 and audio.shape[1] == 2
    assert len(audio[:, 0]) == len(audio[:, 1])
    assert not audio[: round(0.4 * sample_rate)].any()
    payload = json.loads(
        (root / "data_stereo" / "conversation_001.json").read_text(encoding="utf-8")
    )
    assert payload["alignments"][0] == ["ازيك", [0.7, 1.2], "SPEAKER_MAIN"]
    manifest = (root / "train.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(manifest) == 1
    record = json.loads(manifest[0])
    assert record["path"] == "data_stereo/conversation_001.wav"
    assert record["duration"] == len(audio) / sample_rate
