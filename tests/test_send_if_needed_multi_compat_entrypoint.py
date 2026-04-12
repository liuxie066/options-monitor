from __future__ import annotations


def test_select_markets_to_run_compat_export_uses_domain_entrypoint() -> None:
    from om.domain import select_markets_to_run
    from scripts.send_if_needed_multi import _select_markets_to_run

    assert _select_markets_to_run is select_markets_to_run
