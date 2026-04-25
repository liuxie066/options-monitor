"""Regression: scheduled-mode config validation should be cached by hash."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory


def test_scheduled_validation_is_cached() -> None:
    from scripts.config_loader import load_config

    calls: list[int] = []

    def _validate(cfg: dict) -> None:
        calls.append(1)

    with TemporaryDirectory() as td:
        base = Path(td)
        state_dir = base / 'state'
        cfg_path = base / 'cfg.json'
        cfg_path.write_text('{"symbols": [{"symbol": "0700.HK"}] }', encoding='utf-8')

        def _log(_: str) -> None:
            return

        load_config(base=base, config_path=cfg_path, is_scheduled=True, log=_log, validate_config_fn=_validate, state_dir=state_dir)
        load_config(base=base, config_path=cfg_path, is_scheduled=True, log=_log, validate_config_fn=_validate, state_dir=state_dir)

    assert len(calls) == 1


def test_resolve_data_config_path_prefers_explicit_path() -> None:
    from scripts.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        explicit = base / "custom.json"
        explicit.write_text("{}", encoding="utf-8")

        out = resolve_data_config_path(base=base, data_config="custom.json")

    assert out == explicit.resolve()


def test_default_data_config_path_prefers_new_secret_location_when_present() -> None:
    from scripts.config_loader import default_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        secret = base / "secrets" / "portfolio.sqlite.json"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("{}", encoding="utf-8")

        out = default_data_config_path(base=base)

    assert out == secret.resolve()


def test_default_data_config_path_falls_back_to_legacy_location_when_missing() -> None:
    from scripts.config_loader import default_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        out = default_data_config_path(base=base)

    assert out == (base / "secrets" / "portfolio.sqlite.json").resolve()


def test_resolve_data_config_path_prefers_env_override(monkeypatch) -> None:
    from scripts.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        env_path = base / "external" / "portfolio.feishu.json"
        monkeypatch.setenv("OM_DATA_CONFIG", str(env_path))

        out = resolve_data_config_path(base=base, data_config=None)

    assert out == env_path.resolve()


def test_resolve_data_config_path_ignores_legacy_om_pm_config(monkeypatch) -> None:
    from scripts.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        legacy_env_path = base / "external" / "legacy-portfolio.json"
        monkeypatch.delenv("OM_DATA_CONFIG", raising=False)
        monkeypatch.setenv("OM_PM_CONFIG", str(legacy_env_path))

        out = resolve_data_config_path(base=base, data_config=None)

    assert out == (base / "secrets" / "portfolio.sqlite.json").resolve()


def test_resolve_watchlist_and_templates_config_require_canonical_keys() -> None:
    from scripts.config_loader import resolve_templates_config, resolve_watchlist_config

    cfg = {
        "symbols": [{"symbol": "0700.HK"}, {"symbol": "3690.HK"}],
        "templates": {"put_base": {"sell_put": {"min_net_income": 100}}},
    }

    assert [it["symbol"] for it in resolve_watchlist_config(cfg)] == ["0700.HK", "3690.HK"]
    assert resolve_templates_config(cfg) == {"put_base": {"sell_put": {"min_net_income": 100}}}


def test_resolve_watchlist_config_canonicalizes_legacy_market_to_broker() -> None:
    from scripts.config_loader import resolve_watchlist_config

    cfg = {
        "symbols": [
            {"symbol": "0700.HK", "market": "HK"},
            {"symbol": "NVDA", "broker": "US"},
        ]
    }

    rows = resolve_watchlist_config(cfg)

    assert rows == [
        {"symbol": "0700.HK", "broker": "HK"},
        {"symbol": "NVDA", "broker": "US"},
    ]


def test_normalize_portfolio_broker_config_converts_legacy_fields_to_canonical() -> None:
    from scripts.config_loader import normalize_portfolio_broker_config

    out = normalize_portfolio_broker_config({"portfolio": {"broker": "富途", "data_config": "x.json", "account": "lx"}})

    assert out["portfolio"]["broker"] == "富途"
    assert out["portfolio"]["data_config"] == "x.json"
    assert "market" not in out["portfolio"]
    assert "pm_config" not in out["portfolio"]

    out_legacy = normalize_portfolio_broker_config({"portfolio": {"market": "富途", "account": "lx"}})
    assert out_legacy["portfolio"]["broker"] == "富途"
    assert "market" not in out_legacy["portfolio"]

    out_no_data = normalize_portfolio_broker_config({"portfolio": {"account": "lx"}})
    assert "data_config" not in out_no_data["portfolio"]


def test_set_watchlist_config_updates_symbols_only() -> None:
    from scripts.config_loader import set_watchlist_config

    cfg = {}
    out = set_watchlist_config(cfg, [{"symbol": "0700.HK"}])

    assert out["symbols"] == [{"symbol": "0700.HK"}]


def test_set_watchlist_config_writes_broker_only() -> None:
    from scripts.config_loader import set_watchlist_config

    cfg = {}
    out = set_watchlist_config(cfg, [{"symbol": "0700.HK", "market": "HK"}])

    assert out["symbols"] == [{"symbol": "0700.HK", "broker": "HK"}]
