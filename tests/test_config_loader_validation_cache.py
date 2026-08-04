"""Regression: scheduled-mode config validation should be cached by hash."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json


def test_scheduled_validation_is_cached() -> None:
    from src.application.config_loader import load_config

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


def test_config_payload_is_consumed_without_reopening_mutable_path(
    tmp_path: Path,
) -> None:
    from src.application.config_loader import load_config

    cfg_path = tmp_path / "config.override.json"
    cfg_path.write_text('{"runtime":{"marker":"mutable-path"}}', encoding="utf-8")
    validated_payload = {
        "portfolio": {"account": "lx"},
        "runtime": {"marker": "validated-payload"},
        "symbols": [],
    }

    loaded = load_config(
        base=tmp_path,
        config_path=cfg_path,
        config_payload=validated_payload,
        is_scheduled=False,
        log=lambda _message: None,
        validate_config_fn=lambda _cfg: None,
    )

    assert loaded["runtime"]["marker"] == "validated-payload"


def test_scheduled_validation_failure_is_not_cached() -> None:
    from src.application.config_loader import load_config

    calls: list[int] = []

    def _validate(cfg: dict) -> None:
        calls.append(1)
        raise RuntimeError("bad config")

    with TemporaryDirectory() as td:
        base = Path(td)
        state_dir = base / 'state'
        cfg_path = base / 'cfg.json'
        cfg_path.write_text('{"symbols": [{"symbol": "0700.HK"}] }', encoding='utf-8')

        def _log(_: str) -> None:
            return

        for _ in range(2):
            try:
                load_config(base=base, config_path=cfg_path, is_scheduled=True, log=_log, validate_config_fn=_validate, state_dir=state_dir)
            except SystemExit as exc:
                assert "validation failed" in str(exc)
            else:
                raise AssertionError("expected validation failure")

        cache_path = state_dir / 'config_validation_cache.json'
        assert not cache_path.exists()

    assert len(calls) == 2


def test_scheduled_validation_rechecks_same_hash_when_validator_version_changes() -> None:
    from src.application.config_loader import SCHEDULED_CONFIG_VALIDATOR_VERSION, load_config

    calls: list[int] = []
    with TemporaryDirectory() as td:
        base = Path(td)
        state_dir = base / 'state'
        state_dir.mkdir()
        cfg_path = base / 'cfg.json'
        cfg = {"symbols": [{"symbol": "0700.HK"}], "notifications": {"render_style": "compact"}}
        cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
        payload = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
        cfg_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        (state_dir / 'config_validation_cache.json').write_text(
            json.dumps({"sha256": cfg_hash, "validator_version": "v1"}),
            encoding='utf-8',
        )

        load_config(
            base=base,
            config_path=cfg_path,
            is_scheduled=True,
            log=lambda _message: None,
            validate_config_fn=lambda _cfg: calls.append(1),
            state_dir=state_dir,
        )

        cache = json.loads((state_dir / 'config_validation_cache.json').read_text(encoding='utf-8'))

    assert calls == [1]
    assert cache["validator_version"] == SCHEDULED_CONFIG_VALIDATOR_VERSION


def test_resolve_data_config_path_prefers_explicit_path() -> None:
    from src.application.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        explicit = base / "custom.json"
        explicit.write_text("{}", encoding="utf-8")

        out = resolve_data_config_path(base=base, data_config="custom.json")

    assert out == explicit.resolve()


def test_default_data_config_path_prefers_runtime_config_location_when_present() -> None:
    from src.application.config_loader import default_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        data_config = base / "portfolio.runtime.json"
        data_config.write_text("{}", encoding="utf-8")

        out = default_data_config_path(base=base)

    assert out == data_config.resolve()


def test_default_data_config_path_falls_back_to_runtime_config_location_when_missing() -> None:
    from src.application.config_loader import default_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        out = default_data_config_path(base=base)

    assert out == (base / "portfolio.runtime.json").resolve()


def test_data_config_candidates_use_runtime_location_only() -> None:
    from src.application.config_loader import data_config_candidates

    with TemporaryDirectory() as td:
        base = Path(td)
        out = data_config_candidates(base=base)

    assert out == [(base / "portfolio.runtime.json").resolve()]


def test_resolve_data_config_path_prefers_env_override(monkeypatch) -> None:
    from src.application.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        env_path = base / "external" / "portfolio.feishu.json"
        monkeypatch.setenv("OM_DATA_CONFIG", str(env_path))

        out = resolve_data_config_path(base=base, data_config=None)

    assert out == env_path.resolve()


def test_resolve_data_config_path_ignores_legacy_om_pm_config(monkeypatch) -> None:
    from src.application.config_loader import resolve_data_config_path

    with TemporaryDirectory() as td:
        base = Path(td)
        legacy_env_path = base / "external" / "legacy-portfolio.json"
        monkeypatch.delenv("OM_DATA_CONFIG", raising=False)
        monkeypatch.setenv("OM_PM_CONFIG", str(legacy_env_path))

        out = resolve_data_config_path(base=base, data_config=None)

    assert out == (base / "portfolio.runtime.json").resolve()


def test_resolve_watchlist_and_templates_config_require_canonical_keys() -> None:
    from src.application.config_sections import resolve_templates_config, resolve_watchlist_config

    cfg = {
        "symbols": [{"symbol": "0700.HK"}, {"symbol": "3690.HK"}],
        "templates": {"put_base": {"sell_put": {"min_net_income": 100}}},
    }

    assert [it["symbol"] for it in resolve_watchlist_config(cfg)] == ["0700.HK", "3690.HK"]
    assert resolve_templates_config(cfg) == {"put_base": {"sell_put": {"min_net_income": 100}}}


def test_resolve_watchlist_config_canonicalizes_legacy_market_to_broker() -> None:
    from src.application.config_sections import resolve_watchlist_config

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
    from src.application.config_loader import normalize_portfolio_broker_config

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
    from src.application.config_sections import set_watchlist_config

    cfg = {}
    out = set_watchlist_config(cfg, [{"symbol": "0700.HK"}])

    assert out["symbols"] == [{"symbol": "0700.HK"}]


def test_set_watchlist_config_writes_broker_only() -> None:
    from src.application.config_sections import set_watchlist_config

    cfg = {}
    out = set_watchlist_config(cfg, [{"symbol": "0700.HK", "market": "HK"}])

    assert out["symbols"] == [{"symbol": "0700.HK", "broker": "HK"}]
