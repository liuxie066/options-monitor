from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from scripts import guardrails_check


def test_sensitive_artifact_guard_redacts_new_provider_key(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    leaked = "sk-proj-" + "Ab9_" * 8
    candidate = repo / "runtime.json"
    candidate.write_text('{"api_key": "' + leaked + '"}\n', encoding="utf-8")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_sensitive_repository_artifacts([candidate])

    assert len(issues) == 1
    rendered = issues[0].render()
    assert "high-confidence credential pattern" in rendered
    assert leaked not in rendered
    assert "<redacted credential pattern>" in rendered


def test_sensitive_artifact_guard_detects_facebook_secret_context(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = repo / "settings.toml"
    field_name = "facebook_app_" + "secret"
    credential = "".join(("a1b2c3d4", "e5f60718", "293a4b5c", "6d7e8f90"))
    candidate.write_text(
        f'{field_name} = "{credential}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_sensitive_repository_artifacts([candidate])

    assert len(issues) == 1
    assert credential not in issues[0].render()


def test_sensitive_artifact_guard_allows_env_reference(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = repo / "settings.json"
    candidate.write_text('{"api_key_env": "OM_LLM_API_KEY"}\n', encoding="utf-8")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_sensitive_repository_artifacts([candidate]) == []


def test_sensitive_artifact_guard_keeps_only_exact_invalid_fixtures_allowlisted() -> None:
    repo = Path(__file__).resolve().parents[1]
    fixtures = [
        repo / "tests" / "test_research.py",
        repo / "tests" / "test_agent_plugin_smoke.py",
    ]

    assert guardrails_check.check_sensitive_repository_artifacts(fixtures) == []


def test_sensitive_artifact_guard_redacts_known_private_email(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    private_email = "private.person@example.test"
    candidate = repo / "notes.md"
    candidate.write_text(f"contact: {private_email}\n", encoding="utf-8")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)
    monkeypatch.setattr(
        guardrails_check,
        "KNOWN_PRIVATE_EMAIL_SHA256",
        {hashlib.sha256(private_email.encode("utf-8")).hexdigest()},
    )

    issues = guardrails_check.check_sensitive_repository_artifacts([candidate])

    assert len(issues) == 1
    assert "known personal email fingerprint" in issues[0].render()
    assert private_email not in issues[0].render()


def test_git_identity_guard_rejects_known_private_email(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    private_email = "private.author@example.test"
    subprocess.run(["git", "config", "user.email", private_email], cwd=repo, check=True)
    monkeypatch.setattr(guardrails_check, "ROOT", repo)
    monkeypatch.setattr(
        guardrails_check,
        "KNOWN_PRIVATE_EMAIL_SHA256",
        {hashlib.sha256(private_email.encode("utf-8")).hexdigest()},
    )

    issues = guardrails_check.check_git_identity_privacy()

    assert len(issues) == 1
    assert "use a noreply identity" in issues[0].render()
    assert private_email not in issues[0].render()


def test_staged_guard_reads_index_instead_of_safer_worktree(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    candidate = repo / "settings.json"
    candidate.write_text(
        '{"api_key": "sk-proj-' + "A9b_" * 8 + '"}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "settings.json"], cwd=repo, check=True)
    candidate.write_text('{"api_key_env": "OM_LLM_API_KEY"}\n', encoding="utf-8")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    staged = guardrails_check.staged_file_paths()
    issues = guardrails_check.check_sensitive_repository_artifacts(
        guardrails_check.text_staged_files(staged),
        line_reader=guardrails_check.read_staged_lines,
    )

    assert len(issues) == 1
    assert "high-confidence credential pattern" in issues[0].render()


def test_staged_guard_reads_added_file_missing_from_worktree(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    candidate = repo / "settings.json"
    candidate.write_text(
        '{"api_key": "sk-proj-' + "B8c_" * 8 + '"}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "settings.json"], cwd=repo, check=True)
    candidate.unlink()
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    staged = guardrails_check.staged_file_paths()
    issues = guardrails_check.check_sensitive_repository_artifacts(
        guardrails_check.text_staged_files(staged),
        line_reader=guardrails_check.read_staged_lines,
    )

    assert len(issues) == 1


def test_pre_commit_hook_runs_staged_sensitive_guard() -> None:
    repo = Path(__file__).resolve().parents[1]
    hook = (repo / ".githooks" / "pre-commit").read_text(encoding="utf-8")

    assert "--staged" in hook
    assert "--check-sensitive-artifacts" in hook
