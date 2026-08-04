import json

import numpy as np

from moshi_data_pipeline.audio.io import write_pcm16
from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.output.manifest import rebuild_manifest


def test_manifest_is_stable_deduplicated_and_uses_posix_paths(tmp_path) -> None:
    data_stereo = tmp_path / "data_stereo"
    reports = tmp_path / "reports"
    data_stereo.mkdir()
    reports.mkdir()
    wav_path = data_stereo / "conversation_001.wav"
    write_pcm16(wav_path, np.ones((24_000, 2), dtype=np.float32) * 0.01, 24_000)
    atomic_write_json(
        wav_path.with_suffix(".json"),
        {"alignments": [["ازيك", [0.1, 0.5], "SPEAKER_MAIN"]]},
    )
    atomic_write_json(
        reports / "episode_qc.json",
        {
            "clips": [
                {
                    "path": "data_stereo\\conversation_001.wav",
                    "status": "PASS",
                }
            ]
        },
    )
    first = rebuild_manifest(tmp_path)
    second = rebuild_manifest(tmp_path)
    assert first == second == [{"path": "data_stereo/conversation_001.wav", "duration": 1.0}]
    lines = (tmp_path / "train.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == "data_stereo/conversation_001.wav"
