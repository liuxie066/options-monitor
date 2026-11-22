"""Publication and provenance contracts; all artifacts and failures are isolated."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.research import underlier_history as history
from src.interfaces.cli import research as cli


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("publication tests must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield
    assert not attempts


@pytest.mark.parametrize("module", [history, quote], ids=["history", "quote"])
def test_publication_syncs_private_bytes_before_link(tmp_path, monkeypatch, module):
    target = tmp_path / "original.bin"
    content = b"synthetic\x00\xff\n"
    calls = []
    real_sync, real_link = os.fsync, os.link

    def sync(fd):
        calls.append("sync")
        assert not target.exists()
        return real_sync(fd)

    def link(temporary, destination):
        calls.append("link")
        assert calls == ["sync", "link"]
        assert temporary.parent == target.parent
        assert temporary.read_bytes() == content
        assert temporary.stat().st_mode & 0o777 == 0o600
        assert not target.exists()
        return real_link(temporary, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        patch.setattr(os, "link", link)
        digest = module._write_once(target, content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("module", [history, quote], ids=["history", "quote"])
@pytest.mark.parametrize("existing", [b"same", b"different"])
def test_publication_rejects_existing_target_even_with_same_bytes(tmp_path, module, existing):
    target = tmp_path / "original.bin"
    target.write_bytes(existing)
    before = target.stat()
    with pytest.raises(FileExistsError):
        module._write_once(target, b"same")
    assert target.read_bytes() == existing
    assert target.stat().st_ino == before.st_ino
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("module", [history, quote], ids=["history", "quote"])
@pytest.mark.parametrize("stage", ["chmod", "write", "flush", "fsync", "link", "cancel"])
def test_publication_failure_preserves_exception_and_cleans_temp(tmp_path, monkeypatch, module, stage):
    target = tmp_path / "original.bin"
    failure = asyncio.CancelledError("isolated cancellation") if stage == "cancel" else OSError(stage)
    original_open = Path.open

    def fail(*args, **kwargs):
        raise failure

    class FailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, content):
            if stage == "write":
                self.stream.write(content[:1])
                raise failure
            return self.stream.write(content)

        def flush(self):
            raise failure

    def open_stream(path, *args, **kwargs):
        assert args == ("xb",)
        return FailingStream(original_open(path, *args, **kwargs))

    with monkeypatch.context() as patch:
        if stage == "chmod":
            patch.setattr(Path, "chmod", fail)
        elif stage in {"write", "flush"}:
            patch.setattr(Path, "open", open_stream)
        else:
            patch.setattr(os, "fsync" if stage == "cancel" else stage, fail)
        with pytest.raises(type(failure)) as caught:
            module._write_once(target, b"synthetic")
        assert caught.value is failure
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("module", [history, quote], ids=["history", "quote"])
def test_cleanup_failure_after_link_keeps_published_target(tmp_path, monkeypatch, module):
    target = tmp_path / "original.bin"
    failure = OSError("isolated cleanup failure")

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(OSError) as caught:
            module._write_once(target, b"synthetic")
        assert caught.value is failure
    assert target.read_bytes() == b"synthetic"
    temporary, = tmp_path.glob(".*.tmp.*")
    assert temporary.stat().st_ino == target.stat().st_ino


def test_wrappers_keep_their_own_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_sha", lambda _: "history-owned-digest")
    assert history._write_once(tmp_path / "history", b"data") == "history-owned-digest"
    assert quote._write_once(tmp_path / "quote", b"data") == hashlib.sha256(b"data").hexdigest()


def test_series_put_still_adopts_only_matching_private_file(tmp_path):
    target = tmp_path / "artifact"
    series._put(target, b"data")
    before = target.stat()
    series._put(target, b"data")
    assert target.stat().st_ino == before.st_ino
    with pytest.raises(ValueError, match="immutable archive differs"):
        series._put(target, b"changed")
    target.chmod(0o640)
    with pytest.raises(ValueError, match="immutable archive differs"):
        series._put(target, b"data")
    assert target.read_bytes() == b"data"


def test_publication_source_is_bound_by_existing_fingerprint_fields(tmp_path, monkeypatch):
    root = Path(quote.__file__).resolve().parents[3]
    publisher = Path(inspect.getsourcefile(history._publish_bytes_once)).resolve()
    assert publisher == Path(history.__file__).resolve()
    relpath = publisher.relative_to(root).as_posix()
    assert relpath in quote.IMPLEMENTATION_SOURCES
    assert relpath in series.IMPLEMENTATION_SOURCES
    for path in series.IMPLEMENTATION_SOURCES:
        dest = tmp_path / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((root / path).read_bytes())
    for module in [quote, series, history]:
        monkeypatch.setattr(module, "__file__", str(tmp_path / Path(module.__file__).resolve().relative_to(root)))

    def manifest():
        return history._manifest(plan={"remaining": []}, plan_hash="synthetic", host="fixture.invalid",
                                 port=11111, execution_error_type=None, files=[])

    before = manifest()
    assert set(before) == {
        "schema_version", "status", "plan_sha256", "execution_error_type", "collector_sha256",
        "provider", "endpoint", "provider_quota", "files", "remaining",
    }
    assert before["schema_version"] == "research.underlier-history-backfill.v1"
    source = tmp_path / relpath
    assert before["collector_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    quote_before, series_before = quote._implementation_sha256(), series._implementation()
    raw = source.read_bytes()
    assert raw.count(b"temporary.chmod(0o600)") == 1
    # Change only the private source copy. The mutation is never imported or executed.
    source.write_bytes(raw.replace(b"temporary.chmod(0o600)", b"temporary.chmod(0o640)"))
    after = manifest()
    assert set(after) == set(before)
    assert after["collector_sha256"] != before["collector_sha256"]
    assert after["collector_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert quote._implementation_sha256() != quote_before
    assert series._implementation() != series_before


@pytest.mark.parametrize("kind", ["history", "quote"])
@pytest.mark.parametrize("confirm", [False, True])
def test_cli_preview_and_initial_publication_failure_never_open_gateway(
    tmp_path, monkeypatch, capsys, kind, confirm,
):
    gateway_calls = []

    def forbidden_gateway(**kwargs):
        gateway_calls.append(kwargs)
        raise AssertionError("gateway must not be opened")

    if kind == "history":
        as_of = int(datetime(2026, 2, 5, tzinfo=timezone.utc).timestamp() * 1000)
        key = ContractKey.from_values(broker="futu", account="fixture", underlying_symbol="ACME",
                                      option_type="put", strike="90", expiration_ymd="2026-02-20")
        event = TradeEvent(event_id="fixture-open", event_type="open",
                           event_time_ms=int(datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp() * 1000),
                           contract_key=key, contracts=1, price=Decimal("1"), currency="USD",
                           source="synthetic", multiplier=100, raw_payload={"side": "sell"})
        events = tmp_path / "events.json"
        events.write_text(json.dumps({"schema_version": history.INPUT_SCHEMA, "captured_at_ms": as_of,
                                      "events": [event.to_dict()]}))
        plan = history.build_underlier_history_plan(events, account="fixture", broker="futu",
                                                    month="2026-01", as_of_ms=as_of)
        command, extras, module = "underlier-history-backfill", ["--events", str(events)], history
    else:
        plan = {"schema_version": quote.PLAN_SCHEMA, "scope_id": "synthetic",
                "observation_at_utc": datetime.now(timezone.utc).isoformat(),
                "option_codes": ["US.ACME261120P90000"]}
        command, extras, module = "option-quote-capture", [], quote
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    out = tmp_path / "output"
    args = ["research", command, "--plan", str(plan_path), "--output-dir", str(out),
            "--opend-host", "fixture.invalid", "--opend-port", "11111", *extras]
    monkeypatch.setattr(module, "build_ready_futu_quote_gateway", forbidden_gateway)
    if confirm:
        def fail_sync(fd):
            raise OSError("isolated fsync")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_sync)
            with pytest.raises(OSError, match="isolated fsync"):
                cli.main([*args, "--confirm"])
        assert out.is_dir()
        assert not (out / "plan.json").exists()
        assert not list(out.rglob(".*.tmp.*"))
    else:
        assert cli.main(args) == 0
        response = json.loads(capsys.readouterr().out)
        assert response["ok"] and response["data"]["status"] == "preview"
        assert response["data"]["provider_called"] is False
        assert not out.exists()
    assert not gateway_calls
