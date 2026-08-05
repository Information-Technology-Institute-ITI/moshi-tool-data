from moshi_data_pipeline.model_revisions import resolve_model_revision, snapshot_for_revision


def test_explicit_sha_is_already_immutable() -> None:
    revision = "a" * 40
    assert resolve_model_revision("owner/model", revision, allow_network=False) == revision


def test_local_model_path_is_reported_as_local(tmp_path) -> None:
    path = tmp_path / "model"
    path.mkdir()
    snapshot, revision = snapshot_for_revision(str(path), None)
    assert snapshot == path.resolve()
    assert revision == "local"
