from __future__ import annotations

import pytest

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import stat
import subprocess
import threading

from src.application.release_target import parse_release_tags
from src.application.version_check import bump_version, check_version_update, compare_versions, update_local_version


def _write_version(tmp_path: Path, value: str) -> Path:
    (tmp_path / "VERSION").write_text(value + "\n", encoding="utf-8")
    return tmp_path


def _fixed_now() -> datetime:
    return datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _tag_listing_run(stdout: str):
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    return _run


def _auto_apply(**overrides) -> dict[str, object]:
    """Keyword defaults shared by the ``bump="auto"`` apply cases."""
    return {
        "bump": "auto",
        "apply": True,
        "recommendation_digest": "sha256:" + "a" * 64,
        "expected_base_version": "1.0.0",
        "expected_target_version": "1.1.0",
        **overrides,
    }


def _apply_isolated_version(base: Path, mode: str, target: str = "1.1.0"):
    if mode == "auto":
        return update_local_version(
            base_dir=base,
            **_auto_apply(recommendation_fn=lambda **_kwargs: _auto_recommendation()),
        )
    return update_local_version(base_dir=base, target_version=target, apply=True, now_fn=_fixed_now)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_version_apply_preserves_existing_fixed_temporary_file(tmp_path: Path, mode: str) -> None:
    base = _write_version(tmp_path, "1.0.0")
    version = base / "VERSION"
    original_mode = stat.S_IMODE(version.stat().st_mode)
    sentinel = base / "VERSION.tmp"
    sentinel.write_bytes(b"unrelated preparation notes\n")
    sentinel.chmod(0o600)

    out = _apply_isolated_version(base, mode)

    assert out["mode"] == "applied"
    assert version.read_bytes() == b"1.1.0\n"
    assert sentinel.read_bytes() == b"unrelated preparation notes\n"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o600
    assert stat.S_IMODE(version.stat().st_mode) == original_mode
    assert {path.name for path in base.iterdir()} == {"VERSION", "VERSION.tmp"}


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("failure", ["write", "replace"])
def test_version_apply_cleans_only_its_temporary_file_on_failure(tmp_path: Path, monkeypatch, mode, failure) -> None:
    base = _write_version(tmp_path, "1.0.0")
    version = base / "VERSION"
    sentinel = base / "VERSION.tmp"
    sentinel.write_bytes(b"unrelated preparation notes\n")
    before = {path.name: path.read_bytes() for path in base.iterdir()}
    write_text, replace = Path.write_text, Path.replace

    def fail_write(path, content, *args, **kwargs):
        if path.parent == base and path != version:
            write_text(path, content[:2], *args, **kwargs)
            raise OSError("fixture write failure")
        return write_text(path, content, *args, **kwargs)

    def fail_replace(path, target):
        if Path(target) == version:
            raise OSError("fixture replace failure")
        return replace(path, target)

    if failure == "write":
        monkeypatch.setattr(Path, "write_text", fail_write)
    else:
        monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match=f"fixture {failure} failure"):
        _apply_isolated_version(base, mode)

    assert {path.name: path.read_bytes() for path in base.iterdir()} == before


def test_version_apply_interleaving_publishes_each_writers_own_content(tmp_path: Path, monkeypatch) -> None:
    base = _write_version(tmp_path, "1.0.0")
    version = base / "VERSION"
    a_ready, b_ready, a_replaced = threading.Event(), threading.Event(), threading.Event()
    writer = threading.local()
    seen = {}
    replace = Path.replace

    def ordered_replace(path, target):
        if Path(target) != version:
            return replace(path, target)
        label = writer.label
        if label == "a":
            a_ready.set()
            assert b_ready.wait(5), "second writer did not finish staging"
        else:
            b_ready.set()
            assert a_replaced.wait(5), "first writer did not publish"
        try:
            seen[label] = path.read_text(encoding="utf-8")
            return replace(path, target)
        finally:
            if label == "a":
                a_replaced.set()

    def apply(label, target):
        writer.label = label
        try:
            return _apply_isolated_version(base, "manual", target)
        except Exception as exc:
            return exc

    monkeypatch.setattr(Path, "replace", ordered_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(apply, "a", "1.1.0")
        assert a_ready.wait(5), "first writer did not finish staging"
        second = executor.submit(apply, "b", "1.2.0")
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert seen == {"a": "1.1.0\n", "b": "1.2.0\n"}
    assert all(isinstance(result, dict) and result["mode"] == "applied" for result in results), results
    assert version.read_bytes() == b"1.2.0\n"
    assert {path.name for path in base.iterdir()} == {"VERSION"}


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
    tags = "\n".join(["abc refs/tags/not-a-version", "abc refs/tags/v0.1.0-beta.4", "abc refs/tags/v0.1.0-beta.10"])

    out = check_version_update(base_dir=base, run_cmd=_tag_listing_run(tags), now_fn=_fixed_now)
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
    out = check_version_update(
        base_dir=base, run_cmd=_tag_listing_run("abc refs/tags/v0.1.0\nabc refs/tags/v0.0.9\n"), now_fn=_fixed_now
    )
    assert out["ok"] is True
    assert out["update_available"] is False
    assert out["message"] == "没有可升级版本。当前已是最新版本 0.1.0"


def test_check_version_update_reports_current_ahead(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "0.2.0")
    out = check_version_update(
        base_dir=base, run_cmd=_tag_listing_run("abc refs/tags/v0.1.9\n"), now_fn=_fixed_now
    )
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

    out = check_version_update(base_dir=base, run_cmd=_tag_listing_run("abc refs/tags/foo\n"), now_fn=_fixed_now)
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
        **_auto_apply(
            remote_name="origin",
            recommendation_digest=digest,
            recommendation_fn=lambda **_kwargs: _auto_recommendation(digest=digest),
        ),
    )

    assert out["status"] == "applied"
    assert out["write"]["changed"] is True
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert (base / "CHANGELOG.md").read_text(encoding="utf-8") == "unchanged\n"


def test_update_local_version_auto_apply_rejects_stale_digest(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.0.0")

    out = update_local_version(
        base_dir=base,
        **_auto_apply(recommendation_fn=lambda **_kwargs: _auto_recommendation(digest="sha256:" + "b" * 64)),
    )

    assert out["status"] == "stale"
    assert out["reason_code"] == "RECOMMENDATION_STALE"
    assert out["write"]["changed"] is False
    assert (base / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_update_local_version_auto_retry_at_target_is_noop_without_remote(tmp_path: Path) -> None:
    base = _write_version(tmp_path, "1.1.0")

    def _unexpected(**_kwargs):
        raise AssertionError("already_at_target must not query remote")

    out = update_local_version(base_dir=base, **_auto_apply(recommendation_fn=_unexpected))

    assert out["status"] == "already_at_target"
    assert out["write"]["changed"] is False
    assert out["write"]["already_at_target"] is True


def test_bundled_version_checks_published_release_without_git(tmp_path, monkeypatch):
    from src.application import runtime_bundle
    (tmp_path / 'VERSION').write_text('1.2.3\n')
    (tmp_path / 'om.runtime.json').write_text('{}')
    monkeypatch.setattr(runtime_bundle, 'load_manifest', lambda base: {'repository': runtime_bundle.REPOSITORY})
    calls = []
    def published(tag, *, repository):
        calls.append((tag, repository))
        return dict(tag_name='v1.2.4', draft=False, prerelease=False)
    result = check_version_update(base_dir=tmp_path, release_resolver=published,
                                  run_cmd=lambda *a, **k: pytest.fail('Git called by bundled install'))
    assert result['ok'] and result['update_available'] and result['release_tag'] == 'v1.2.4'
    assert calls == [('latest', runtime_bundle.REPOSITORY)]


def test_broken_declared_manifest_is_version_failure_without_git(tmp_path):
    (tmp_path / 'VERSION').write_text('1.2.3\n')
    (tmp_path / 'runtime').mkdir()
    result = check_version_update(base_dir=tmp_path, run_cmd=lambda *a, **k: pytest.fail('Git fallback'))
    assert result['ok'] is False and result['latest_version'] is None


def test_dangling_bundle_marker_is_not_source_install(tmp_path):
    (tmp_path / 'VERSION').write_text('1.2.3\n')
    (tmp_path / 'om.runtime.json').symlink_to(tmp_path / 'missing')
    result = check_version_update(base_dir=tmp_path, run_cmd=lambda *a, **k: pytest.fail('Git fallback'))
    assert result['ok'] is False
