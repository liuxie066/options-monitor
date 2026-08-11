from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest


BASE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_secret_backend(monkeypatch: pytest.MonkeyPatch):
    """Keep legacy env-oriented tests away from real OS credential stores.

    New secret-subsystem tests inject ``InMemorySecretProvider`` directly. The
    existing suite remains an explicit compatibility-backend test until each
    caller has its own injected provider fixture.
    """

    from src.application.secret_store import reset_default_secret_provider

    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    reset_default_secret_provider()
    yield
    reset_default_secret_provider()


def phase2_opening_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add the normalized Phase-1 evidence required by formal candidate scans."""

    out = dict(row)
    symbol = str(out.get("symbol") or "NVDA").strip().upper()
    market = str(out.get("market") or ("HK" if symbol.endswith(".HK") else "US")).upper()
    if market == "HK":
        digits = symbol.removesuffix(".HK")
        owner = f"HK.{digits.zfill(5)}"
    else:
        owner = f"US.{symbol}"
    multiplier = out.get("multiplier")
    out.setdefault("market", market)
    out.setdefault("quote_update_time", "2026-04-01 10:59:00")
    out.setdefault("quote_observed_at_utc", "2026-04-01T14:59:00Z")
    out.setdefault("quote_age_seconds", 60)
    out.setdefault("snapshot_received_at_utc", "2026-04-01T14:59:00Z")
    out.setdefault("spot_update_time", "2026-04-01 10:59:00")
    out.setdefault("spot_observed_at_utc", "2026-04-01T14:59:00Z")
    out.setdefault("spot_age_seconds", 60)
    out.setdefault("market_state", "MORNING")
    out.setdefault("underlier_observation_status", "ready")
    out.setdefault("underlier_observation_reason_code", None)
    out.setdefault("price_tick", 0.01)
    out.setdefault("term_matched_rv", out.get("realized_volatility_estimate", 0.20))
    out.setdefault("term_matched_rv_status", "ok")
    out.setdefault("term_matched_rv_reason", None)
    out.setdefault("term_matched_rv_input_hash", "b" * 64)
    out.setdefault("option_standard_type", "STANDARD")
    out.setdefault("stock_owner", owner)
    out.setdefault("stock_type", "DRVT")
    out.setdefault("option_sec_status", "NORMAL")
    out.setdefault("option_suspension", False)
    out.setdefault("chain_multiplier", multiplier)
    out.setdefault("snapshot_multiplier", multiplier)
    out.setdefault("opening_contract_status", "ready")
    out.setdefault("opening_contract_reason_codes", "")
    return out


@pytest.fixture
def example_config_path(tmp_path: Path) -> Path:
    from src.application.config_yaml import build_yaml_runtime_config_file

    cfg_path = (tmp_path / "config.us.json").resolve()
    build_yaml_runtime_config_file(
        repo_root=BASE,
        market="us",
        config_path=BASE / "configs" / "examples" / "config.yaml.example",
        output_config_path=cfg_path,
    )
    return cfg_path


@pytest.fixture
def runtime_config_copy(tmp_path, example_config_path: Path) -> Path:
    cfg_path = (tmp_path / "config.us.json").resolve()
    cfg_path.write_text(example_config_path.read_text(encoding="utf-8"), encoding="utf-8")
    return cfg_path


@pytest.fixture
def argv_scope(monkeypatch) -> Callable[[list[str]], None]:
    def _apply(argv: list[str]) -> None:
        monkeypatch.setattr(sys, "argv", list(argv))

    return _apply


class FakeRunLogger:
    def __init__(self, _base: Path):
        self.run_id = "test-run"
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        rec = {"step": step, "status": status}
        rec.update(kwargs)
        self.events.append(rec)


@pytest.fixture
def fake_runlog_factory() -> Callable[[list[dict[str, Any]] | None], FakeRunLogger]:
    def _factory(shared_events: list[dict[str, Any]] | None = None) -> FakeRunLogger:
        logger = FakeRunLogger(BASE)
        if shared_events is not None:
            def _safe_event(step: str, status: str, **kwargs) -> None:
                rec = {"step": step, "status": status}
                rec.update(kwargs)
                shared_events.append(rec)

            logger.safe_event = _safe_event  # type: ignore[assignment]
        return logger

    return _factory
