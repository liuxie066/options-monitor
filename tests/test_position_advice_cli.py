from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.domain.position_advice_authority import (
    portfolio_account_identity_hash,
    scope_for,
)
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.position_advice_authority_binding import (
    build_first_use_identity_binding_from_runtime,
)
from src.application.position_advice_authority_service import (
    authority_policy_path,
)
from src.application.position_advice_source_receipts import (
    publish_source_receipt,
    sha256_bytes,
)
from src.interfaces.cli.position_advice_ops import main


CONFIG_YAML = """\
accounts:
  lx:
    type: futu
    futu_account_id: "12345"
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
  hk:
    accounts: [lx]
    symbols: ["0700.HK"]
"""


def _runtime_fixture(
    tmp_path: Path,
    *,
    observed_at: datetime,
) -> tuple[Path, Path, str]:
    repo_root = Path(__file__).resolve().parents[1]
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(CONFIG_YAML, encoding="utf-8")
    identity = portfolio_account_identity_hash(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["12345"],
    )
    for market in ("us", "hk"):
        config, _meta = resolve_yaml_runtime_config(
            repo_root=repo_root,
            market=market,
            config_path=config_yaml,
        )
        (tmp_path / f"config.{market}.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )
        run_id = f"run-{market}"
        state_dir = (
            tmp_path
            / "output_runs"
            / run_id
            / "accounts"
            / "lx"
            / "state"
        )
        receipt_relpath = (
            f"position_advice_producers/portfolio/{market}/receipt.json"
        )
        receipt_path = state_dir / receipt_relpath
        receipt = publish_source_receipt(
            producer_root=state_dir,
            receipt_relpath=receipt_relpath,
            payload_relpath=(
                f"position_advice_producers/portfolio/{market}/payload.json"
            ),
            payload_bytes=json.dumps(
                {
                    "portfolio_source_name": "futu",
                    "source_account_identifiers": ["12345"],
                }
            ).encode(),
            source_kind="portfolio",
            producer_schema_version="portfolio_context.v2",
            producer_run_id=run_id,
            producer_scope="account",
            producer_account_run_id=run_id,
            broker="futu",
            account="lx",
            portfolio_account_identity_hash=identity,
            included_markets=[market.upper()],
            source_native_id=f"portfolio-{market}",
            source_observed_at=observed_at.isoformat(),
            completed_at=(observed_at + timedelta(seconds=1)).isoformat(),
            producer_policy_hash="b" * 64,
        )
        summary = {
            "schema_version": "position_advice_account_sources.v2",
            "account_run_id": run_id,
            "account": "lx",
            "included_markets": [market.upper()],
            "normalized_portfolio_source": "futu",
            "portfolio_account_identity_hash": identity,
            "source_receipts": [
                {
                    "source_kind": "portfolio",
                    "producer_root": str(state_dir),
                    "receipt_path": str(receipt_path),
                    "snapshot_id": receipt["snapshot_id"],
                    "receipt_hash": sha256_bytes(receipt_path.read_bytes()),
                }
            ],
        }
        (state_dir / "position_advice_sources.v2.json").write_text(
            json.dumps(summary),
            encoding="utf-8",
        )
    return repo_root, config_yaml, identity


def test_first_use_binding_requires_all_enabled_market_views_and_receipts(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    repo_root, config_yaml, identity = _runtime_fixture(
        tmp_path,
        observed_at=now,
    )

    binding = build_first_use_identity_binding_from_runtime(
        repo_root=repo_root,
        runtime_root=tmp_path,
        normalized_account="lx",
        config_yaml_path=config_yaml,
        now=now + timedelta(seconds=2),
    )

    assert binding["portfolio_account_identity_hash"] == identity
    assert binding["normalized_portfolio_source"] == "futu"
    assert binding["identity_binding_evidence"]["enabled_markets"] == [
        "HK",
        "US",
    ]


def test_position_advice_authority_cli_is_dry_run_by_default_and_confirmed_apply(
    tmp_path: Path,
    capsys,
) -> None:
    now = datetime.now(timezone.utc)
    _repo_root, config_yaml, _identity = _runtime_fixture(
        tmp_path,
        observed_at=now,
    )
    policy_path = authority_policy_path(tmp_path, scope_for("lx"))
    common = [
        "--runtime-root",
        str(tmp_path),
        "authority",
        "set",
        "--account",
        "lx",
        "--mode",
        "v2_shadow",
        "--expected-policy-hash",
        "absent",
        "--config-yaml",
        str(config_yaml),
        "--actor",
        "operator@example",
    ]

    assert main(common) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["status"] == "ready"
    assert not policy_path.exists()

    assert main([*common, "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["policy"]["mode"] == "v2_shadow"
    assert policy_path.is_file()


def test_first_use_cli_failure_returns_hash_intent_without_state_write(
    tmp_path: Path,
    capsys,
) -> None:
    now = datetime.now(timezone.utc)
    _repo_root, config_yaml, _identity = _runtime_fixture(
        tmp_path,
        observed_at=now,
    )
    (tmp_path / "config.hk.json").unlink()
    policy_path = authority_policy_path(tmp_path, scope_for("lx"))

    result = main(
        [
            "--runtime-root",
            str(tmp_path),
            "authority",
            "set",
            "--account",
            "lx",
            "--mode",
            "v2_shadow",
            "--expected-policy-hash",
            "absent",
            "--config-yaml",
            str(config_yaml),
            "--actor",
            "operator@example",
            "--confirm",
        ]
    )
    blocked = json.loads(capsys.readouterr().out)

    assert result == 2
    assert blocked["status"] == "blocked"
    assert blocked["applied"] is False
    assert blocked["dry_run"] is False
    intent = blocked["identity_binding_intent"]
    assert len(intent["authoring_config_hash"]) == 64
    assert intent["binding_result"] == "failed"
    assert intent["enabled_markets"] == ["HK", "US"]
    bindings = {
        item["market"]: item for item in intent["market_bindings"]
    }
    assert bindings["HK"]["generated_config_hash"] is None
    assert len(bindings["HK"]["source_receipt_hashes"]) == 1
    assert len(bindings["US"]["generated_config_hash"]) == 64
    assert len(bindings["US"]["source_receipt_hashes"]) == 1
    assert not policy_path.exists()
    assert not policy_path.parent.exists()
