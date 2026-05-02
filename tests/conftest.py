from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


BASE = Path(__file__).resolve().parents[1]


@pytest.fixture
def example_config_path() -> Path:
    return (BASE / "configs" / "examples" / "config.example.us.json").resolve()


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


@pytest.fixture
def send_if_needed_module():
    return importlib.import_module("scripts.send_if_needed")


@pytest.fixture
def send_if_needed_common_patches(monkeypatch, send_if_needed_module):
    mod = send_if_needed_module
    calls: list[list[str]] = []

    monkeypatch.setattr(mod, "run_tick", lambda argv: calls.append(list(argv)) or 0)
    return {"module": mod, "calls": calls}
