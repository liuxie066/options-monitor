import argparse
import json
from pathlib import Path

from src.interfaces.cli.service_ops import add_service_update_commands, handle_service_update_command


def test_drift_explicit_output_has_details_and_private_mode(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    add_service_update_commands(parser.add_subparsers(dest="command"))
    output = tmp_path / "drift.json"
    args = parser.parse_args(["service", "drift", "--runtime-root", str(tmp_path), "--output", str(output)])
    response = handle_service_update_command(args, service_drift_fn=lambda **kwargs: {
        "summary": {"ok": False, "error_count": 1}, "details": [{"unit": "tick-hk", "reason": "missing"}],
    })
    assert response["ok"] is False
    assert json.loads(output.read_text())["details"][0]["reason"] == "missing"
    assert output.stat().st_mode & 0o777 == 0o600
