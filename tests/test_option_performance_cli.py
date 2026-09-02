from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.interfaces.cli.main import parse_args
from src.interfaces.cli import option_performance


def test_report_cli_uses_same_request_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _report(payload, **_kwargs):
        captured.update(payload)
        return {"period": {}, "scope": {}, "quality": {}}, [], {}

    monkeypatch.setattr(option_performance, "option_performance_report_tool", _report)
    args = parse_args(
        [
            "option-performance",
            "report",
            "--period",
            "ytd",
            "--as-of-date",
            "2026-09-02",
            "--account",
            "LX",
            "--include-rows",
        ]
    )

    result = option_performance.handle_option_performance_command(args)

    assert "schema_version" not in result
    assert captured == {
        "config_key": "us",
        "config_path": None,
        "data_config": None,
        "account": "LX",
        "broker": None,
        "period": "ytd",
        "as_of_date": "2026-09-02",
        "include_rows": True,
    }


def test_report_cli_normalizes_config_failure_to_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        option_performance,
        "load_runtime_config",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private path")),
    )
    args = parse_args(["option-performance", "report", "--period", "mtd"])

    with pytest.raises(AgentToolError) as caught:
        option_performance.handle_option_performance_command(args)

    assert caught.value.code == "READ_ERROR"
    assert caught.value.details == {"reason_codes": ["ledger_read_failed"]}
    assert "private path" not in caught.value.message


@pytest.mark.parametrize(
    "flags",
    [
        ["--period", "month"],
        ["--month", "2026-09"],
        ["--year", "2026"],
        ["--start-date", "2026-09-01"],
        ["--end-date", "2026-09-02"],
        ["--refresh-quotes"],
        ["--no-refresh-quotes"],
    ],
)
def test_report_cli_rejects_removed_period_and_quote_flags(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["option-performance", "report", *flags])


def test_evidence_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "option-performance",
                "evidence",
                "import",
                "--file",
                "facts.json",
                "--dry-run",
                "--apply",
            ]
        )


def test_evidence_import_does_not_advertise_unapplied_scope_filters() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "option-performance",
                "evidence",
                "import",
                "--file",
                "facts.json",
                "--account",
                "lx",
            ]
        )


class _ImportResult:
    def __init__(self, applied: bool):
        self.applied = applied

    def to_dict(self):
        return {"applied": self.applied, "inserted_count": int(self.applied), "envelope": {}}


class _EvidenceRepo:
    def __init__(self):
        self.calls = []

    def import_envelope(self, value, *, apply, migrated_at_ms):
        self.calls.append((value, apply, migrated_at_ms))
        return _ImportResult(apply)


def _patch_import_dependencies(monkeypatch: pytest.MonkeyPatch, repo: _EvidenceRepo, tmp_path: Path) -> None:
    monkeypatch.setattr(
        option_performance,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        option_performance,
        "resolve_public_data_config_path",
        lambda _payload, _portfolio: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        option_performance,
        "open_position_ledger_from_data_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )
    monkeypatch.setattr(option_performance, "open_performance_evidence_repository", lambda _ledger: repo)
    monkeypatch.setattr(option_performance, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(option_performance, "mask_path", lambda value: str(value))


def test_evidence_import_defaults_to_dry_run_and_apply_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"schema_version": "option_performance_evidence.v1", "valuation_marks": [], "fx_rates": []}))
    repo = _EvidenceRepo()
    _patch_import_dependencies(monkeypatch, repo, tmp_path)

    dry_args = parse_args(["option-performance", "evidence", "import", "--file", str(facts)])
    dry = option_performance.handle_option_performance_command(dry_args)
    apply_args = parse_args(
        ["option-performance", "evidence", "import", "--file", str(facts), "--apply"]
    )
    applied = option_performance.handle_option_performance_command(apply_args)

    assert dry["dry_run"] is True
    assert applied["dry_run"] is False
    assert [call[1] for call in repo.calls] == [False, True]


def test_evidence_capture_defaults_to_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _capture(payload, *, apply, **_kwargs):
        captured.update(payload)
        captured["apply"] = apply
        return {"schema_version": "option_performance_evidence_capture.output.v1", "dry_run": not apply}, [], {}

    monkeypatch.setattr(option_performance, "capture_option_performance_evidence", _capture)
    args = parse_args(
        ["option-performance", "evidence", "capture", "--config-key", "us", "--account", "lx"]
    )

    data = option_performance.handle_option_performance_command(args)

    assert data["dry_run"] is True
    assert captured["apply"] is False
    assert captured["account"] == "lx"


def test_cash_conversion_backfill_defaults_to_dry_run_and_preserves_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _backfill(args):
        captured.update(vars(args))
        return {
            "schema_version": "option_performance_cash_conversion_backfill.output.v1",
            "dry_run": not args.apply,
        }

    monkeypatch.setattr(option_performance, "_backfill_cash_conversion", _backfill)
    args = parse_args(
        [
            "option-performance",
            "cash-conversion",
            "backfill",
            "--account",
            "lx",
            "--start-date",
            "2026-04-01",
            "--end-date",
            "2026-07-24",
        ]
    )

    result = option_performance.handle_option_performance_command(args)

    assert result["dry_run"] is True
    assert captured["apply"] is False
    assert captured["account"] == "lx"
    assert captured["start_date"] == "2026-04-01"
    assert captured["end_date"] == "2026-07-24"


def test_cash_conversion_backfill_apply_and_dry_run_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "option-performance",
                "cash-conversion",
                "backfill",
                "--dry-run",
                "--apply",
            ]
        )


def test_cash_conversion_correction_defaults_to_dry_run_and_preserves_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _correct(args):
        captured.update(vars(args))
        return {
            "schema_version": "option_performance_cash_conversion_correction.output.v1",
            "dry_run": True,
        }

    monkeypatch.setattr(option_performance, "_correct_cash_conversion", _correct)
    args = parse_args(
        [
            "option-performance",
            "cash-conversion",
            "correct",
            "--account",
            "lx",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-08-26",
        ]
    )

    result = option_performance.handle_option_performance_command(args)

    assert result["dry_run"] is True
    assert captured["apply"] is False
    assert captured["account"] == "lx"


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--apply"], "use --confirm or --yes"),
        (["--confirm"], "require --apply together"),
        (["--dry-run", "--apply", "--confirm"], "cannot be combined"),
    ],
)
def test_cash_conversion_correction_requires_explicit_apply_and_confirmation(
    flags: list[str],
    message: str,
) -> None:
    args = parse_args(
        ["option-performance", "cash-conversion", "correct", *flags]
    )
    with pytest.raises(SystemExit, match=message):
        option_performance.handle_option_performance_command(args)


def test_cash_conversion_correction_confirmed_apply_uses_guard_and_returns_audit_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = {}

    class _Result:
        def to_dict(self):
            return {
                "applied": True,
                "batch_id": "cashfxcorr_test",
                "preview_conversion_count": 2,
                "migrated_conversion_count": 2,
                "unresolved": [],
                "changes": [],
            }

    monkeypatch.setattr(
        option_performance,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        option_performance,
        "resolve_public_data_config_path",
        lambda _payload, _portfolio: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        option_performance,
        "guard_ledger_write",
        lambda **kwargs: calls.setdefault("guard", kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        option_performance,
        "open_position_ledger_from_data_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )
    monkeypatch.setattr(
        option_performance,
        "open_performance_evidence_repository",
        lambda _repo: object(),
    )

    def _correct(*_args, **kwargs):
        calls["correct"] = kwargs
        return _Result()

    monkeypatch.setattr(option_performance, "correct_superseded_cash_conversions", _correct)
    args = parse_args(
        [
            "option-performance",
            "cash-conversion",
            "correct",
            "--account",
            "lx",
            "--apply",
            "--confirm",
        ]
    )

    result = option_performance.handle_option_performance_command(args)

    assert calls["guard"]["data_config"] == tmp_path / "portfolio.runtime.json"
    assert calls["correct"]["apply"] is True
    assert result["dry_run"] is False
    assert result["corrected_conversion_count"] == 2
    assert result["audit_id"] == "cashfxcorr_test"
