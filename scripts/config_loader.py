"""Config loader (JSON/YAML legacy) + validation gating.

Why:
- Keep run_pipeline orchestration-only (Stage 3).
- Centralize scheduled-mode validation caching (hash-based) to avoid repeated cost.

Design:
- No side effects beyond optional validation-cache file write (scheduled mode).
- Validation function is injectable for unit tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable


def pm_config_candidates(*, base: Path) -> list[Path]:
    base = Path(base).resolve()
    candidates = [
        (base / "secrets" / "portfolio.feishu.json").resolve(),
        Path("/opt/options-monitor/secrets/portfolio.feishu.json").resolve(),
        (base / "../portfolio-management/config.json").resolve(),
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for item in candidates:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def default_pm_config_path(*, base: Path) -> Path:
    candidates = pm_config_candidates(base=base)
    for item in candidates:
        if item.exists():
            return item
    return candidates[-1]


def resolve_pm_config_path(*, base: Path, pm_config: str | Path | None) -> Path:
    if pm_config is not None and str(pm_config).strip():
        path = Path(pm_config)
        if not path.is_absolute():
            path = (Path(base).resolve() / path).resolve()
        return path
    return default_pm_config_path(base=base)

def resolve_templates_config(cfg: dict | None) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    templates = data.get('templates')
    if isinstance(templates, dict):
        return templates
    return {}


def resolve_watchlist_config(cfg: dict | None) -> list[dict]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get('symbols')
    if isinstance(symbols, list):
        return [it for it in symbols if isinstance(it, dict)]
    return []


def set_watchlist_config(cfg: dict | None, items: list[dict]) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    normalized = [it for it in (items or []) if isinstance(it, dict)]
    data['symbols'] = normalized
    return data


def _should_validate_scheduled(*, cfg: dict, state_dir: Path) -> bool:
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_path = (state_dir / 'config_validation_cache.json').resolve()

    payload = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    sha256 = hashlib.sha256(payload.encode('utf-8')).hexdigest()

    prev = None
    try:
        if cache_path.exists() and cache_path.stat().st_size > 0:
            prev = json.loads(cache_path.read_text(encoding='utf-8')).get('sha256')
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        prev = None

    if prev == sha256:
        return False

    cache_path.write_text(
        json.dumps({'sha256': sha256}, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return True


def load_config(
    *,
    base: Path,
    config_path: Path,
    is_scheduled: bool,
    log: Callable[[str], None],
    validate_config_fn: Callable[[dict], None] | None = None,
    state_dir: Path | None = None,
) -> dict:
    cfg_path = config_path
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    if cfg_path.suffix.lower() == '.json':
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    else:
        # YAML is legacy but still supported.
        import yaml

        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise SystemExit('[CONFIG_ERROR] config must be a JSON/YAML object')

    try:
        if validate_config_fn is None:
            from scripts.validate_config import validate_config as validate_config_fn  # type: ignore

        should_validate = True
        if is_scheduled:
            sd = state_dir if state_dir is not None else (base / 'output' / 'state').resolve()
            should_validate = _should_validate_scheduled(cfg=cfg, state_dir=sd)

        if should_validate:
            validate_config_fn(cfg)
    except SystemExit:
        raise
    except ImportError as e:
        # Do not block the pipeline if validator module is not available.
        log(f"[WARN] config validation skipped (import failed): {e}")
    except Exception as e:
        # Validation logic itself raised — surface this as an error, don't swallow.
        log(f"[ERR] config validation failed: {e}")
        raise SystemExit(f"[CONFIG_ERROR] validation failed: {e}") from e

    return cfg
