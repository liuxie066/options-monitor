from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_runtime_config_sync_notifications_apply_and_check() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.sync_runtime_configs import compute_sync_plan, apply_sync_plan

    with TemporaryDirectory() as td:
        root = Path(td)

        us = {"notifications": {"channel": "feishu", "target": "user:U_US"}, "schedule": {"enabled": True}}
        hk = {"notifications": {"channel": "weixin", "target": "chat:C_HK"}, "schedule": {"enabled": True}}
        _write_json(root / "config.us.json", us)
        _write_json(root / "config.hk.json", hk)

        # Existing derived file with stale notifications.
        _write_json(
            root / "config.market_us.json",
            {
                "notifications": {"channel": "old", "target": "old"},
                "runtime": {"keep": True},
            },
        )

        # Templates used when derived files are missing.
        for name in (
            "config.scheduled.example.json",
            "config.market_hk.example.json",
            "config.market_us.fallback_yahoo.example.json",
            "config.legacy.example.json",
        ):
            _write_json(root / name, {"notifications": {"channel": "template", "target": "template"}, "seed": name})

        # Pre-apply: should detect drift.
        plan = compute_sync_plan(base_dir=root)
        changed = {str(x["target"]) for x in plan if bool(x["changed"])}
        assert "config.market_us.json" in changed
        assert "config.market_hk.json" in changed  # missing, will be created from template
        assert "config.json" in changed

        updated = apply_sync_plan(base_dir=root, plans=plan)
        assert updated >= 1

        # Post-apply: no drift.
        plan2 = compute_sync_plan(base_dir=root)
        assert all(not bool(x["changed"]) for x in plan2)

        us_market = json.loads((root / "config.market_us.json").read_text(encoding="utf-8"))
        hk_market = json.loads((root / "config.market_hk.json").read_text(encoding="utf-8"))
        scheduled = json.loads((root / "config.scheduled.json").read_text(encoding="utf-8"))

        assert us_market["notifications"] == us["notifications"]
        assert hk_market["notifications"] == hk["notifications"]
        assert scheduled["notifications"] == us["notifications"]

