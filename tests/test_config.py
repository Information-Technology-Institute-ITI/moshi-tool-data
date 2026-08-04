from moshi_data_pipeline.config import load_config


def test_none_cli_overrides_do_not_replace_defaults() -> None:
    config = load_config(
        overrides={
            "transcription": {
                "model": None,
                "language": "ar",
                "device": None,
                "compute_type": None,
                "batch_size": None,
            },
            "diarization": {"min_speakers": None, "max_speakers": None},
        }
    )
    assert config.transcription.model == "large-v3"
    assert config.transcription.language == "ar"
    assert config.transcription.batch_size == 1
    assert config.diarization.min_speakers == 2
