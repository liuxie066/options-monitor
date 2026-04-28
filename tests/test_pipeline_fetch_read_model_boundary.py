from __future__ import annotations

import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _make_dirs(root: Path) -> tuple[Path, Path]:
    required = (root / "required_data").resolve()
    state_dir = (root / "state").resolve()
    (required / "parsed").mkdir(parents=True, exist_ok=True)
    (required / "raw").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return required, state_dir


def test_ensure_required_data_uses_read_model_error_to_force_refetch() -> None:
    from scripts import pipeline_fetch_models as models
    import scripts.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_error").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")

    models.record_fetch_snapshot(
        state_dir=state_dir,
        symbol=symbol,
        source="opend",
        status="error",
        reason="previous_failed",
    )

    old_fetch = mod.fetch_required_data_opend
    called: list[object] = []
    try:
        mod.fetch_required_data_opend = lambda **kwargs: called.append(kwargs)  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.fetch_required_data_opend = old_fetch  # type: ignore[assignment]

    assert len(called) == 1
    request = called[0]["request"]
    assert request.symbol == symbol
    assert request.option_types == "put"


def test_ensure_required_data_skips_when_read_model_is_ok_and_dte_satisfies() -> None:
    from scripts import pipeline_fetch_models as models
    import scripts.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_ok").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")

    models.record_fetch_snapshot(
        state_dir=state_dir,
        symbol=symbol,
        source="opend",
        status="ok",
    )

    old_fetch = mod.fetch_required_data_opend
    called: list[object] = []
    try:
        mod.fetch_required_data_opend = lambda **kwargs: called.append(kwargs)  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            min_dte=5,
        )
    finally:
        mod.fetch_required_data_opend = old_fetch  # type: ignore[assignment]

    assert called == []


def test_ensure_required_data_treats_futu_source_as_opend_path() -> None:
    import scripts.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_futu_source").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)

    old_fetch = mod.fetch_required_data_opend
    called: list[object] = []
    try:
        mod.fetch_required_data_opend = lambda **kwargs: called.append(kwargs)  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol="AAPL",
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="futu",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.fetch_required_data_opend = old_fetch  # type: ignore[assignment]

    assert len(called) == 1
    request = called[0]["request"]
    assert request.symbol == "AAPL"
    assert request.option_types == "put"


def test_ensure_required_data_does_not_read_raw_fetch_file_on_main_path() -> None:
    import pathlib
    import scripts.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_no_raw").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")
    (required / "raw" / f"{symbol}_required_data.json").write_text(
        '{"meta": {"error": "legacy_error"}}',
        encoding="utf-8",
    )

    old_fetch = mod.fetch_required_data_opend
    old_read_text = pathlib.Path.read_text
    called: list[object] = []
    raw_touched: list[Path] = []

    def _guard_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        p = str(self)
        if p.endswith(f"{symbol}_required_data.json"):
            raw_touched.append(self)
        return old_read_text(self, *args, **kwargs)

    try:
        mod.fetch_required_data_opend = lambda **kwargs: called.append(kwargs)  # type: ignore[assignment]
        pathlib.Path.read_text = _guard_read_text  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.fetch_required_data_opend = old_fetch  # type: ignore[assignment]
        pathlib.Path.read_text = old_read_text  # type: ignore[assignment]

    assert called == []
    assert raw_touched == []


def test_fetch_required_data_opend_normalizes_timestamp_explicit_expirations(tmp_path: Path) -> None:
    from src.application.required_data_fetching import RequiredDataFetchRequest
    import src.application.required_data_fetching as mod

    old_fetch = mod.fetch_symbol
    old_save = mod.save_outputs
    captured: dict[str, object] = {}
    try:
        def _fake_fetch_symbol(symbol, **kwargs):  # type: ignore[no-untyped-def]
            captured["symbol"] = symbol
            captured["explicit_expirations"] = kwargs.get("explicit_expirations")
            return {"rows": [], "expiration_count": 0}

        def _fake_save_outputs(base, symbol, payload, *, output_root=None):  # type: ignore[no-untyped-def]
            return Path(base) / "raw.json", Path(base) / "parsed.csv"

        mod.fetch_symbol = _fake_fetch_symbol  # type: ignore[assignment]
        mod.save_outputs = _fake_save_outputs  # type: ignore[assignment]
        mod.fetch_required_data_opend(
            base=tmp_path,
            request=RequiredDataFetchRequest(
                symbol="FUTU",
                limit_expirations=2,
                explicit_expirations=[1777420800, "1781740800000"],
            ),
        )
    finally:
        mod.fetch_symbol = old_fetch  # type: ignore[assignment]
        mod.save_outputs = old_save  # type: ignore[assignment]

    assert captured["symbol"] == "FUTU"
    assert captured["explicit_expirations"] == ["2026-04-29", "2026-06-18"]


def main() -> None:
    test_ensure_required_data_uses_read_model_error_to_force_refetch()
    test_ensure_required_data_skips_when_read_model_is_ok_and_dte_satisfies()
    test_ensure_required_data_treats_futu_source_as_opend_path()
    test_ensure_required_data_does_not_read_raw_fetch_file_on_main_path()
    test_fetch_required_data_opend_normalizes_timestamp_explicit_expirations(Path("."))
    print("OK (pipeline-fetch-read-model-boundary)")


if __name__ == "__main__":
    main()
