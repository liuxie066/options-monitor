from __future__ import annotations

from pathlib import Path


def test_scan_pipeline_defaults_runtime_outputs_to_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.application import pipeline_runtime
    from src.application import pipeline_watchlist

    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_load_config(**kwargs):
        captured["load_config_base"] = kwargs["base"]
        captured["load_config_path"] = kwargs["config_path"]
        return {
            "symbols": [
                {
                    "symbol": "MSFT",
                    "fetch": {"source": "opend"},
                    "sell_put": {"enabled": False},
                    "sell_call": {"enabled": False},
                }
            ],
            "portfolio": {"broker": "富途", "data_config": "portfolio.runtime.json"},
            "notifications": {"enabled": False},
        }

    def _fake_run_watchlist_pipeline_default(**kwargs):
        captured["pipeline_base"] = kwargs["base"]
        captured["report_dir"] = kwargs["report_dir"]
        captured["state_dir"] = kwargs["state_dir"]
        captured["required_data_dir"] = kwargs["required_data_dir"]
        return []

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(pipeline_runtime, "load_runtime_pipeline_config", _fake_load_config)
    monkeypatch.setattr(pipeline_watchlist, "run_watchlist_pipeline_default", _fake_run_watchlist_pipeline_default)

    rc = pipeline_runtime.main([
        "--config",
        str(config_path),
        "--stage",
        "fetch",
        "--no-context",
    ])

    repo_root = Path(__file__).resolve().parents[1]
    assert rc == 0
    assert captured["load_config_base"] == repo_root
    assert captured["load_config_path"] == config_path.resolve()
    assert captured["pipeline_base"] == runtime_root.resolve()
    assert captured["report_dir"] == (runtime_root / "output" / "reports").resolve()
    assert captured["state_dir"] == (runtime_root / "output" / "state").resolve()
    assert captured["required_data_dir"] == (runtime_root / "output").resolve()
