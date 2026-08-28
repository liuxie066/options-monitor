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
    assert manifest["upstream_commit"] == "ab1f1f3b333e4d33663d87beb5dda3eced671c04"
    assert len(manifest["upstream_commit"]) == 40
    assert all(character in "0123456789abcdef" for character in manifest["upstream_commit"])
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == manifest["sha256"]

    document = json.loads(contract_path.read_text(encoding="utf-8"))
    assert set(document["components"]["schemas"]["PublicErrorResponse"]["required"]) == {
        "success",
        "error_code",
        "message",
        "request_id",
        "details",
    }
    operations = {
        (method.upper(), path)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    assert set(CONTRACT_OPERATIONS) <= operations
    assert ("GET", "/api/v1/analysis/cash-facts") not in CONTRACT_OPERATIONS


def test_vendored_pm_openapi_declares_version_and_error_contracts() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document = json.loads((ROOT / manifest["contract_path"]).read_text(encoding="utf-8"))
    for (method, path), success_status in CONTRACT_OPERATIONS.items():
        operation = document["paths"][path][method.lower()]
        assert (
            operation["responses"][str(success_status)]["headers"]["X-PM-API-Version"]["schema"]["const"]
            == "portfolio.api.v1"
        )
        for status, response in operation["responses"].items():
            if status != str(success_status):
                assert response["headers"]["X-PM-API-Version"]["schema"]["const"] == "portfolio.api.v1"
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/PublicErrorResponse"
                }


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
