from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUALITY_DOCS = ROOT / "docs" / "quality-monitoring"
CROSS_SYSTEM_DOCUMENTS = {
    "api-contract.md",
    "architecture.md",
    "check-matrix.md",
    "hub-check-implementation.md",
    "implementation-plan.md",
    "implementation-status.md",
    "phase5-runbook.md",
}


def test_om_does_not_duplicate_hub_owned_quality_documents() -> None:
    duplicated = [
        document
        for document in CROSS_SYSTEM_DOCUMENTS
        if (QUALITY_DOCS / document).exists()
    ]

    assert duplicated == []


def test_om_retains_only_local_quality_documentation() -> None:
    assert (QUALITY_DOCS / "README.md").is_file()
    assert (QUALITY_DOCS / "om-check-implementation.md").is_file()
    assert (QUALITY_DOCS / "om-operator.md").is_file()
