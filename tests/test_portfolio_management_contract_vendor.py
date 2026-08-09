from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.infrastructure.portfolio_management_client import CONTRACT_OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "portfolio-management"
MANIFEST_PATH = CONTRACT_DIR / "vendor-manifest.json"


def test_vendored_pm_openapi_matches_manifest_and_client_operations() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    contract_path = ROOT / manifest["contract_path"]
    assert manifest["api_version"] == "portfolio.api.v1"
    assert manifest["upstream_release_state"] == "unpublished"
    assert manifest["planned_upstream_contract_release"] == "pm-api-v1.0.0"
    assert manifest["upstream_commit"] == "7c406e5f70e7b10e17d74b1f1ed242b4262e8ca3"
    assert len(manifest["upstream_commit"]) == 40
    assert all(character in "0123456789abcdef" for character in manifest["upstream_commit"])
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == manifest["sha256"]

    document = json.loads(contract_path.read_text(encoding="utf-8"))
    operations = {
        (method.upper(), path)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    assert operations == CONTRACT_OPERATIONS
    assert "/api/v1/analysis/cash-facts" not in document["paths"]


def test_vendored_pm_openapi_declares_version_and_error_contracts() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document = json.loads((ROOT / manifest["contract_path"]).read_text(encoding="utf-8"))
    for method, path in CONTRACT_OPERATIONS:
        operation = document["paths"][path][method.lower()]
        assert (
            operation["responses"]["200"]["headers"]["X-PM-API-Version"]["schema"]["const"]
            == "portfolio.api.v1"
        )
        assert operation["responses"]["503"]["content"]["application/json"]["schema"]


def test_vendored_pm_openapi_requires_core_success_and_freshness_fields() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document = json.loads((ROOT / manifest["contract_path"]).read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    assert {
        "success",
        "accounts",
        "count",
        "freshness",
        "retrieved_at_utc",
    } <= set(schemas["AccountsResponse"]["required"])
    assert {
        "success",
        "count",
        "freshness",
        "retrieved_at_utc",
    } <= set(schemas["HoldingsResponse"]["required"])


def test_distribution_openapi_does_not_claim_row_level_value_currency() -> None:
    """Keep the CNY unit assumption explicit at the OM consumer boundary."""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document = json.loads((ROOT / manifest["contract_path"]).read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["DistributionResponse"]
    by_asset = schema["properties"]["by_asset"]["anyOf"][0]
    assert by_asset["items"] == {
        "additionalProperties": True,
        "type": "object",
    }
    assert "valuation_currency" not in schema["properties"]
