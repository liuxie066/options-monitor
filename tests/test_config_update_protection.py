from __future__ import annotations

import hashlib
from pathlib import Path

from scripts import guardrails_check


def test_guardrails_classifies_only_root_runtime_configs() -> None:
    assert guardrails_check.is_root_runtime_config_path(Path("config.us.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.hk.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.assistant.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.market_us.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.local.prod.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.us.json.bak.20260507-100000"))

    assert not guardrails_check.is_root_runtime_config_path(Path("configs/examples/user.example.us.json"))
    assert not guardrails_check.is_root_runtime_config_path(Path("src/application/config_loader.py"))


def test_guardrails_rejects_tracked_root_runtime_configs() -> None:
    issues = guardrails_check.check_runtime_config_tracking(
        [
            Path("config.us.json"),
            Path("config.hk.json"),
            Path("config.assistant.json"),
            Path("configs/examples/user.example.us.json"),
        ]
    )

    assert [issue.path.as_posix() for issue in issues] == [
        "config.us.json",
        "config.hk.json",
        "config.assistant.json",
    ]
    assert all("root runtime config must stay untracked" in issue.reason for issue in issues)


def test_sensitive_artifact_guard_uses_fingerprints_without_echoing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_number = "987654321012345678"
    tracked = tmp_path / "artifact.md"
    tracked.write_text(
        f"account={private_number}\npath=/Users/"
        "private-person/work/repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    monkeypatch.setattr(
        guardrails_check,
        "KNOWN_PRIVATE_TOKEN_SHA256",
        {hashlib.sha256(private_number.encode("utf-8")).hexdigest()},
    )

    issues = guardrails_check.check_sensitive_repository_artifacts(
        [Path("artifact.md")]
    )

    assert len(issues) == 2
    rendered = "\n".join(issue.render() for issue in issues)
    assert private_number not in rendered
    assert "private-person" not in rendered
    assert "<redacted" in rendered


def test_sensitive_artifact_guard_allows_generic_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tracked = tmp_path / "artifact.md"
    tracked.write_text(
        "/Users/om/work/repo /home/user/apps/repo /Volumes/Workspace/repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)

    assert guardrails_check.check_sensitive_repository_artifacts(
        [Path("artifact.md")]
    ) == []
