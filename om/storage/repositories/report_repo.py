from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.io_utils import atomic_write_json as write_json


def prepare_dirs(base: Path, report_dir: str | None, state_dir: str | None) -> tuple[Path, Path]:
    rd = (Path(report_dir).resolve() if report_dir else (base / "output" / "reports").resolve())
    sd = (Path(state_dir).resolve() if state_dir else (base / "output" / "state").resolve())
    rd.mkdir(parents=True, exist_ok=True)
    sd.mkdir(parents=True, exist_ok=True)
    return rd, sd


def ensure_report_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_state_json(state_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (state_dir / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, payload)
    return out


def write_state_text(state_dir: Path, name: str, text: str) -> Path:
    out = (state_dir / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(str(text), encoding="utf-8")
    return out


def write_state_json_text(state_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (state_dir / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out

