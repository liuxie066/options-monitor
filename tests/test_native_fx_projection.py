from datetime import datetime, timezone
import json

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256


def envelope(at="2026-10-23T03:00:00.123456+00:00"):
    pairs = {
        pair: dict(rate=rate, source="tencent_quote", quote_at_utc=at, observed_at_utc=at)
        for pair, rate in (("USDCNY", 7.1), ("HKDCNY", 0.91))
    }
    snapshot = dict(schema_version=2, evaluated_at_utc=at, pairs=pairs)
    return dict(schema_version=1, run_id="frozen-run", snapshot=snapshot, snapshot_sha256=canonical_sha256(snapshot))


def project(value, run_id="frozen-run"):
    from om_strategy_core.evaluation import project_run_fx_facts

    return project_run_fx_facts(json.dumps(value).encode(), run_id=run_id)


def test_native_projection_matches_existing_application_owner():
    from src.application.cash_conversion import cash_fx_observation_facts

    value = envelope()
    expected = cash_fx_observation_facts(value["snapshot"], observed_at_ms=1792724401000)
    result = project(value)
    assert result["status"] == "complete"
    assert result["missing_pairs"] == []
    assert result["facts"] == [fact.normalized_payload() for fact in expected]
    assert result["snapshot_sha256"] == value["snapshot_sha256"]
    assert project(value) == result


@pytest.mark.parametrize("at", ["2026-10-18T03:00:00Z", "2027-01-02T03:00:00Z"])
def test_decode_does_not_make_non_session_quotes_eligible(at):
    result = project(envelope(at))
    assert result["status"] == "incomplete"
    assert result["facts"] == []
    assert result["missing_pairs"] == ["USDCNY", "HKDCNY"]


@pytest.mark.parametrize("change", ["wrong_run", "digest", "missing_asof", "naive_asof"])
def test_identity_digest_and_explicit_asof_are_required(change):
    from src.application.current_fx_run import RunFXError

    value = envelope()
    if change == "wrong_run":
        value["run_id"] = "another-run"
    elif change == "digest":
        value["snapshot"]["pairs"]["USDCNY"]["rate"] = 8
    else:
        value["snapshot"]["evaluated_at_utc"] = None if change == "missing_asof" else "2026-10-23T03:00:00"
        value["snapshot_sha256"] = canonical_sha256(value["snapshot"])
    with pytest.raises(RunFXError):
        project(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rate", True),
        ("rate", 0),
        ("source", "unknown"),
        ("quote_at_utc", "2026-10-23T03:00:01Z"),
        ("observed_at_utc", "2026-10-23T03:00:00.123457+00:00"),
    ],
)
def test_partial_invalid_pairs_remain_missing(field, value):
    item = envelope()
    item["snapshot"]["pairs"]["USDCNY"][field] = value
    item["snapshot_sha256"] = canonical_sha256(item["snapshot"])
    result = project(item)
    assert result["status"] == "incomplete"
    assert result["missing_pairs"] == ["USDCNY"]
    assert [row["base_currency"] for row in result["facts"]] == ["HKD"]


def test_existing_application_entry_is_the_same_owner():
    from domain.domain.performance import cash_conversion as owner
    from src.application import cash_conversion, current_fx_run
    from src.infrastructure import exchange_rates

    assert cash_conversion.cash_fx_observation_facts is owner.cash_fx_observation_facts
    assert current_fx_run._decode is owner.decode_run_fx_snapshot
    assert exchange_rates._verified_pairs is owner._verified_pairs
    assert exchange_rates._verified_pair is owner._verified_pair
    assert owner.verified_exchange_rate_pairs(
        envelope()["snapshot"], observed_at=datetime(2026, 10, 23, 4, tzinfo=timezone.utc)
    )


@pytest.mark.parametrize("run_id", [None, "", " frozen-run", 1])
def test_projection_requires_an_explicit_run_identity(run_id):
    from src.application.current_fx_run import RunFXError

    with pytest.raises(RunFXError):
        project(envelope(), run_id=run_id)


def candidate_snapshot():
    original = envelope()
    observation = original["snapshot"]
    return {
        "schema_version": "opening_candidate_snapshot.v3",
        "run_id": original["run_id"],
        "dependencies": [{"kind": "fx", "sha256": "a" * 64}],
        "capacity_fx_evidence": {
            "evaluated_at_utc": observation["evaluated_at_utc"],
            "observation": observation,
            "observation_sha256": canonical_sha256(observation),
            "run_fx_snapshot_sha256": "a" * 64,
            "observation_status": "ready",
        },
    }


def project_candidate(value):
    from om_strategy_core.evaluation import project_candidate_fx_facts

    return project_candidate_fx_facts(json.dumps(value).encode(), run_id="frozen-run")


def test_candidate_observation_reuses_native_fact_owner():
    result = project_candidate(candidate_snapshot())
    assert result["facts"] == project(envelope())["facts"]
    assert result["status"] == "complete"
    assert result["source_kind"] == "candidate_snapshot"
    assert result["run_fx_snapshot_sha256"] == "a" * 64


@pytest.mark.parametrize("field", ["schema_version", "run_id", "capacity_fx_evidence", "dependencies"])
def test_candidate_identity_and_fx_reference_are_required(field):
    from src.application.current_fx_run import RunFXError

    value = candidate_snapshot()
    value[field] = None
    with pytest.raises(RunFXError):
        project_candidate(value)


@pytest.mark.parametrize("change", ["hash", "quote", "clock", "reference", "duplicate_reference"])
def test_candidate_observation_binding_is_not_repaired(change):
    from src.application.current_fx_run import RunFXError

    value = candidate_snapshot()
    evidence = value["capacity_fx_evidence"]
    if change == "hash":
        evidence["observation_sha256"] = "b" * 64
    elif change == "quote":
        evidence["observation"]["pairs"]["USDCNY"]["rate"] = 8
    elif change == "clock":
        evidence["evaluated_at_utc"] = "2026-10-24T00:00:00Z"
    elif change == "reference":
        evidence["run_fx_snapshot_sha256"] = "b" * 64
    else:
        value["dependencies"].append(dict(value["dependencies"][0]))
    with pytest.raises(RunFXError):
        project_candidate(value)


@pytest.mark.parametrize("at", ["2026-10-18T03:00:00Z", "2026-10-23T03:00:00.123457+00:00"])
def test_candidate_ready_marker_cannot_qualify_invalid_pairs(at):
    value = candidate_snapshot()
    evidence = value["capacity_fx_evidence"]
    for pair in evidence["observation"]["pairs"].values():
        pair["quote_at_utc"] = at
        pair["observed_at_utc"] = at
    evidence["observation_sha256"] = canonical_sha256(evidence["observation"])
    result = project_candidate(value)
    assert result["status"] == "incomplete"
    assert result["facts"] == []
    assert result["missing_pairs"] == ["USDCNY", "HKDCNY"]
