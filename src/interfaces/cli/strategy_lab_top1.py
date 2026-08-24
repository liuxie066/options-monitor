from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.ledger.api import resolve_position_ledger_sqlite_path
from src.application.service_deploy import load_service_profile, service_status_from_profile
from src.application.service_drift import service_drift
from src.application.strategy_lab.top1.advance import ADVANCE_REVISION, advance_scheduled
from src.application.strategy_lab.top1.capability_receipts import (
    Top1CapabilityReceiptError,
    capability_facts_from_receipt,
    load_account_fee_plan_receipt,
    read_top1_capability_receipt,
    refresh_top1_capability_receipt,
)
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    preview_archived_recommendation_point_migration,
    read_corpus_status,
    read_market_calendar_binding,
    refresh_market_calendar_binding,
)
from src.application.strategy_lab.top1.lifecycle import (
    read_public_receipt,
    read_public_status,
)
from src.application.strategy_lab.top1.readiness import (
    CAPABILITY_FACTS,
    build_top1_readiness,
)
from src.application.strategy_lab.top1.workspace import (
    Top1WorkspaceError,
    preview_sell_put_top1_research,
    preview_sell_put_top1_validation,
    start_confirmed_research,
    start_confirmed_validation,
)
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.infrastructure.futu_gateway import (
    FutuGatewayError,
    build_ready_futu_quote_gateway,
)
from src.infrastructure.performance_evidence_sqlite import (
    EvidenceReadBundle,
    PerformanceEvidenceSQLiteRepository,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


_TOP1_PROFILE_FIELDS = {
    "enabled",
    "market",
    "account",
    "opend_binding",
    "advance_interval",
    "timeout_start_sec",
}


def _add_identity(parser: Any) -> None:
    parser.add_argument("--market", required=True, choices=("hk",))
    parser.add_argument("--account", required=True, choices=("lx",))
    parser.add_argument("--profile-path", required=True)


def add_top1_commands(strategy_lab_subparsers: Any) -> None:
    top1 = strategy_lab_subparsers.add_parser(
        "top1-loop",
        help="inspect or advance the experimental HK/lx Sell Put Top1 loop",
    )
    commands = top1.add_subparsers(dest="top1_loop_command", required=True)

    advance = commands.add_parser("advance", help="run one scheduled local advance")
    _add_identity(advance)
    advance.add_argument("--scheduled", action="store_true")
    advance.add_argument("--write", action="store_true")

    calendar = commands.add_parser("calendar", help="manage HK calendar evidence")
    calendar_commands = calendar.add_subparsers(required=True)
    calendar_refresh = calendar_commands.add_parser(
        "refresh", help="collect and publish HK calendar evidence"
    )
    _add_identity(calendar_refresh)
    calendar_refresh.add_argument("--coverage-start", required=True)
    calendar_refresh.add_argument("--coverage-end", required=True)
    calendar_refresh.add_argument("--calendar-version", required=True)
    calendar_refresh.add_argument("--write", action="store_true")

    capabilities = commands.add_parser(
        "capabilities", help="manage compact W0R capability evidence"
    )
    capability_commands = capabilities.add_subparsers(required=True)
    capability_refresh = capability_commands.add_parser(
        "refresh", help="run one explicit W0R provider probe"
    )
    _add_identity(capability_refresh)
    capability_refresh.add_argument("--fee-plan-receipt-path", required=True)
    capability_refresh.add_argument("--stock-owner", required=True)
    capability_refresh.add_argument("--contract-symbol", required=True)
    capability_refresh.add_argument("--terms-expiration", required=True)
    capability_refresh.add_argument("--close-expiration", required=True)
    capability_refresh.add_argument("--write", action="store_true")

    status = commands.add_parser("status", help="show one experiment's public status")
    _add_identity(status)
    status.add_argument("--experiment-id", required=True)

    readiness = commands.add_parser(
        "readiness", help="show source-delivery and validation-runtime blockers"
    )
    _add_identity(readiness)

    history = commands.add_parser("history", help="inspect archived recommendation points")
    history_commands = history.add_subparsers(required=True)
    history_migrate = history_commands.add_parser("migrate")
    history_migrate_commands = history_migrate.add_subparsers(
        dest="top1_history_migrate_command", required=True
    )
    history_preview = history_migrate_commands.add_parser("preview")
    _add_identity(history_preview)
    history_preview.add_argument("--source-root", required=True)

    research = commands.add_parser("research", help="preview or start the 20-day research")
    research_commands = research.add_subparsers(
        dest="top1_research_command", required=True
    )
    for name in ("preview", "start"):
        parser = research_commands.add_parser(name)
        _add_identity(parser)
        parser.add_argument("--cutoff-at-utc", required=True)
        parser.add_argument("--latest-mature-trading-date", required=True)
        if name == "start":
            parser.add_argument("--confirmed-start-file", required=True)
            parser.add_argument("--write", action="store_true")

    validation = commands.add_parser(
        "validation", help="preview or start the 10-day hidden validation"
    )
    validation_commands = validation.add_subparsers(
        dest="top1_validation_command", required=True
    )
    for name in ("preview", "start"):
        parser = validation_commands.add_parser(name)
        _add_identity(parser)
        parser.add_argument("--experiment-id", required=True)
        parser.add_argument("--validation-start-trading-date", required=True)
        if name == "start":
            parser.add_argument("--confirmed-start-file", required=True)
            parser.add_argument("--write", action="store_true")

    receipt = commands.add_parser(
        "receipt", help="read one published final experiment receipt"
    )
    _add_identity(receipt)
    receipt.add_argument("--experiment-id", required=True)


def _absolute_profile_path(raw: object) -> Path:
    path = Path(str(raw or "").strip()).expanduser()
    return path if path.is_absolute() else path.resolve()


def _path_from_profile(profile: Mapping[str, Any], key: str) -> Path:
    raw = str(profile.get(key) or "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        raise AgentToolError(
            code="CONFIG_ERROR", message=f"profile {key} must be an absolute path"
        )
    return path


def _profile_context(
    args: argparse.Namespace, *, require_top1: bool
) -> dict[str, Any]:
    profile_path = _absolute_profile_path(args.profile_path)
    try:
        profile = load_service_profile(profile_path)
    except (OSError, ValueError) as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc
    repo_root = _path_from_profile(profile, "repo_root")
    runtime_root = _path_from_profile(profile, "runtime_root")
    top1_raw = profile.get("strategy_lab_top1")
    top1 = dict(top1_raw) if isinstance(top1_raw, Mapping) else {}
    if top1 and (
        top1.get("market") != args.market or top1.get("account") != args.account
    ):
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="requested market/account disagrees with the Strategy Lab Top1 profile",
        )
    if require_top1:
        _path_from_profile(profile, "env_file")
        binding_raw = top1.get("opend_binding")
        binding = dict(binding_raw) if isinstance(binding_raw, Mapping) else {}
        selected_markets = profile.get("markets")
        selected_accounts = profile.get("accounts")
        valid = (
            profile.get("service_provider") == "systemd"
            and isinstance(selected_markets, list)
            and "hk" in selected_markets
            and isinstance(selected_accounts, list)
            and "lx" in selected_accounts
            and set(top1) == _TOP1_PROFILE_FIELDS
            and top1.get("enabled") is True
            and top1.get("market") == "hk"
            and top1.get("account") == "lx"
            and isinstance(binding.get("host"), str)
            and bool(str(binding.get("host") or "").strip())
            and type(binding.get("port")) is int
            and 0 < binding["port"] <= 65535
            and type(top1.get("advance_interval")) is int
            and top1["advance_interval"] > 0
            and type(top1.get("timeout_start_sec")) is int
            and top1["timeout_start_sec"] > 0
        )
        if not valid:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="Strategy Lab Top1 systemd profile binding is missing or invalid",
            )
    config_paths = profile.get("config_paths")
    config_hk = (
        Path(str(config_paths.get("hk") or "")).expanduser()
        if isinstance(config_paths, Mapping)
        else Path()
    )
    if require_top1 and (not config_hk.is_absolute() or not str(config_hk)):
        raise AgentToolError(
            code="CONFIG_ERROR", message="profile HK runtime config path is invalid"
        )
    return {
        "profile_path": profile_path,
        "profile": profile,
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "config_hk": config_hk,
        "top1": top1,
        "store_path": runtime_root
        / "output_shared"
        / "research"
        / "strategy_lab"
        / "experiments.sqlite3",
        "artifact_root": runtime_root / "output_shared" / "research" / "strategy_lab",
    }


def _readiness(context: Mapping[str, Any], store: ExperimentStore) -> dict[str, Any]:
    profile = context["profile"]
    errors: list[dict[str, str]] = []
    try:
        drift = service_drift(
            repo_root=context["repo_root"],
            runtime_root=context["runtime_root"],
            profile_path=context["profile_path"],
            profile=profile,
            confirm=False,
        )
    except Exception as exc:
        drift = {"summary": {"status": "error"}}
        errors.append({"reason_code": "service_drift_unavailable", "message": str(exc)})
    try:
        status = service_status_from_profile(
            profile, include_status=True, include_enabled=True
        )
    except Exception as exc:
        status = {"services": []}
        errors.append({"reason_code": "service_status_unavailable", "message": str(exc)})

    schema = store.schema_state()
    corpus: dict[str, Any] | None = None
    if schema.get("status") == "ready":
        try:
            corpus = read_corpus_status(store, market="HK", account="lx")
        except Exception as exc:
            errors.append({"reason_code": "top1_status_unavailable", "message": str(exc)})
    try:
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception as exc:
        calendar = None
        errors.append(
            {"reason_code": "market_calendar_binding_unavailable", "message": str(exc)}
        )
    capability_receipt: dict[str, object] | None = None
    binding = context["top1"].get("opend_binding")
    if isinstance(binding, Mapping):
        try:
            capability_receipt = read_top1_capability_receipt(
                context["artifact_root"],
                market="HK",
                account="lx",
                expected_opend_binding=binding,
            )
        except Top1CapabilityReceiptError as exc:
            errors.append({"reason_code": exc.reason_code, "message": str(exc)})
    result = build_top1_readiness(
        profile=profile,
        drift=drift,
        service_status=status,
        schema_state=schema,
        corpus_status=corpus,
        calendar_binding=calendar,
        capability_facts=(
            capability_facts_from_receipt(capability_receipt)
            if capability_receipt is not None
            else None
        ),
    )
    result["facts"]["capability_receipt"] = (
        {
            key: capability_receipt[key]
            for key in (
                "observed_at_utc",
                "receipt_ref",
                "content_sha256",
                "receipt_file_sha256",
            )
        }
        if capability_receipt is not None
        else None
    )
    if errors:
        result["fact_errors"] = errors
    return result


def _store_not_ready(tool_name: str, store: ExperimentStore) -> dict[str, Any]:
    schema = store.schema_state()
    return build_response(
        tool_name=tool_name,
        ok=False,
        data={"store_schema": schema},
        error={
            "code": "STORE_NOT_READY",
            "message": "Strategy Lab Top1 store is not ready; read commands do not migrate it",
        },
    )


def _research_inputs(
    context: Mapping[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any]]:
    top1 = context["top1"]
    try:
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception:
        calendar = {}
    try:
        capability = read_top1_capability_receipt(
            context["artifact_root"],
            market="HK",
            account="lx",
            expected_opend_binding=top1["opend_binding"],
        )
    except Exception:
        capability = {}
    plan = capability.get("account_fee_plan_receipt")
    fee_contract: dict[str, object] = {
        "market": "HK",
        "account": "lx",
        "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "account_fee_plan": (
            {
                key: plan[key]
                for key in ("commission_free", "platform_fee", "fee_plan_ref")
            }
            if isinstance(plan, Mapping)
            and all(key in plan for key in ("commission_free", "platform_fee", "fee_plan_ref"))
            else None
        ),
    }
    try:
        _config_path, config = load_runtime_config(
            config_path=context["config_hk"], expected_market="hk"
        )
        ledger_path = resolve_position_ledger_sqlite_path(
            base=context["repo_root"],
            cfg=config,
            config_path=context["config_hk"],
        )
        evidence_bundle = PerformanceEvidenceSQLiteRepository(ledger_path).read_all()
    except Exception:
        config = {}
        evidence_bundle = EvidenceReadBundle(schema_state="not_initialized")
    preview_inputs = {
        "market": args.market.upper(),
        "account": args.account,
        "cutoff_at_utc": args.cutoff_at_utc,
        "latest_mature_trading_date": args.latest_mature_trading_date,
        "market_calendar": calendar,
        "datasets_root_ref": (
            "remote_archive/prod/output_shared/research/shadow_replay/datasets"
        ),
        "runs_root_ref": "remote_archive/prod/output_runs",
        "fee_contract": fee_contract,
        "capability_facts": capability_facts_from_receipt(capability),
        "evidence_bundle": evidence_bundle,
        "environ": None,
    }
    return preview_inputs, config


def handle_top1_command(args: argparse.Namespace) -> dict[str, Any]:
    command = args.top1_loop_command
    context = _profile_context(
        args,
        require_top1=command
        in {"advance", "calendar", "capabilities", "research", "validation"},
    )

    if command == "calendar":
        if not args.write:
            raise AgentToolError(
                code="INPUT_ERROR", message="Top1 calendar refresh requires --write"
            )
        top1 = context["top1"]
        binding = top1["opend_binding"]
        gateway = None
        try:
            gateway = build_ready_futu_quote_gateway(
                host=str(binding["host"]),
                port=int(binding["port"]),
                is_option_chain_cache_enabled=False,
            )
            result = refresh_market_calendar_binding(
                context["artifact_root"],
                gateway=gateway,
                market=args.market.upper(),
                market_calendar_version=args.calendar_version,
                coverage_start=args.coverage_start,
                coverage_end=args.coverage_end,
                observed_at_utc=datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            )
        except (CorpusError, FutuGatewayError) as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", getattr(exc, "code", "ERROR"))),
                message=str(exc),
            ) from exc
        finally:
            if gateway is not None:
                gateway.close()
        calendar_binding = result["binding"]
        return build_response(
            tool_name="research.strategy-lab.top1-loop.calendar.refresh",
            ok=True,
            data={
                "status": result["status"],
                "market": calendar_binding["market"],
                "market_calendar_version": calendar_binding[
                    "market_calendar_version"
                ],
                "coverage_start": calendar_binding["coverage_start"],
                "coverage_end": calendar_binding["coverage_end"],
                "trading_date_count": len(calendar_binding["trading_dates"]),
                "source_receipt_sha256": calendar_binding[
                    "source_receipt_sha256"
                ],
                "observed_at_utc": calendar_binding["observed_at_utc"],
                "snapshot_ref": calendar_binding["snapshot_ref"],
                "snapshot_content_sha256": calendar_binding[
                    "snapshot_content_sha256"
                ],
                "snapshot_file_sha256": calendar_binding[
                    "snapshot_file_sha256"
                ],
            },
        )

    if command == "capabilities":
        if not args.write:
            raise AgentToolError(
                code="INPUT_ERROR", message="Top1 capability refresh requires --write"
            )
        try:
            fee_plan = load_account_fee_plan_receipt(
                Path(args.fee_plan_receipt_path).expanduser()
            )
        except Top1CapabilityReceiptError as exc:
            raise AgentToolError(code=exc.reason_code, message=str(exc)) from exc
        top1 = context["top1"]
        binding = top1["opend_binding"]
        gateway = None
        try:
            gateway = build_ready_futu_quote_gateway(
                host=str(binding["host"]),
                port=int(binding["port"]),
                is_option_chain_cache_enabled=False,
            )
            receipt = refresh_top1_capability_receipt(
                context["artifact_root"],
                gateway=gateway,
                market=args.market.upper(),
                account=args.account,
                opend_binding=binding,
                account_fee_plan_receipt=fee_plan,
                stock_owner=args.stock_owner,
                contract_symbol=args.contract_symbol,
                terms_expiration=args.terms_expiration,
                close_expiration=args.close_expiration,
                observed_at_utc=datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            )
        except (Top1CapabilityReceiptError, FutuGatewayError) as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", getattr(exc, "code", "ERROR"))),
                message=str(exc),
            ) from exc
        finally:
            if gateway is not None:
                gateway.close()
        return build_response(
            tool_name="research.strategy-lab.top1-loop.capabilities.refresh",
            ok=True,
            data={
                "status": "published",
                "market": receipt["market"],
                "account": receipt["account"],
                "observed_at_utc": receipt["observed_at_utc"],
                "receipt_ref": receipt["receipt_ref"],
                "receipt_content_sha256": receipt["content_sha256"],
                "receipt_file_sha256": receipt["receipt_file_sha256"],
                "capabilities": capability_facts_from_receipt(receipt),
            },
        )

    store = ExperimentStore(context["store_path"])

    if command == "history":
        try:
            data = preview_archived_recommendation_point_migration(
                store,
                args.source_root,
                context["artifact_root"],
                market=args.market.upper(),
                account=args.account,
            )
        except CorpusError as exc:
            raise AgentToolError(code=exc.reason_code, message=str(exc)) from exc
        return build_response(
            tool_name="research.strategy-lab.top1-loop.history.migrate.preview",
            ok=True,
            data=data,
        )

    if command == "research":
        if args.top1_research_command == "start" and not args.write:
            raise AgentToolError(
                code="INPUT_ERROR", message="Top1 research start requires --write"
            )
        preview_inputs, config = _research_inputs(context, args)
        tool_name = (
            "research.strategy-lab.top1-loop.research."
            f"{args.top1_research_command}"
        )
        if args.top1_research_command == "preview":
            data = preview_sell_put_top1_research(
                context["artifact_root"], **preview_inputs
            )
            return build_response(
                tool_name=tool_name,
                ok=data["status"] in {"available", "blocked", "disabled", "unsupported"},
                data=data,
            )
        try:
            command_payload = json.loads(
                Path(args.confirmed_start_file).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentToolError(
                code="INPUT_ERROR", message="confirmed start file is unavailable or invalid"
            ) from exc
        binding = context["top1"]["opend_binding"]
        gateway = None
        try:
            gateway = build_ready_futu_quote_gateway(
                host=str(binding["host"]),
                port=int(binding["port"]),
                is_option_chain_cache_enabled=False,
            )
            data = start_confirmed_research(
                store,
                context["artifact_root"],
                confirmed_start=command_payload,
                gateway=gateway,
                config=config,
                **preview_inputs,
            )
        except (Top1WorkspaceError, FutuGatewayError) as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", getattr(exc, "code", "ERROR"))),
                message=str(exc),
            ) from exc
        finally:
            if gateway is not None:
                gateway.close()
        return build_response(tool_name=tool_name, ok=True, data=data)

    if command == "validation":
        if args.top1_validation_command == "start" and not args.write:
            raise AgentToolError(
                code="INPUT_ERROR", message="Top1 validation start requires --write"
            )
        if store.schema_state().get("status") != "ready":
            return _store_not_ready(
                "research.strategy-lab.top1-loop.validation", store
            )
        try:
            _config_path, config = load_runtime_config(
                config_path=context["config_hk"], expected_market="hk"
            )
            schedule = config.get("schedule")
            if not isinstance(schedule, Mapping):
                raise ValueError("HK runtime schedule is missing")
        except Exception as exc:
            raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc
        top1 = context["top1"]
        preview_inputs = {
            "experiment_id": args.experiment_id,
            "validation_start_trading_date": args.validation_start_trading_date,
            "schedule": schedule,
            "timer_binding": {
                "revision": ADVANCE_REVISION,
                "producer_catchup_grace_seconds": 30,
                "producer_run_timeout_upper_bound_seconds": int(
                    top1["timeout_start_sec"]
                ),
                "advance_cadence_seconds": int(top1["advance_interval"]),
                "fill_observation_duration_upper_bound_seconds": int(
                    top1["timeout_start_sec"]
                ),
                "terms_capture_duration_upper_bound_seconds": int(
                    top1["timeout_start_sec"]
                ),
            },
            "as_of_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "environ": None,
        }
        tool_name = (
            "research.strategy-lab.top1-loop.validation."
            f"{args.top1_validation_command}"
        )
        if args.top1_validation_command == "preview":
            data = preview_sell_put_top1_validation(
                store,
                context["artifact_root"],
                **preview_inputs,
            )
            return build_response(
                tool_name=tool_name,
                ok=data["status"] in {"available", "blocked", "disabled"},
                data=data,
            )
        try:
            command_payload = json.loads(
                Path(args.confirmed_start_file).expanduser().read_text(
                    encoding="utf-8"
                )
            )
            data = start_confirmed_validation(
                store,
                context["artifact_root"],
                confirmed_start=command_payload,
                **preview_inputs,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentToolError(
                code="INPUT_ERROR",
                message="confirmed start file is unavailable or invalid",
            ) from exc
        except Top1WorkspaceError as exc:
            raise AgentToolError(code=exc.reason_code, message=str(exc)) from exc
        return build_response(tool_name=tool_name, ok=True, data=data)

    if command == "readiness":
        return build_response(
            tool_name="research.strategy-lab.top1-loop.readiness",
            ok=True,
            data=_readiness(context, store),
        )

    if command == "status":
        if store.schema_state().get("status") != "ready":
            return _store_not_ready(
                "research.strategy-lab.top1-loop.status", store
            )
        try:
            data = read_public_status(
                store,
                experiment_id=args.experiment_id,
                expected_market=args.market.upper(),
                expected_account=args.account,
            )
        except Exception as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", "STATE_ERROR")),
                message=str(exc),
            ) from exc
        return build_response(
            tool_name="research.strategy-lab.top1-loop.status", ok=True, data=data
        )

    if command == "receipt":
        if store.schema_state().get("status") != "ready":
            return _store_not_ready(
                "research.strategy-lab.top1-loop.receipt", store
            )
        try:
            data = read_public_receipt(
                store,
                experiment_id=args.experiment_id,
            )
        except Exception as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", "STATE_ERROR")),
                message=str(exc),
            ) from exc
        return build_response(
            tool_name="research.strategy-lab.top1-loop.receipt", ok=True, data=data
        )

    if command != "advance":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported Top1 command: {command}")
    if not args.scheduled or not args.write:
        raise AgentToolError(
            code="INPUT_ERROR", message="Top1 advance requires --scheduled and --write"
        )

    occurred_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.migrate(migrated_at_utc=occurred_at_utc)
    top1 = context["top1"]
    gateway_box: dict[str, Any] = {}

    def load_schedule() -> Mapping[str, Any]:
        _path, config = load_runtime_config(
            config_path=context["config_hk"], expected_market="hk"
        )
        schedule = config.get("schedule")
        if not isinstance(schedule, Mapping):
            raise ValueError("HK runtime schedule is missing")
        return schedule

    def load_gateway() -> Any:
        binding = top1["opend_binding"]
        gateway = build_ready_futu_quote_gateway(
            host=str(binding["host"]),
            port=int(binding["port"]),
            is_option_chain_cache_enabled=False,
        )
        gateway_box["gateway"] = gateway
        return gateway

    idempotency_key = hashlib.sha256(
        f"{context['profile_path']}\0{occurred_at_utc}".encode()
    ).hexdigest()
    try:
        data = advance_scheduled(
            store,
            context["runtime_root"],
            context["artifact_root"],
            market="HK",
            account="lx",
            load_schedule=load_schedule,
            load_readiness=lambda: _readiness(context, store),
            load_gateway=load_gateway,
            advance_revision=ADVANCE_REVISION,
            advance_interval_seconds=int(top1["advance_interval"]),
            actor="strategy-lab-top1-scheduled",
            occurred_at_utc=occurred_at_utc,
            idempotency_key=idempotency_key,
        )
    finally:
        gateway = gateway_box.get("gateway")
        if gateway is not None:
            gateway.close()
    return build_response(
        tool_name="research.strategy-lab.top1-loop.advance",
        ok=data.get("status") in {"ok", "disabled"},
        data=data,
    )


__all__ = ["add_top1_commands", "handle_top1_command"]
