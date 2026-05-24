from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.strategy_lab.dataset_contracts import StrategyLabDataset


EXPERIMENT_CURRENT_SCHEMA_VERSION = "strategy_lab_current.v1"


class StrategyLabStorage:
    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.root = self.runtime_root / "output_shared" / "strategy_lab"
        self.dataset_dir = self.root / "datasets"
        self.experiment_dir = self.root / "experiments"
        self.report_dir = self.root / "reports"
        self.current_dir = self.runtime_root / "output_shared" / "state" / "current"

    def dataset_path(self, dataset_id: str) -> Path:
        return self._safe_child(self.dataset_dir, f"{_safe_stem(dataset_id)}.json")

    def experiment_path(self, experiment_id: str) -> Path:
        return self._safe_child(self.experiment_dir, f"{_safe_stem(experiment_id)}.json")

    def report_path(self, experiment_id: str) -> Path:
        return self._safe_child(self.report_dir, f"{_safe_stem(experiment_id)}.md")

    def current_path(self) -> Path:
        return self._safe_child(self.current_dir, "strategy_lab.current.json")

    def write_dataset(self, dataset: StrategyLabDataset) -> Path:
        path = self.dataset_path(dataset.dataset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, dataset.to_dict())
        return path

    def read_dataset(self, dataset_id: str) -> StrategyLabDataset:
        path = self.dataset_path(dataset_id)
        if not path.exists():
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"strategy lab dataset not found: {dataset_id}",
                hint="Run `om strategy-lab dataset collect --confirm` first, or pass an existing dataset_id.",
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return StrategyLabDataset.from_dict(payload)

    def write_experiment(self, result: dict[str, Any], *, report_markdown: str) -> dict[str, str]:
        experiment_id = str(result.get("experiment_id") or "strategy_lab_experiment")
        result_path = self.experiment_path(experiment_id)
        report_path = self.report_path(experiment_id)
        current_path = self.current_path()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(result_path, result)
        report_path.write_text(report_markdown, encoding="utf-8")
        _write_json(
            current_path,
            {
                "schema_version": EXPERIMENT_CURRENT_SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "experiment_id": experiment_id,
                "dataset_id": result.get("dataset_id"),
                "status": result.get("status"),
                "recommendation": (result.get("recommendation") or {}).get("recommendation"),
                "result_path": self.relative(result_path),
                "report_path": self.relative(report_path),
            },
        )
        return {
            "result_path": self.relative(result_path),
            "report_path": self.relative(report_path),
            "current_path": self.relative(current_path),
        }

    def read_current(self) -> dict[str, Any]:
        path = self.current_path()
        if not path.exists():
            return {
                "schema_version": EXPERIMENT_CURRENT_SCHEMA_VERSION,
                "exists": False,
                "current_path": self.relative(path),
            }
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload["exists"] = True
            return payload
        return {"schema_version": EXPERIMENT_CURRENT_SCHEMA_VERSION, "exists": False, "current_path": self.relative(path)}

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.runtime_root).as_posix()
        except ValueError:
            return f".../{path.name}"

    def _safe_child(self, directory: Path, name: str) -> Path:
        path = (directory / name).resolve()
        try:
            path.relative_to(self.runtime_root)
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message="strategy lab paths must stay under runtime root") from exc
        return path


def _safe_stem(value: str) -> str:
    out = []
    for ch in str(value or "").strip():
        out.append(ch if ch.isalnum() or ch in {"_", "-", "."} else "_")
    return "".join(out).strip("._-") or "strategy_lab"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

