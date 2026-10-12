from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.application.ledger import decision_snapshot as snapshot
from src.application.ledger import lifecycle_overlay as overlay
from test_decision_snapshot_reuse import _many, _rows
from test_lifecycle_overlay_resolver import _bridge_anchor, _case, _direct_anchor, _lot


def _bundle(kind):
    cases = [_case("a", {"lot-1": 1})]
    evidence, claims = [], []
    if kind in {"direct", "contested", "transitive", "void"}:
        anchor, claim = _direct_anchor(
            case_id="a", evidence_id="anchor-a", source_suffix="deal-a",
            manifest={"lot-1": 1}, received_at_ms=1_700_000_000_200,
        )
        evidence.append(anchor)
        claims.append(claim)
    if kind in {"contested", "transitive"}:
        manifest = {"lot-1": 1, "lot-2": 1}
        cases.append(_case("b", manifest))
        anchor, claim = _direct_anchor(
            case_id="b", evidence_id="anchor-b", source_suffix="deal-b",
            manifest=manifest, received_at_ms=1_700_000_000_300,
        )
        evidence.append(anchor)
        claims.append(claim)
        if kind == "transitive":
            cases.append(_case("c", {"lot-2": 1, "lot-3": 1}))
    if kind == "bridge":
        evidence, claims, legacy = _bridge_anchor(
            case_id="a", legacy_case_id="legacy", manifest={"lot-1": 1},
        )
        cases.append(legacy)
    allocations = []
    if kind == "void":
        allocations = [{"allocation_id": "allocation-a", "case_id": "a",
                        "target_lot_id": "lot-1", "contracts_allocated": 1,
                        "canonical_terminal_event_id": "terminal-a"}]
    return dict(account="lx", cases=cases, evidence=evidence, allocations=allocations,
                source_claims=claims, timing_policies=[],
                position_lots=[_lot(f"lot-{i}") for i in range(1, 4)],
                effective_void_event_ids=["terminal-a"] if kind == "void" else [])


@pytest.mark.parametrize("kind", ["waiting", "direct", "contested", "transitive", "bridge", "void"])
def test_fact_check_omits_preimages_but_preserves_every_fact(kind, monkeypatch):
    bundle = _bundle(kind)
    frozen = deepcopy(bundle)
    events = Mock(wraps=overlay._events_for_lots)
    monkeypatch.setattr(overlay, "_events_for_lots", events)
    full = overlay.resolve_account_lifecycle_overlay(**bundle)
    assert events.call_count == len(full["generation_tokens"])
    assert overlay.validate_account_lifecycle_resolution(full) == ()
    events.reset_mock()
    facts = overlay.resolve_account_lifecycle_overlay(**bundle, _include_generation_token_hashes=False)
    assert events.call_count == 0
    assert snapshot._lifecycle_resolution_fact_view(facts) == snapshot._lifecycle_resolution_fact_view(full)
    assert all("generation_token" not in item for item in facts["generation_tokens"])
    assert bundle == frozen
    resolution = overlay.lifecycle_case_resolution(full, case_id="a")
    assert resolution["status"] == {"waiting": "missing", "direct": "direct",
                                    "contested": "conflict", "transitive": "conflict",
                                    "bridge": "bridged", "void": "direct"}[kind]
    if kind == "void":
        assert resolution["effective_reservations_by_lot"] == {"lot-1": 1}
        unvoided = overlay.resolve_account_lifecycle_overlay(**{**bundle, "effective_void_event_ids": []})
        assert overlay.lifecycle_case_resolution(unvoided, case_id="a")["effective_reservations_by_lot"] == {}
    if kind == "transitive":
        assert facts["generation_tokens"][0]["dependency_case_ids"] == ["a", "b", "c"]
        assert facts["generation_tokens"][0]["target_lot_ids"] == ["lot-1", "lot-2", "lot-3"]


@pytest.mark.parametrize("kind", ["direct", "bridge", "transitive"])
def test_snapshot_rejections_match_full_expected_resolution(tmp_path, monkeypatch, kind):
    rows = _rows(tmp_path)
    bundle = _bundle(kind)
    account = rows["lx"]
    # Frozen rows use real projected lots; bind synthetic cases to those identities.
    lot_id = account["account_position_lots"][0]["record_id"]
    for item in bundle["cases"] + bundle["evidence"]:
        manifest = item.get("target_contracts_by_lot")
        if manifest:
            item["target_contracts_by_lot"] = {lot_id: 1}
    # Direct claims bind the complete evidence payload; rebuild after rebinding.
    from src.application.ledger.source_consumption import build_source_consumption_claim
    if kind != "bridge":
        bundle["source_claims"] = [build_source_consumption_claim(
            source_key=item["source_event_id"], case_id=item["case_id"],
            owner_evidence_id=item["evidence_id"], source_role="option_anchor",
            economic_payload=item,
        ) for item in bundle["evidence"]]
    for field, key in (("cases", "cases"), ("evidence", "evidence"),
                       ("source_consumptions", "source_claims")):
        account[f"account_lifecycle_{field}"] = bundle[key]
    account["account_lifecycle_evidence_received_at_ms_by_id"] = {
        item["evidence_id"]: 1_700_000_000_200 for item in bundle["evidence"]
    }
    good = _many(rows)["lx"]
    assert good["snapshot_status"] == "trusted"
    assert snapshot.validate_position_fact_snapshot_contract(good) == ()
    variants = {"valid": good}
    for name in ("account", "receipt", "evidence", "resolution", "token", "fingerprint",
                 "dependencies_resealed", "targets_resealed", "anchor_resealed", "void_resealed"):
        item = deepcopy(good)
        resolution = item["account_lifecycle_resolution"]
        if name == "account":
            item["normalized_account"] = "sy"
        elif name == "receipt":
            item["account_lifecycle_evidence_received_at_ms_by_id"].clear()
        elif name == "evidence":
            item["account_lifecycle_evidence"][0]["untrusted_observation"] = {"amount": 999}
        elif name == "resolution":
            resolution["case_resolutions"][0]["status"] = "tampered"
        elif name == "token":
            resolution["generation_tokens"][0]["generation_token"] = "1" * 64
        elif name == "fingerprint":
            item["decision_state_fingerprint"] = "0" * 64
        elif name == "dependencies_resealed":
            resolution["generation_tokens"][0]["dependency_case_ids"].append("phantom-case")
        elif name == "targets_resealed":
            resolution["generation_tokens"][0]["target_lot_ids"].append("phantom-lot")
        elif name == "anchor_resealed":
            # Resealing the outer fingerprint cannot disguise altered owner facts.
            evidence = item["account_lifecycle_evidence"][0]
            if kind == "bridge":
                evidence["raw"]["price"] = "12"
            else:
                evidence["price"] = "12"
        else:
            item["effective_void_event_ids"] = ["unrelated-terminal"]
            # An unrelated void does not change facts; invalid duplicate must still fail.
            item["effective_void_event_ids"].append("unrelated-terminal")
        if name.endswith("_resealed"):
            item["decision_state_fingerprint"] = snapshot.decision_state_snapshot_fingerprint(item)
        variants[name] = item
    actual = {name: snapshot.validate_position_fact_snapshot_contract(item)
              for name, item in variants.items()}
    assert not actual["valid"]
    assert all(reasons for name, reasons in actual.items() if name != "valid")
    for name in ("dependencies_resealed", "targets_resealed", "anchor_resealed"):
        assert "position_fact_lifecycle_resolution_facts_mismatch" in actual[name]
    original = overlay.resolve_account_lifecycle_overlay

    def full_expected(**kwargs):
        assert kwargs.pop("_include_generation_token_hashes") is False
        return original(**kwargs)

    monkeypatch.setattr(snapshot, "resolve_account_lifecycle_overlay", full_expected)
    assert actual == {name: snapshot.validate_position_fact_snapshot_contract(item)
                      for name, item in variants.items()}
    # The other account remains isolated from all lifecycle evidence above.
    assert snapshot.validate_position_fact_snapshot_contract(_many(rows)["sy"]) == ()
