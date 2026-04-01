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
