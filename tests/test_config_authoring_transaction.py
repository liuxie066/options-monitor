from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import (
    config_source_sha256,
    locked_config_authoring,
    publish_yaml_config_generation,
    publish_yaml_config_generation_locked,
)
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.runtime_config_freshness import GENERATED_KEY, check_runtime_config_freshness


REPO_ROOT = Path(__file__).resolve().parents[1]


def _config_doc() -> dict:
    return {
        "accounts": {
            "lx": {
                "type": "futu",
                "futu_account_id": "12345678",
            }
        },
        "markets": {
            "us": {"accounts": ["lx"], "symbols": ["NVDA"]},
            "hk": {"accounts": ["lx"], "symbols": ["0700.HK"]},
        },
    }


def _write_yaml(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _pending_transaction(
    *,
    transaction_module,
    runtime_root: Path,
    source: Path,
    before_source_sha: str,
    after_source_sha: str,
    targets: list[dict],
    audit_id: str,
) -> Path:
    manifest = transaction_module._prepare_transaction_manifest(
        transaction_dir=runtime_root / "output_shared" / "state" / "config_authoring_transactions" / audit_id,
        audit_id=audit_id,
        source_path=source,
        before_source_sha=before_source_sha,
        after_source_sha=after_source_sha,
        targets=targets,
    )
    transaction_module._set_manifest_phase(manifest, "committing")
    return manifest


def test_config_authoring_rejects_stale_preview_without_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = _config_doc()
    _write_yaml(config_path, original)
    expected = config_source_sha256(config_path)
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")
    _write_yaml(config_path, changed)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=original,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
            expected_source_sha256=expected,
        )

    assert exc_info.value.code == "STALE_PREVIEW"
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == changed


def test_config_authoring_rejects_source_change_during_generation_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    config_path = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(config_path, before_doc)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    concurrent_doc = _config_doc()
    concurrent_doc["markets"]["us"]["symbols"].append("AMD")
    original_prepare = transaction_module._prepare_generation

    def _prepare_then_change_source(**kwargs):  # type: ignore[no-untyped-def]
        prepared = original_prepare(**kwargs)
        _write_yaml(config_path, concurrent_doc)
        return prepared

    monkeypatch.setattr(transaction_module, "_prepare_generation", _prepare_then_change_source)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
        )

    assert exc_info.value.code == "STALE_PREVIEW"
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == concurrent_doc


def test_config_authoring_compensates_generation_when_source_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    config_path = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(config_path, before_doc)
    us_path = tmp_path / "config.us.json"
    hk_path = tmp_path / "config.hk.json"
    assistant_path = tmp_path / "resolved" / "config.assistant.json"
    assistant_path.parent.mkdir(parents=True)
    us_path.write_text('{"old":"us"}\n', encoding="utf-8")
    hk_path.write_text('{"old":"hk"}\n', encoding="utf-8")
    assistant_path.write_text('{"old":"assistant"}\n', encoding="utf-8")
    before_bytes = {
        config_path: config_path.read_bytes(),
        us_path: us_path.read_bytes(),
        hk_path: hk_path.read_bytes(),
        assistant_path: assistant_path.read_bytes(),
    }
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    original_atomic_write = transaction_module._atomic_write_bytes
    failed = False

    def _fail_source_once(path: Path, payload: bytes) -> None:
        nonlocal failed
        if path.resolve() == config_path.resolve() and not failed:
            failed = True
            raise OSError("injected source commit failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(transaction_module, "_atomic_write_bytes", _fail_source_once)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
        )

    assert exc_info.value.code == "CONFIG_WRITE_FAILED"
    assert exc_info.value.details["recovery_error"] is None
    assert exc_info.value.details["write_applied"] is True
    assert exc_info.value.details["stage"] == "commit"
    assert exc_info.value.details["backup_write_applied"] is True
    assert exc_info.value.details["transaction_write_applied"] is True
    assert not Path(exc_info.value.details["transaction_dir"]).exists()
    compensation = exc_info.value.details["recovered_transactions"][0]
    assert compensation["mode"] == "roll_back"
    assert compensation["cleanup"] is True
    assert any(item["write_applied"] for item in compensation["targets"])
    for path, payload in before_bytes.items():
        assert path.read_bytes() == payload
    assert json.loads(us_path.read_text(encoding="utf-8")) == {"old": "us"}


def test_config_authoring_retarget_preserves_effective_fingerprint_for_new_document(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    before_sha = config_source_sha256(source)
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")
    changed["markets"]["hk"]["symbols"].append("9988.HK")
    comparison_source = tmp_path / "expected.yaml"
    _write_yaml(comparison_source, changed)
    expected_effective = {}
    for market in ("us", "hk"):
        config, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market=market, config_path=comparison_source)
        expected_effective[market] = next(
            item["effective"] for item in config[GENERATED_KEY]["sources"] if item["role"] == "market_user"
        )

    result = publish_yaml_config_generation(
        repo_root=REPO_ROOT, config_yaml_path=source, config_doc=changed,
        runtime_root=tmp_path, markets=["us", "hk"], include_assistant=False,
        apply=True, expected_source_sha256=before_sha,
    )

    assert result["write_applied"] is True
    after_sha = config_source_sha256(source)
    assert before_sha != after_sha
    assert result["source_revision"] == {"before_sha256": before_sha, "after_sha256": after_sha}
    for market in ("us", "hk"):
        runtime = tmp_path / f"config.{market}.json"
        config = json.loads(runtime.read_text(encoding="utf-8"))
        record = next(item for item in config[GENERATED_KEY]["sources"] if item["role"] == "market_user")
        assert record["path"] == str(source)
        assert record["sha256"] == after_sha
        assert record["effective"] == expected_effective[market]
        assert record["effective"]["kind"] == "yaml-market-user-v1"
        assert record["effective"]["market"] == market
        assert config["_resolved"]["config_yaml_path"] == str(source)
        assert config["_resolved"]["config_yaml_sha256"] == after_sha
        assert check_runtime_config_freshness(
            config, repo_root=REPO_ROOT, market=market, runtime_config_path=runtime,
        )["ok"] is True


def test_config_authoring_assistant_only_edit_still_invalidates_preview_sha(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    original = _config_doc()
    _write_yaml(source, original)
    expected_sha = config_source_sha256(source)
    changed = _config_doc()
    changed["assistant"] = {"enabled": False}
    _write_yaml(source, changed)
    before = source.read_bytes()

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT, config_yaml_path=source, config_doc=original,
            runtime_root=tmp_path, markets=["us", "hk"], apply=True,
            expected_source_sha256=expected_sha,
        )

    assert exc.value.code == "STALE_PREVIEW"
    assert source.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_config_authoring_dry_run_creates_no_state_and_does_not_recover(tmp_path: Path) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    runtime = tmp_path / "config.us.json"
    runtime.write_text('{"old":true}\n', encoding="utf-8")
    desired_runtime = b'{"recovered":true}\n'
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha="unused",
        targets=[{"role": "runtime_us", "path": runtime, "payload": desired_runtime, "source": False}],
        audit_id="dry-run-pending",
    )
    before_state = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = publish_yaml_config_generation(
        repo_root=REPO_ROOT,
        config_yaml_path=source,
        config_doc=before_doc,
        runtime_root=tmp_path,
        markets=["us"],
        include_assistant=False,
        apply=False,
        expected_source_sha256=before_sha,
    )

    assert result["dry_run"] is True
    assert result["recovered_transactions"] == []
    assert runtime.read_text(encoding="utf-8") == '{"old":true}\n'
    assert manifest.exists()
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before_state


def test_regular_publisher_recovers_before_rejecting_stale_source(tmp_path: Path) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    after_sha = transaction_module._bytes_sha256(after_bytes)
    runtime = tmp_path / "config.us.json"
    runtime.write_text('{"old":true}\n', encoding="utf-8")
    desired_runtime = b'{"recovered":true}\n'
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=after_sha,
        targets=[
            {"role": "runtime_us", "path": runtime, "payload": desired_runtime, "source": False},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id="recover-before-read",
    )
    source.write_bytes(after_bytes)

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=before_doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=before_sha,
        )

    assert exc.value.code == "STALE_PREVIEW"
    assert exc.value.details["write_applied"] is True
    recovered = exc.value.details["recovered_transactions"]
    assert recovered[0]["audit_id"] == "recover-before-read"
    assert recovered[0]["mode"] == "roll_forward"
    assert recovered[0]["write_applied"] is True
    assert recovered[0]["cleanup"] is True
    assert recovered[0]["source_revision"] == {
        "before_sha256": before_sha,
        "after_sha256": after_sha,
    }
    assert runtime.read_bytes() == desired_runtime
    assert not manifest.exists()


def test_regular_publisher_preserves_first_recovery_when_later_manifest_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    doc = _config_doc()
    _write_yaml(source, doc)
    source_sha = config_source_sha256(source)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b'{"old":1}\n')
    second.write_bytes(b'{"old":2}\n')
    first_manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[{"role": "first", "path": first, "payload": b'{"new":1}\n'}],
        audit_id="a-first-recovery",
    )
    second_manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[{"role": "second", "path": second, "payload": b'{"new":2}\n'}],
        audit_id="b-read-failure",
    )
    first.write_bytes(b'{"partially-committed":1}\n')
    original_read_manifest = transaction_module._read_manifest
    second_reads = 0

    def _fail_second_recovery_read(path: Path) -> dict:
        nonlocal second_reads
        if path == second_manifest:
            second_reads += 1
            if second_reads == 2:
                raise OSError("injected second manifest read failure")
        return original_read_manifest(path)

    monkeypatch.setattr(transaction_module, "_read_manifest", _fail_second_recovery_read)

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["stage"] == "recovery"
    assert exc.value.details["write_applied"] is True
    audits = exc.value.details["recovered_transactions"]
    assert audits[0]["audit_id"] == "a-first-recovery"
    assert audits[0]["write_applied"] is True
    assert audits[1]["audit_id"] == "b-read-failure"
    assert audits[1]["write_applied"] is None
    assert first.read_bytes() == b'{"old":1}\n'
    assert not first_manifest.exists()
    assert second_manifest.exists()
    assert source.read_bytes() == yaml.safe_dump(doc, sort_keys=False).encode("utf-8")


@pytest.mark.parametrize("violation", ["special_mode", "other_owner"])
def test_regular_publisher_rejects_unsafe_pending_target_before_recovery(
    violation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    doc = _config_doc()
    _write_yaml(source, doc)
    source_before = source.read_bytes()
    source_sha = config_source_sha256(source)
    target = tmp_path / "config.us.json"
    target.write_bytes(b'{"old":true}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[{"role": "runtime_us", "path": target, "payload": b'{"new":true}\n'}],
        audit_id=f"unsafe-{violation}",
    )
    target_before = target.read_bytes()
    if violation == "special_mode":
        target.chmod(0o1644)
    else:
        original_stat = Path.stat

        def _other_owner(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_stat(path, *args, **kwargs)
            if path == target:
                values = list(result)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(Path, "stat", _other_owner)

    with pytest.raises(ValueError, match="special config mode|deployment user"):
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert target.read_bytes() == target_before
    assert source.read_bytes() == source_before
    assert manifest.exists()


def test_regular_publisher_allows_safe_source_outside_runtime_root_during_recovery(
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    runtime_root = tmp_path / "runtime"
    source = tmp_path / "authoring" / "config.yaml"
    source.parent.mkdir()
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    source_sha = config_source_sha256(source)
    runtime = runtime_root / "config.us.json"
    runtime.parent.mkdir()
    runtime.write_bytes(b'{"old":true}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=runtime_root,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[
            {"role": "runtime_us", "path": runtime, "payload": b'{"pending":true}\n'},
            {"role": "config_yaml", "path": source, "payload": b"unused", "source": True},
        ],
        audit_id="outside-source-recovery",
    )
    runtime.write_bytes(b'{"partially-committed":true}\n')
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")

    result = publish_yaml_config_generation(
        repo_root=REPO_ROOT,
        config_yaml_path=source,
        config_doc=changed,
        runtime_root=runtime_root,
        markets=["us"],
        include_assistant=False,
        apply=True,
        backup=False,
        expected_source_sha256=source_sha,
    )

    assert result["write_applied"] is True
    assert result["recovered_transactions"][0]["audit_id"] == "outside-source-recovery"
    assert result["recovered_transactions"][0]["mode"] == "roll_back"
    assert not manifest.exists()
    assert yaml.safe_load(source.read_text(encoding="utf-8")) == changed


def test_regular_publisher_rejects_missing_existing_target_before_any_journal_recovery(
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    doc = _config_doc()
    _write_yaml(source, doc)
    source_before = source.read_bytes()
    source_sha = config_source_sha256(source)
    earlier_target = tmp_path / "earlier.json"
    missing_target = tmp_path / "missing.json"
    earlier_target.write_bytes(b'{"old":"earlier"}\n')
    missing_target.write_bytes(b'{"old":"missing"}\n')
    earlier_manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[
            {"role": "earlier", "path": earlier_target, "payload": b'{"new":"earlier"}\n'}
        ],
        audit_id="a-earlier-recovery",
    )
    missing_manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha="f" * 64,
        targets=[
            {"role": "missing", "path": missing_target, "payload": b'{"new":"missing"}\n'}
        ],
        audit_id="b-missing-target",
    )
    earlier_target.write_bytes(b'{"partially-committed":"earlier"}\n')
    earlier_before = earlier_target.read_bytes()
    missing_target.unlink()
    journal_root = tmp_path / "output_shared" / "state" / "config_authoring_transactions"
    journals_before = {
        path.relative_to(journal_root): path.read_bytes()
        for path in journal_root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["stage"] == "target_preflight"
    assert exc.value.details["target"] == str(missing_target)
    assert exc.value.details["write_applied"] is False
    assert exc.value.details["recovered_transactions"][0]["audit_id"] == "b-missing-target"
    assert earlier_target.read_bytes() == earlier_before
    assert source.read_bytes() == source_before
    assert earlier_manifest.exists()
    assert missing_manifest.exists()
    assert {
        path.relative_to(journal_root): path.read_bytes()
        for path in journal_root.rglob("*")
        if path.is_file()
    } == journals_before


def test_recovery_creates_target_that_did_not_exist_when_journal_was_prepared(
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    new_target = tmp_path / "new-runtime.json"
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=transaction_module._bytes_sha256(after_bytes),
        targets=[
            {"role": "new_runtime", "path": new_target, "payload": b'{"created":true}\n'}
        ],
        audit_id="new-target",
    )
    source.write_bytes(after_bytes)

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        recovered = lock.recovered_transactions

    assert recovered[0]["mode"] == "roll_forward"
    assert recovered[0]["write_applied"] is True
    assert new_target.read_bytes() == b'{"created":true}\n'
    assert not manifest.exists()


def test_committed_cleanup_allows_target_that_disappeared_after_commit(tmp_path: Path) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_sha = config_source_sha256(source)
    target = tmp_path / "installed.json"
    target.write_bytes(b'{"installed":true}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha=source_sha,
        targets=[{"role": "runtime", "path": target, "payload": target.read_bytes()}],
        audit_id="committed-missing-target",
    )
    transaction_module._set_manifest_phase(manifest, "committed")
    target.unlink()

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        recovered = lock.recovered_transactions

    assert recovered[0]["mode"] == "cleanup_committed"
    assert recovered[0]["write_applied"] is False
    assert not manifest.exists()


def test_held_lock_publish_rejects_released_or_wrong_root(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        with pytest.raises(AgentToolError, match="same runtime root"):
            publish_yaml_config_generation_locked(
                lock=lock,
                repo_root=REPO_ROOT,
                config_yaml_path=source,
                config_doc=after_doc,
                runtime_root=tmp_path / "other",
                markets=["us"],
                include_assistant=False,
                expected_source_sha256=before_sha,
            )
        result = publish_yaml_config_generation_locked(
            lock=lock,
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            expected_source_sha256=before_sha,
        )

    assert result["write_applied"] is True
    with pytest.raises(AgentToolError, match="live config authoring lock"):
        publish_yaml_config_generation_locked(
            lock=lock,
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
        )


def test_authoring_lock_is_released_when_recovery_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    original_recovery = transaction_module._recover_incomplete_transactions

    def _interrupt_recovery(*, state_root: Path) -> list[dict]:
        del state_root
        raise KeyboardInterrupt

    monkeypatch.setattr(transaction_module, "_recover_incomplete_transactions", _interrupt_recovery)
    interrupted_lock = locked_config_authoring(runtime_root=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with interrupted_lock:
            pytest.fail("interrupted recovery must not enter the authoring window")

    assert interrupted_lock.handle is None
    monkeypatch.setattr(transaction_module, "_recover_incomplete_transactions", original_recovery)
    with locked_config_authoring(runtime_root=tmp_path) as reacquired:
        assert reacquired.handle is not None


def test_committed_journal_cleanup_is_not_reported_as_a_config_write(tmp_path: Path) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_sha = config_source_sha256(source)
    runtime = tmp_path / "config.us.json"
    runtime.write_text('{"installed":true}\n', encoding="utf-8")
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha=source_sha,
        targets=[{"role": "runtime_us", "path": runtime, "payload": runtime.read_bytes(), "source": False}],
        audit_id="committed-cleanup",
    )
    transaction_module._set_manifest_phase(manifest, "committed")

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        recovered = lock.recovered_transactions

    assert recovered == [
        {
            "audit_id": "committed-cleanup",
            "mode": "cleanup_committed",
            "targets": [],
            "source_revision": {"before_sha256": source_sha, "after_sha256": source_sha},
            "write_applied": False,
            "cleanup": True,
        }
    ]
    assert runtime.read_text(encoding="utf-8") == '{"installed":true}\n'
    assert not manifest.exists()


def test_current_publish_cleanup_failure_is_not_hidden_by_cleanup_only_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    runtime = tmp_path / "config.us.json"
    runtime.write_bytes(b'{"old":true}\n')
    prior_manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=before_sha,
        targets=[{"role": "runtime_us", "path": runtime, "payload": runtime.read_bytes()}],
        audit_id="prior-cleanup-only",
    )
    transaction_module._set_manifest_phase(prior_manifest, "committed")
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")
    original_rmtree = transaction_module.shutil.rmtree

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        assert lock.recovered_transactions[0]["mode"] == "cleanup_committed"
        assert lock.recovered_transactions[0]["write_applied"] is False

        def _fail_current_cleanup(path: str | Path, *args: object, **kwargs: object) -> None:
            if Path(path).name.startswith("cfg-"):
                raise OSError("injected current transaction cleanup failure")
            original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(transaction_module.shutil, "rmtree", _fail_current_cleanup)
        with pytest.raises(AgentToolError) as exc:
            publish_yaml_config_generation_locked(
                lock=lock,
                repo_root=REPO_ROOT,
                config_yaml_path=source,
                config_doc=changed,
                runtime_root=tmp_path,
                markets=["us"],
                include_assistant=False,
                backup=False,
                expected_source_sha256=before_sha,
            )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    assert exc.value.details["write_applied"] is True
    assert exc.value.details["cleanup"] is False
    assert all(target["write_applied"] is True for target in exc.value.details["targets"])
    assert exc.value.details["recovered_transactions"][0]["audit_id"] == "prior-cleanup-only"
    assert config_source_sha256(source) != before_sha
    current_manifest = Path(exc.value.details["transaction_manifest"])
    assert current_manifest.exists()
    assert json.loads(current_manifest.read_text(encoding="utf-8"))["phase"] == "committed"


def test_recovery_failure_reports_preflight_and_partial_target_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    after_sha = transaction_module._bytes_sha256(after_bytes)
    first = tmp_path / "config.us.json"
    second = tmp_path / "config.hk.json"
    first.write_text('{"old":"us"}\n', encoding="utf-8")
    second.write_text('{"old":"hk"}\n', encoding="utf-8")
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=after_sha,
        targets=[
            {"role": "runtime_us", "path": first, "payload": b'{"new":"us"}\n', "source": False},
            {"role": "runtime_hk", "path": second, "payload": b'{"new":"hk"}\n', "source": False},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id="partial-recovery",
    )
    source.write_bytes(after_bytes)
    original_write = transaction_module._atomic_write_bytes

    def _fail_second(path: Path, payload: bytes) -> None:
        if path.resolve() == second.resolve():
            raise OSError("injected recovery failure")
        original_write(path, payload)

    monkeypatch.setattr(transaction_module, "_atomic_write_bytes", _fail_second)
    preflight_targets: list[Path] = []

    with pytest.raises(AgentToolError) as exc:
        with locked_config_authoring(
            runtime_root=tmp_path,
            preflight=lambda paths: preflight_targets.extend(paths),
        ):
            pytest.fail("recovery failure must prevent entering the authoring window")

    assert preflight_targets == [first.resolve(), second.resolve(), source.resolve()]
    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["stage"] == "target"
    assert exc.value.details["write_applied"] is True
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["targets"][0]["write_applied"] is True
    assert audit["targets"][1]["write_applied"] is None
    assert audit["cleanup"] is False
    assert first.read_bytes() == b'{"new":"us"}\n'
    assert second.read_text(encoding="utf-8") == '{"old":"hk"}\n'
    assert manifest.exists()


def test_first_recovery_target_failure_preserves_unknown_write_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    after_sha = transaction_module._bytes_sha256(after_bytes)
    target = tmp_path / "config.us.json"
    target.write_text('{"old":"us"}\n', encoding="utf-8")
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=after_sha,
        targets=[
            {"role": "runtime_us", "path": target, "payload": b'{"new":"us"}\n', "source": False},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id="unknown-first-target",
    )
    source.write_bytes(after_bytes)

    def _fail_first(_path: Path, _payload: bytes) -> None:
        raise OSError("injected unknown first-target failure")

    monkeypatch.setattr(transaction_module, "_atomic_write_bytes", _fail_first)

    with pytest.raises(AgentToolError) as exc:
        with locked_config_authoring(runtime_root=tmp_path):
            pytest.fail("recovery failure must prevent entering the authoring window")

    assert exc.value.details["write_applied"] is None
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["write_applied"] is None
    assert audit["targets"][0]["write_applied"] is None
    assert audit["cleanup"] is False
    assert manifest.exists()


@pytest.mark.parametrize("mutation", ["missing", "tampered", "truncated"])
@pytest.mark.parametrize("mode", ["roll_forward", "roll_back"])
def test_recovery_validates_every_journal_artifact_before_live_writes(
    mutation: str,
    mode: str,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(source, before_doc)
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    first = tmp_path / "config.us.json"
    second = tmp_path / "config.hk.json"
    first.write_bytes(b'{"old":"us"}\n')
    second.write_bytes(b'{"old":"hk"}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=transaction_module._bytes_sha256(after_bytes),
        targets=[
            {"role": "runtime_us", "path": first, "payload": b'{"new":"us"}\n'},
            {"role": "runtime_hk", "path": second, "payload": b'{"new":"hk"}\n'},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id=f"invalid-{mode}-{mutation}",
    )
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    artifact_key = "desired_path" if mode == "roll_forward" else "backup_path"
    artifact = Path(manifest_doc["targets"][1][artifact_key])
    if mode == "roll_forward":
        source.write_bytes(after_bytes)
    else:
        first.write_bytes(b'{"partially-committed":"us"}\n')
        second.write_bytes(b'{"partially-committed":"hk"}\n')
    live_before = {path: path.read_bytes() for path in (source, first, second)}
    if mutation == "missing":
        artifact.unlink()
    elif mutation == "tampered":
        artifact.write_bytes(b"tampered")
    else:
        artifact.write_bytes(artifact.read_bytes()[:1])

    with pytest.raises(AgentToolError) as exc:
        with locked_config_authoring(runtime_root=tmp_path):
            pytest.fail("invalid journal must prevent entering the authoring window")

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["stage"] == "journal_validation"
    assert exc.value.details["write_applied"] is False
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["mode"] == mode
    assert audit["targets"] == []
    assert audit["write_applied"] is False
    assert audit["cleanup"] is False
    assert {path: path.read_bytes() for path in live_before} == live_before
    assert manifest.exists()


def test_commit_validates_all_journal_payloads_before_first_live_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    before_sha = config_source_sha256(source)
    first = tmp_path / "config.us.json"
    second = tmp_path / "config.hk.json"
    first.write_bytes(b'{"old":"us"}\n')
    second.write_bytes(b'{"old":"hk"}\n')
    live_before = {path: path.read_bytes() for path in (source, first, second)}
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    original_set_phase = transaction_module._set_manifest_phase

    def _tamper_before_commit(path: Path, phase: str) -> None:
        original_set_phase(path, phase)
        if phase == "committing":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            Path(manifest["targets"][1]["desired_path"]).write_bytes(b"tampered")

    monkeypatch.setattr(transaction_module, "_set_manifest_phase", _tamper_before_commit)

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            include_assistant=False,
            apply=True,
            backup=False,
            expected_source_sha256=before_sha,
        )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    assert exc.value.details["write_applied"] is False
    assert exc.value.details["backup_path"] is None
    assert exc.value.details["backup_write_applied"] is False
    assert exc.value.details["transaction_write_applied"] is True
    assert exc.value.details["targets"] == []
    assert {path: path.read_bytes() for path in live_before} == live_before
    manifest_path = Path(exc.value.details["transaction_manifest"])
    assert manifest_path.exists()
    recovery = exc.value.details["recovered_transactions"][0]
    assert recovery["write_applied"] is False
    assert recovery["cleanup"] is False


@pytest.mark.parametrize("readback_failure", ["error", "mismatch"])
def test_recovery_readback_failure_keeps_journal_and_effect_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    readback_failure: str,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    runtime = tmp_path / "config.us.json"
    runtime.write_bytes(b'{"old":true}\n')
    desired_runtime = b'{"recovered":true}\n'
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=transaction_module._bytes_sha256(after_bytes),
        targets=[
            {"role": "runtime_us", "path": runtime, "payload": desired_runtime},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id="source-readback-failure",
    )
    source.write_bytes(after_bytes)
    original_source_sha = transaction_module.config_source_sha256
    reads = 0

    def _fail_second_source_read(path: str | Path) -> str:
        nonlocal reads
        reads += 1
        if reads == 2:
            if readback_failure == "error":
                raise OSError("injected source readback failure")
            return "0" * 64
        return original_source_sha(path)

    monkeypatch.setattr(transaction_module, "config_source_sha256", _fail_second_source_read)

    with pytest.raises(AgentToolError) as exc:
        with locked_config_authoring(runtime_root=tmp_path):
            pytest.fail("readback failure must prevent entering the authoring window")

    assert exc.value.details["stage"] == "source_readback"
    assert exc.value.details["write_applied"] is True
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["mode"] == "roll_forward"
    assert audit["write_applied"] is True
    assert audit["cleanup"] is False
    assert runtime.read_bytes() == desired_runtime
    assert manifest.exists()


def test_manifest_is_published_after_journal_artifacts_and_directories_are_flushed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_sha = config_source_sha256(source)
    target = tmp_path / "config.us.json"
    target.write_bytes(b'{"old":true}\n')
    transaction_dir = (
        tmp_path
        / "output_shared"
        / "state"
        / "config_authoring_transactions"
        / "durability-order"
    )
    events: list[tuple[str, Path]] = []
    original_artifact_write = transaction_module._write_journal_artifact
    original_directory_fsync = transaction_module._fsync_directory
    original_atomic_write = transaction_module._atomic_write_bytes

    def _artifact_write(path: Path, payload: bytes) -> None:
        original_artifact_write(path, payload)
        events.append(("artifact", path))

    def _directory_fsync(path: Path) -> None:
        original_directory_fsync(path)
        events.append(("directory", path))

    def _atomic_write(path: Path, payload: bytes) -> None:
        events.append(("manifest", path))
        original_atomic_write(path, payload)

    monkeypatch.setattr(transaction_module, "_write_journal_artifact", _artifact_write)
    monkeypatch.setattr(transaction_module, "_fsync_directory", _directory_fsync)
    monkeypatch.setattr(transaction_module, "_atomic_write_bytes", _atomic_write)

    previous_umask = os.umask(0o022)
    try:
        manifest = transaction_module._prepare_transaction_manifest(
            transaction_dir=transaction_dir,
            audit_id="durability-order",
            source_path=source,
            before_source_sha=source_sha,
            after_source_sha=source_sha,
            targets=[{"role": "runtime_us", "path": target, "payload": b'{"new":true}\n'}],
        )
    finally:
        os.umask(previous_umask)

    manifest_event = events.index(("manifest", manifest))
    assert events[0] == ("directory", transaction_dir.parent)
    assert all(index < manifest_event for index, event in enumerate(events) if event[0] == "artifact")
    assert ("directory", transaction_dir) in events[:manifest_event]
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    artifact_paths = [
        Path(value)
        for target_item in manifest_doc["targets"]
        for value in (target_item["desired_path"], target_item["backup_path"])
        if value
    ]
    assert artifact_paths
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in artifact_paths)


def test_later_publish_error_preserves_prior_lock_recovery_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    before_sha = config_source_sha256(source)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    after_bytes = yaml.safe_dump(after_doc, sort_keys=False).encode("utf-8")
    after_sha = transaction_module._bytes_sha256(after_bytes)
    runtime = tmp_path / "config.us.json"
    runtime.write_bytes(b'{"old":true}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=before_sha,
        after_source_sha=after_sha,
        targets=[
            {"role": "runtime_us", "path": runtime, "payload": b'{"recovered":true}\n'},
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True},
        ],
        audit_id="recovery-before-raw-error",
    )
    source.write_bytes(after_bytes)

    with locked_config_authoring(runtime_root=tmp_path) as lock:
        assert not manifest.exists()
        monkeypatch.setattr(
            transaction_module,
            "_prepare_generation",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("injected prepare failure")),
        )
        with pytest.raises(AgentToolError) as exc:
            publish_yaml_config_generation_locked(
                lock=lock,
                repo_root=REPO_ROOT,
                config_yaml_path=source,
                config_doc=after_doc,
                runtime_root=tmp_path,
                markets=["us"],
                include_assistant=False,
                expected_source_sha256=after_sha,
            )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    assert "injected prepare failure" in exc.value.details["error"]
    assert exc.value.details["write_applied"] is True
    assert exc.value.details["recovered_transactions"][0]["audit_id"] == "recovery-before-raw-error"


def test_pending_manifest_with_missing_target_path_reports_no_write_audit(
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_before = source.read_bytes()
    source_sha = config_source_sha256(source)
    runtime = tmp_path / "config.us.json"
    runtime.write_bytes(b'{"old":true}\n')
    manifest = _pending_transaction(
        transaction_module=transaction_module,
        runtime_root=tmp_path,
        source=source,
        before_source_sha=source_sha,
        after_source_sha=source_sha,
        targets=[{"role": "runtime_us", "path": runtime, "payload": b'{"new":true}\n'}],
        audit_id="malformed-target",
    )
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    del manifest_doc["targets"][0]["path"]
    manifest.write_text(json.dumps(manifest_doc), encoding="utf-8")
    journal_before = {
        path: path.read_bytes()
        for path in manifest.parent.iterdir()
        if path.is_file()
    }

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=_config_doc(),
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["audit_id"] == "malformed-target"
    assert exc.value.details["manifest"] == str(manifest)
    assert exc.value.details["stage"] == "recovery"
    assert exc.value.details["write_applied"] is False
    assert len(exc.value.details["recovered_transactions"]) == 1
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["audit_id"] == "malformed-target"
    assert audit["write_applied"] is False
    assert source.read_bytes() == source_before
    assert runtime.read_bytes() == b'{"old":true}\n'
    assert {
        path: path.read_bytes()
        for path in manifest.parent.iterdir()
        if path.is_file()
    } == journal_before


def test_backup_success_then_manifest_prepare_failure_reports_durable_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_before = source.read_bytes()
    source_sha = config_source_sha256(source)
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")
    observed_paths: list[Path | None] = []
    original_path_exists = transaction_module._path_exists

    def _observe_non_backup_path(path: Path | None) -> bool | None:
        observed_paths.append(path)
        if path is not None and ".bak." in path.name:
            return None
        return original_path_exists(path)

    monkeypatch.setattr(transaction_module, "_path_exists", _observe_non_backup_path)
    monkeypatch.setattr(
        transaction_module,
        "_prepare_transaction_manifest",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("injected manifest preparation failure")),
    )

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=changed,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    details = exc.value.details
    assert details["stage"] == "transaction_prepare"
    assert details["audit_id"].startswith("cfg-")
    backup_path = Path(details["backup_path"])
    assert backup_path == source.with_name(f"config.yaml.bak.{details['audit_id']}")
    assert backup_path.read_bytes() == source_before
    assert details["backup_write_applied"] is True
    assert backup_path not in observed_paths
    assert details["transaction_write_applied"] is False
    assert details["write_applied"] is True
    assert Path(details["transaction_manifest"]).parent == Path(details["transaction_dir"])
    assert not Path(details["transaction_dir"]).exists()
    assert source.read_bytes() == source_before
    assert not (tmp_path / "config.us.json").exists()


def test_partial_backup_failure_reports_known_backup_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_before = source.read_bytes()
    source_sha = config_source_sha256(source)
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")

    def _partial_copy(_source: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"partial backup")
        raise OSError("injected partial backup failure")

    monkeypatch.setattr(transaction_module.shutil, "copy2", _partial_copy)

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=changed,
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    details = exc.value.details
    assert details["stage"] == "backup"
    assert details["audit_id"].startswith("cfg-")
    backup_path = Path(details["backup_path"])
    assert backup_path == source.with_name(f"config.yaml.bak.{details['audit_id']}")
    assert backup_path.read_bytes() == b"partial backup"
    assert details["backup_write_applied"] is True
    assert details["transaction_write_applied"] is False
    assert details["write_applied"] is True
    assert not Path(details["transaction_dir"]).exists()
    assert source.read_bytes() == source_before
    assert not (tmp_path / "config.us.json").exists()


def test_backup_failure_with_unavailable_path_observation_reports_unknown_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    source = tmp_path / "config.yaml"
    _write_yaml(source, _config_doc())
    source_sha = config_source_sha256(source)
    original_path_exists = transaction_module._path_exists

    def _fail_backup(_source: Path, *, audit_id: str) -> Path:
        raise OSError(f"injected backup failure for {audit_id}")

    def _unknown_backup_path(path: Path | None) -> bool | None:
        if path is not None and ".bak." in path.name:
            return None
        return original_path_exists(path)

    monkeypatch.setattr(transaction_module, "_create_human_backup", _fail_backup)
    monkeypatch.setattr(transaction_module, "_path_exists", _unknown_backup_path)

    with pytest.raises(AgentToolError) as exc:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=source,
            config_doc=_config_doc(),
            runtime_root=tmp_path,
            markets=["us"],
            include_assistant=False,
            apply=True,
            expected_source_sha256=source_sha,
        )

    assert exc.value.code == "CONFIG_WRITE_FAILED"
    assert exc.value.details["stage"] == "backup"
    assert exc.value.details["backup_write_applied"] is None
    assert exc.value.details["transaction_write_applied"] is False
    assert exc.value.details["write_applied"] is None
    assert "injected backup failure" in exc.value.details["error"]
