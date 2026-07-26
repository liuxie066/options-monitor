from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class QualityArtifactRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def write_atomic(self, payload: dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        return self.path


__all__ = ["QualityArtifactRepository"]
