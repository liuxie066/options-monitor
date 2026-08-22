from __future__ import annotations

import pytest

from datetime import datetime, timezone
from pathlib import Path
import subprocess

from src.application.release_target import parse_release_tags
from src.application.version_check import bump_version, check_version_update, compare_versions, update_local_version


def _write_version(tmp_path: Path, value: str) -> Path:
    (tmp_path / "VERSION").write_text(value + "\n", encoding="utf-8")
    return tmp_path


def _fixed_now() -> datetime:
    return datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_version_orders_prerelease_before_stable() -> None:
    assert compare_versions("0.1.0-beta.3", "0.1.0") < 0
    assert compare_versions("0.1.0-beta.3", "0.1.0-beta.10") < 0
    assert compare_versions("0.2.0", "0.1.9") > 0


def test_bump_version_increments_core_and_drops_prerelease() -> None:
    assert bump_version("1.2.3", "patch") == "1.2.4"
    assert bump_version("1.2.3", "minor") == "1.3.0"
    assert bump_version("1.2.3-beta.1", "major") == "2.0.0"


def test_update_local_version_dry_run_does_not_write(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")

    out = update_local_version(base_dir=base, bump="patch", apply=False, now_fn=_fixed_now)

    assert out["mode"] == "dry_run"
    assert out["current_version"] == "1.0.0"
    assert out["target_version"] == "1.0.1"
    assert out["would_change"] is True
    assert out["changed"] is False
    assert out["updated_at"] == "2026-04-27T12:00:00Z"
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_update_local_version_apply_writes_version(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")

    out = update_local_version(base_dir=base, target_version="1.1.0", apply=True, now_fn=_fixed_now)

    assert out["mode"] == "applied"
    assert out["current_version"] == "1.0.0"
    assert out["target_version"] == "1.1.0"
    assert out["changed"] is True
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"


def test_update_local_version_rejects_downgrade_by_default(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")

    with pytest.raises(ValueError) as _caught:
        update_local_version(base_dir=base, target_version="0.9.9", apply=True)
    exc = _caught.value
    assert "lower than current VERSION" in str(exc)

    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_check_version_update_reports_newer_release(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.1.0-beta.3")

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    "abc refs/tags/not-a-version",
                    "abc refs/tags/v0.1.0-beta.4",
                    "abc refs/tags/v0.1.0-beta.10",
                ]
            ),
            stderr="",
        )

    out = check_version_update(base_dir=base, run_cmd=_run, now_fn=_fixed_now)
    assert out["ok"] is True
    assert out["current_version"] == "0.1.0-beta.3"
    assert out["latest_version"] == "0.1.0-beta.10"
    assert out["update_available"] is True
    assert out["release_tag"] == "v0.1.0-beta.10"
    assert out["checked_at"] == "2026-04-27T12:00:00Z"


def test_release_tag_parser_orders_multi_digit_patch_versions() -> None:
    tags = parse_release_tags(
        "\n".join(
            [
                "a refs/tags/v1.2.99",
                "b refs/tags/v1.2.100",
                "c refs/tags/v1.2.113",
            ]
        )
    )

    assert tags[-1] == ("1.2.113", "v1.2.113")


def test_check_version_update_reports_latest(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.1.0")

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="abc refs/tags/v0.1.0\nabc refs/tags/v0.0.9\n",
            stderr="",
        )

    out = check_version_update(base_dir=base, run_cmd=_run, now_fn=_fixed_now)
    assert out["ok"] is True
    assert out["update_available"] is False
    assert out["message"] == "没有可升级版本。当前已是最新版本 0.1.0"


def test_check_version_update_reports_current_ahead(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.2.0")

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="abc refs/tags/v0.1.9\n",
            stderr="",
        )

    out = check_version_update(base_dir=base, run_cmd=_run, now_fn=_fixed_now)
    assert out["ok"] is True
    assert out["update_available"] is False
    assert "高于远端最新版本" in out["message"]


def test_check_version_update_reports_remote_failure(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.1.0")

    def _run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(returncode=2, cmd=["git"], stderr="network down")

    out = check_version_update(base_dir=base, run_cmd=_run, now_fn=_fixed_now)
    assert out["ok"] is False
    assert out["current_version"] == "0.1.0"
    assert out["latest_version"] is None
    assert out["error"] == "network down"
    assert out["message"] == "版本检查失败"


def test_check_version_update_reports_missing_tags(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.1.0")

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="abc refs/tags/foo\n", stderr="")

    out = check_version_update(base_dir=base, run_cmd=_run, now_fn=_fixed_now)
    assert out["ok"] is False
    assert out["error"] == "no valid release tags found on remote"
    assert out["message"] == "未找到可用发布版本"


def _auto_recommendation(*, digest: str = "sha256:" + "a" * 64, target: str = "1.1.0") -> dict[str, object]:
    return {
        "schema_version": "release_version_recommendation.v1",
        "status": "recommended",
        "mode": "dry_run",
        "reason_code": None,
        "message": "recommended minor version bump",
        "base": {"version": "1.0.0", "tag": "v1.0.0"},
        "workspace": {"head": "abc", "changed_files": ["CHANGELOG.md"]},
        "recommendation": {
            "bump": "minor",
            "target_version": target,
            "classification_basis": "changelog_unreleased",
            "declaration_status": "complete",
            "manual_review_required": True,
        },
        "evidence": {"added": ["Feature"]},
        "review_flags": [],
        "recommendation_digest": digest,
        "write": {"changed": False, "already_at_target": False},
    }


def test_update_local_version_auto_preview_is_read_only(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")
    calls: list[tuple[Path, str]] = []

    def _recommend(*, base_dir: Path, remote_name: str):
        calls.append((base_dir, remote_name))
        return _auto_recommendation()

    out = update_local_version(base_dir=base, bump="auto", remote_name="upstream", recommendation_fn=_recommend)

    assert out["status"] == "recommended"
    assert calls == [(base.resolve(), "upstream")]
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_update_local_version_auto_apply_recomputes_and_writes_only_version(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")
    (base / "CHANGELOG.md").write_text("unchanged\n", encoding="utf-8")
    digest = "sha256:" + "a" * 64

    out = update_local_version(
        base_dir=base,
        bump="auto",
        apply=True,
        remote_name="origin",
        recommendation_digest=digest,
        expected_base_version="1.0.0",
        expected_target_version="1.1.0",
        recommendation_fn=lambda **_kwargs: _auto_recommendation(digest=digest),
    )

    assert out["status"] == "applied"
    assert out["write"]["changed"] is True
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert (base / "CHANGELOG.md").read_text(encoding="utf-8") == "unchanged\n"


def test_update_local_version_auto_apply_rejects_stale_digest(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")

    out = update_local_version(
        base_dir=base,
        bump="auto",
        apply=True,
        recommendation_digest="sha256:" + "a" * 64,
        expected_base_version="1.0.0",
        expected_target_version="1.1.0",
        recommendation_fn=lambda **_kwargs: _auto_recommendation(digest="sha256:" + "b" * 64),
    )

    assert out["status"] == "stale"
    assert out["reason_code"] == "RECOMMENDATION_STALE"
    assert out["write"]["changed"] is False
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_update_local_version_auto_retry_at_target_is_noop_without_remote(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.1.0")

    def _unexpected(**_kwargs):
        raise AssertionError("already_at_target must not query remote")

    out = update_local_version(
        base_dir=base,
        bump="auto",
        apply=True,
        recommendation_digest="sha256:" + "a" * 64,
        expected_base_version="1.0.0",
        expected_target_version="1.1.0",
        recommendation_fn=_unexpected,
    )

    assert out["status"] == "already_at_target"
    assert out["write"]["changed"] is False
    assert out["write"]["already_at_target"] is True
