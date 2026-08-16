from __future__ import annotations

from argparse import Namespace

import src.interfaces.quality.cli as quality_cli
from src.interfaces.cli.main import parse_args


def test_quality_cli_exposes_integrity_and_preview_first_cutover(monkeypatch) -> None:
    assert parse_args(["quality", "integrity-status"]).quality_command == (
        "integrity-status"
    )
    assert parse_args(["quality", "integrity", "--no-deep"]).quality_command == (
        "integrity"
    )
    cutover = parse_args(
        ["quality", "cutover", "--evidence", "evidence.json"]
    )
    assert cutover.apply is False

    monkeypatch.setattr(quality_cli, "OMQualityService", lambda: object())
    monkeypatch.setattr(
        quality_cli,
        "quality_hot_path_cutover_preview",
        lambda path: {"status": "eligible", "path": path},
    )
    assert quality_cli.handle_quality_command(
        Namespace(
            quality_command="cutover",
            evidence="evidence.json",
            apply=False,
        )
    ) == {"status": "eligible", "path": "evidence.json"}
