from pathlib import Path

import pytest

from src.application.quality import paths
from src.application.quality.service import OMQualityService
from src.application.runtime_paths import runtime_root_record_path, runtime_root_scope
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository


@pytest.mark.parametrize("selection", ["repo", "record", "env", "scope"])
def test_quality_paths_follow_canonical_runtime_selection(tmp_path, monkeypatch, selection):
    repo, recorded, env, scoped = (tmp_path / name for name in ("repo", "recorded", "env", "scoped"))
    monkeypatch.setattr(paths, "repo_base", lambda: repo)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    monkeypatch.setenv("OM_ENV_FILE", str(empty_env))
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    if selection != "repo":
        recorded.mkdir()
        (recorded / "config.yaml").write_text("accounts: {}\n")
        record = runtime_root_record_path()
        record.parent.mkdir(parents=True)
        record.write_text(str(recorded) + "\n")
        record.chmod(0o600)
    if selection in {"env", "scope"}:
        monkeypatch.setenv("OM_RUNTIME_ROOT", str(env))
    selected = {"repo": repo, "record": recorded, "env": env, "scope": scoped}[selection]

    with runtime_root_scope(scoped if selection == "scope" else None):
        folder = selected / "output_shared/state/quality"
        assert paths.default_quality_artifact_path() == folder / "status.v1.json"
        assert paths.default_quality_control_path() == folder / "control_state.v1.json"
        assert paths.default_quality_integrity_artifact_path() == folder / "integrity_status.v1.json"
        assert paths.default_quality_hot_path_cutover_receipt_path() == folder / "current_hot_path_cutover.v1.json"

    if selection == "scope":
        assert paths.default_quality_artifact_path() == env / "output_shared/state/quality/status.v1.json"


@pytest.mark.parametrize("explicit", [False, True])
def test_quality_service_keeps_explicit_paths_ahead_of_scoped_defaults(tmp_path, explicit):
    custom = tmp_path / "custom"
    artifact = QualityArtifactRepository(custom / "status.json")
    control = QualityControlStateRepository(custom / "control.json")
    integrity = QualityArtifactRepository(custom / "integrity.json")
    arguments = {
        "artifact_repository": artifact,
        "control_repository": control,
        "integrity_artifact_repository": integrity,
        "ledger_probe_path": custom / "ledger.sqlite3",
        "cutover_receipt_path": custom / "cutover.json",
    } if explicit else {}
    runtime = tmp_path / "runtime"
    with runtime_root_scope(runtime):
        service = OMQualityService(**arguments)

    if explicit:
        assert service.artifact_repository is artifact
        assert service.control_repository is control
        assert service.integrity_artifact_repository is integrity
        assert service.ledger_probe_path == custom / "ledger.sqlite3"
        assert service.cutover_receipt_path == custom / "cutover.json"
    else:
        folder = runtime / "output_shared/state/quality"
        assert service.artifact_repository.path == folder / "status.v1.json"
        assert service.integrity_artifact_repository.path == folder / "integrity_status.v1.json"
        assert service.cutover_receipt_path == folder / "current_hot_path_cutover.v1.json"
        assert service.ledger_probe_path == runtime / "output_shared/state/option_positions.sqlite3"
