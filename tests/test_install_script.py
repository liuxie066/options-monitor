from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_install_script_is_shell_parseable_and_has_no_service_side_effects() -> None:
    script = ROOT / "scripts" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)

    text = script.read_text(encoding="utf-8")
    assert "--version is required" in text
    assert "xcode-select --install" in text
    assert "python3-venv" in text
    assert "systemctl enable" not in text
    assert "launchctl bootstrap" not in text
    assert "OM_FEISHU_BOT_APP_SECRET" not in text
