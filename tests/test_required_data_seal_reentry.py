from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.application.required_data_snapshot import (
    RequiredDataSnapshotError,
    seal_required_data_snapshot,
)
from tests.test_required_data_snapshot import (
    _publish_empty_quote,
    _summary,
    _workspace,
)


_SEALED_AT = datetime.now(timezone.utc)


def _empty_summary(*symbols: str) -> dict:
    return _summary(
        *symbols,
        outcomes={symbol: "success_empty" for symbol in symbols},
    )


def _seal(
    *,
    root: Path,
    manifest_path: Path,
    summary: dict,
    sealed_at: datetime = _SEALED_AT,
    close_plan_path: Path | None = None,
) -> dict:
    return seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=summary,
        close_advice_required_data_plan_path=close_plan_path,
        sealed_at=sealed_at,
    )


def test_identical_reseal_adopts_exact_manifest_bytes_and_hash(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_empty_quote(root, run_id="run-1")
    summary = _empty_summary("3690.HK")

    first = _seal(
        root=root,
        manifest_path=manifest_path,
        summary=summary,
    )
    original_bytes = manifest_path.read_bytes()

    second = _seal(
        root=root,
        manifest_path=manifest_path,
        summary=summary,
        sealed_at=_SEALED_AT + timedelta(hours=2),
    )

    assert second == first
    assert second["content_sha256"] == first["content_sha256"]
    assert second["sealed_at_utc"] == first["sealed_at_utc"]
    assert manifest_path.read_bytes() == original_bytes


def test_conflicting_plan_and_status_cannot_replace_terminal_manifest(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_empty_quote(root, run_id="run-1")
    first = _seal(
        root=root,
        manifest_path=manifest_path,
        summary=_empty_summary("3690.HK"),
    )
    original_bytes = manifest_path.read_bytes()

    with pytest.raises(
        RequiredDataSnapshotError,
        match="terminal required-data snapshot manifest conflicts",
    ):
        _seal(
            root=root,
            manifest_path=manifest_path,
            summary=_empty_summary("3690.HK", "9898.HK"),
        )

    assert first["status"] == "complete"
    assert manifest_path.read_bytes() == original_bytes


def test_reseal_rejects_manifest_tampering_without_repairing_it(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_empty_quote(root, run_id="run-1")
    summary = _empty_summary("3690.HK")
    _seal(root=root, manifest_path=manifest_path, summary=summary)
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["summary"]["ready"] = 0
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    tampered_bytes = manifest_path.read_bytes()

    with pytest.raises(
        RequiredDataSnapshotError,
        match="content hash mismatch",
    ):
        _seal(root=root, manifest_path=manifest_path, summary=summary)

    assert manifest_path.read_bytes() == tampered_bytes


def test_reseal_rejects_close_advice_plan_hash_or_root_conflict(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_empty_quote(root, run_id="run-1")
    summary = _empty_summary("3690.HK")
    close_plan_path = manifest_path.parent / "close_advice_required_data_plan.json"
    close_plan_path.write_text('{"plan":"one"}\n', encoding="utf-8")
    _seal(
        root=root,
        manifest_path=manifest_path,
        summary=summary,
        close_plan_path=close_plan_path,
    )
    original_bytes = manifest_path.read_bytes()

    close_plan_path.write_text('{"plan":"two"}\n', encoding="utf-8")
    with pytest.raises(
        RequiredDataSnapshotError,
        match="close-advice required-data plan hash mismatch",
    ):
        _seal(
            root=root,
            manifest_path=manifest_path,
            summary=summary,
            close_plan_path=close_plan_path,
        )
    assert manifest_path.read_bytes() == original_bytes

    close_plan_path.write_text('{"plan":"one"}\n', encoding="utf-8")
    other_root = root.parent / "other_required_data"
    other_root.mkdir()
    with pytest.raises(RequiredDataSnapshotError, match="root mismatch"):
        _seal(
            root=other_root,
            manifest_path=manifest_path,
            summary=summary,
            close_plan_path=close_plan_path,
        )
    assert manifest_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    ("initial_symbols", "published_symbols", "expected_status"),
    [
        (("3690.HK",), ("3690.HK",), "complete"),
        (("3690.HK", "9898.HK"), ("3690.HK",), "partial"),
        (("3690.HK",), (), "failed"),
    ],
)
def test_complete_partial_and_failed_manifests_are_terminal_absorbing(
    tmp_path: Path,
    initial_symbols: tuple[str, ...],
    published_symbols: tuple[str, ...],
    expected_status: str,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    for symbol in published_symbols:
        _publish_empty_quote(root, run_id="run-1", symbol=symbol)
    initial = _seal(
        root=root,
        manifest_path=manifest_path,
        summary=_empty_summary(*initial_symbols),
    )
    original_bytes = manifest_path.read_bytes()
    assert initial["status"] == expected_status

    conflicting_symbols = (
        ("3690.HK", "9898.HK")
        if initial_symbols == ("3690.HK",)
        else ("3690.HK",)
    )
    with pytest.raises(
        RequiredDataSnapshotError,
        match="terminal required-data snapshot manifest conflicts",
    ):
        _seal(
            root=root,
            manifest_path=manifest_path,
            summary=_empty_summary(*conflicting_symbols),
        )

    assert manifest_path.read_bytes() == original_bytes
