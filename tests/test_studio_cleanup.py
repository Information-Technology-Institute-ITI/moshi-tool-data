from moshi_data_pipeline.studio.cleanup import LEGACY_ARTIFACTS, remove_legacy_artifacts


def test_cleanup_removes_only_confirmed_artifacts_and_preserves_raw(tmp_path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "podcast.mp4"
    source.write_bytes(b"original")
    keep = tmp_path / "config.example.yaml"
    keep.write_text("audio: {}", encoding="utf-8")
    for name in LEGACY_ARTIFACTS:
        target = tmp_path / name
        target.mkdir()
        (target / "generated.bin").write_bytes(b"x")
    removed = remove_legacy_artifacts(tmp_path)
    assert {path.name for path in removed} == set(LEGACY_ARTIFACTS)
    assert source.read_bytes() == b"original"
    assert keep.exists()
