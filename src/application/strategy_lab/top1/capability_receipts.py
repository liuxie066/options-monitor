from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.fill_observation import (
    _price,
    _snapshot_by_code,
)
from src.application.strategy_lab.top1.readiness import CAPABILITY_FACTS
from src.infrastructure.private_storage import (
    atomic_write_private_text,
    open_private_text,
    private_path,
)


ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA = "sell_put_top1_account_fee_plan_receipt.v1"
CAPABILITY_RECEIPT_SCHEMA = "sell_put_top1_w0r_capability_receipt.v1"
MAX_CAPABILITY_RECEIPT_BYTES = 8 * 1024

_FEE_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "commission_free",
        "platform_fee",
        "fee_plan_ref",
        "observed_at_utc",
        "evidence_ref",
        "evidence_sha256",
    }
)
_RECORDED_FEE_PLAN_FIELDS = _FEE_PLAN_FIELDS | {"source_receipt_sha256"}
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "observed_at_utc",
        "opend_binding",
        *CAPABILITY_FACTS,
        "content_sha256",
    }
)
_TERMS_FIELDS = frozenset(
    {
        "contract_symbol",
        "stock_owner",
        "expiration",
        "option_type",
        "option_standard_type",
        "strike",
        "multiplier",
        "currency",
    }
)
_HASH = frozenset("0123456789abcdef")


class Top1CapabilityReceiptError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise Top1CapabilityReceiptError(reason_code, message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("top1_capability_input_invalid", f"{label} must be canonical text")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or set(text) - _HASH:
        _fail("top1_capability_input_invalid", f"{label} must be a lowercase SHA-256")
    return text


def _timestamp(value: object, label: str) -> str:
    try:
        normalized = utc_timestamp(value, label)
    except CandidateSnapshotContractError as exc:
        raise Top1CapabilityReceiptError("top1_capability_input_invalid", str(exc)) from exc
    if value != normalized:
        _fail("top1_capability_input_invalid", f"{label} must be canonical UTC")
    return normalized


def _expiration(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise Top1CapabilityReceiptError("top1_capability_input_invalid", f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        _fail("top1_capability_input_invalid", f"{label} must use YYYY-MM-DD")
    return text


def _nonnegative_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        _fail("top1_capability_input_invalid", f"{label} must be non-negative")
    return float(value)


def _positive_number(value: object, label: str) -> float:
    result = _nonnegative_number(value, label)
    if result <= 0:
        _fail("top1_capability_input_invalid", f"{label} must be positive")
    return result


def _identity(market: object, account: object) -> tuple[str, str]:
    if market != "HK" or account != "lx":
        _fail("top1_capability_input_invalid", "capability identity must equal HK/lx")
    return "HK", "lx"


def _opend_binding(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"host", "port"}:
        _fail("top1_capability_input_invalid", "OpenD binding is invalid")
    host = _text(value["host"], "opend_binding.host")
    port = value["port"]
    if type(port) is not int or not 0 < port <= 65535:
        _fail("top1_capability_input_invalid", "opend_binding.port is invalid")
    return {"host": host, "port": port}


def _fee_plan_receipt(value: object, *, recorded: bool) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail("top1_capability_input_invalid", "account fee-plan receipt must be an object")
    item = dict(value)
    expected = _RECORDED_FEE_PLAN_FIELDS if recorded else _FEE_PLAN_FIELDS
    if set(item) != expected or item.get("schema_version") != ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA:
        _fail("top1_capability_input_invalid", "account fee-plan receipt schema is invalid")
    _identity(item.get("market"), item.get("account"))
    if type(item.get("commission_free")) is not bool:
        _fail("top1_capability_input_invalid", "commission_free must be boolean")
    normalized: dict[str, object] = {
        "schema_version": ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
        "market": "HK",
        "account": "lx",
        "commission_free": item["commission_free"],
        "platform_fee": _nonnegative_number(item.get("platform_fee"), "platform_fee"),
        "fee_plan_ref": _text(item.get("fee_plan_ref"), "fee_plan_ref"),
        "observed_at_utc": _timestamp(item.get("observed_at_utc"), "observed_at_utc"),
        "evidence_ref": _text(item.get("evidence_ref"), "evidence_ref"),
        "evidence_sha256": _sha256(item.get("evidence_sha256"), "evidence_sha256"),
    }
    source_hash = canonical_sha256(normalized)
    if recorded and _sha256(item.get("source_receipt_sha256"), "source_receipt_sha256") != source_hash:
        _fail("top1_capability_input_invalid", "account fee-plan receipt hash changed")
    return {**normalized, "source_receipt_sha256": source_hash}


def load_account_fee_plan_receipt(path: str | Path) -> dict[str, object]:
    try:
        with open_private_text(private_path(path)) as handle:
            content = handle.read(MAX_CAPABILITY_RECEIPT_BYTES + 1)
        if len(content.encode("utf-8")) > MAX_CAPABILITY_RECEIPT_BYTES:
            raise ValueError("account fee-plan receipt is too large")
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Top1CapabilityReceiptError(
            "account_fee_plan_receipt_unavailable",
            "account fee-plan receipt cannot be read",
        ) from exc
    return _fee_plan_receipt(payload, recorded=False)


def _capability_ref(market: str, account: str) -> str:
    return f"strategy_lab/top1/capabilities/w0r/{market.lower()}/{account}/current.json"


def _provider_receipts(
    gateway: Any,
    *,
    stock_owner: str,
    contract_symbol: str,
    terms_expiration: str,
    close_expiration: str,
) -> dict[str, object]:
    try:
        rows, reason = _snapshot_by_code(gateway.get_snapshot([contract_symbol]), [contract_symbol])
    except Exception as exc:
        raise Top1CapabilityReceiptError(
            "quote_observation_receipt_unavailable",
            "quote observation capability probe failed",
        ) from exc
    quote = rows.get(contract_symbol)
    bid = _price(quote.get("bid_price", quote.get("bid"))) if quote else None
    ask = _price(quote.get("ask_price", quote.get("ask"))) if quote else None
    if reason is not None or bid is None or bid < 0 or ask is None or ask <= 0 or ask < bid:
        _fail(
            "quote_observation_receipt_unavailable",
            "quote observation capability receipt is invalid",
        )

    try:
        terms_raw = gateway.get_exact_expiration_option_terms(
            code=stock_owner,
            expiration=terms_expiration,
            contract_symbol=contract_symbol,
        )
    except Exception as exc:
        raise Top1CapabilityReceiptError(
            "exact_expiration_terms_receipt_unavailable",
            "exact-expiration terms capability probe failed",
        ) from exc
    if not isinstance(terms_raw, Mapping):
        _fail(
            "exact_expiration_terms_receipt_unavailable",
            "exact-expiration terms capability receipt is invalid",
        )
    terms = dict(terms_raw)

    try:
        quota_raw = gateway.get_history_kl_quota()
    except Exception as exc:
        raise Top1CapabilityReceiptError(
            "history_kline_quota_receipt_unavailable",
            "history K-line quota capability probe failed",
        ) from exc
    if not isinstance(quota_raw, Mapping):
        _fail(
            "history_kline_quota_receipt_unavailable",
            "history K-line quota capability receipt is invalid",
        )
    used = quota_raw.get("used_quota")
    remaining = quota_raw.get("remain_quota")
    details = quota_raw.get("detail_list")
    if not isinstance(details, list):
        _fail(
            "history_kline_quota_receipt_unavailable",
            "history K-line quota capability facts are invalid",
        )

    try:
        close_raw = gateway.get_exact_expiration_close(
            code=stock_owner,
            expiration=close_expiration,
        )
    except Exception as exc:
        raise Top1CapabilityReceiptError(
            "exact_expiration_close_receipt_unavailable",
            "exact-expiration close capability probe failed",
        ) from exc
    if not isinstance(close_raw, Mapping):
        _fail(
            "exact_expiration_close_receipt_unavailable",
            "exact-expiration close capability receipt is invalid",
        )

    return {
        "quote_observation_receipt": {
            "contract_symbol": contract_symbol,
            "bid": bid,
            "ask": ask,
        },
        "exact_expiration_terms_receipt": terms,
        "history_kline_quota_receipt": {
            "used_quota": used,
            "remain_quota": remaining,
            "detail_count": len(details),
            "detail_sha256": canonical_sha256(details),
        },
        "exact_expiration_close_receipt": {key: close_raw.get(key) for key in ("code", "expiration", "close")},
    }


def refresh_top1_capability_receipt(
    artifact_root: str | Path,
    *,
    gateway: Any,
    market: str,
    account: str,
    opend_binding: Mapping[str, object],
    account_fee_plan_receipt: Mapping[str, object],
    stock_owner: str,
    contract_symbol: str,
    terms_expiration: str,
    close_expiration: str,
    observed_at_utc: str,
) -> dict[str, object]:
    """Run one explicit compact W0R probe and replace only its current receipt."""

    market, account = _identity(market, account)
    binding = _opend_binding(opend_binding)
    fee_plan = _fee_plan_receipt(account_fee_plan_receipt, recorded=True)
    observed_at = _timestamp(observed_at_utc, "observed_at_utc")
    owner_identity = resolve_symbol_identity(stock_owner)
    if (
        owner_identity is None
        or owner_identity.market != "HK"
        or owner_identity.futu_code != str(stock_owner).strip().upper()
    ):
        _fail("top1_capability_input_invalid", "stock_owner must be a canonical HK Futu code")
    owner = owner_identity.futu_code
    contract = str(contract_symbol).strip().upper()
    match = OPTION_CODE_RE.fullmatch(contract)
    if match is None or match.group("market") != "HK" or match.group("cp") != "P":
        _fail("top1_capability_input_invalid", "contract_symbol must be a canonical HK PUT code")
    terms_date = _expiration(terms_expiration, "terms_expiration")
    close_date = _expiration(close_expiration, "close_expiration")
    receipts = _provider_receipts(
        gateway,
        stock_owner=owner,
        contract_symbol=contract,
        terms_expiration=terms_date,
        close_expiration=close_date,
    )
    payload: dict[str, object] = {
        "schema_version": CAPABILITY_RECEIPT_SCHEMA,
        "market": market,
        "account": account,
        "observed_at_utc": observed_at,
        "opend_binding": binding,
        "account_fee_plan_receipt": fee_plan,
        **receipts,
    }
    try:
        _validate_recorded_provider_receipts(payload)
    except ValueError as exc:
        raise Top1CapabilityReceiptError(
            "top1_capability_receipt_conflict",
            "provider capability receipt is invalid",
        ) from exc
    payload["content_sha256"] = canonical_sha256(payload)
    content = render_json_text(payload)
    if len(content.encode("utf-8")) > MAX_CAPABILITY_RECEIPT_BYTES:
        _fail("top1_capability_receipt_conflict", "capability receipt exceeds 8 KiB")
    ref = _capability_ref(market, account)
    target = private_path(artifact_root).joinpath(*ref.split("/"))
    try:
        # ponytail: refresh is operator-serialized; add an account lock only if concurrent refresh is supported.
        atomic_write_private_text(target, content)
        return read_top1_capability_receipt(
            artifact_root,
            market=market,
            account=account,
            expected_opend_binding=binding,
        )
    except (OSError, ValueError) as exc:
        raise Top1CapabilityReceiptError(
            "top1_capability_receipt_conflict",
            "capability receipt cannot be published",
        ) from exc


def read_top1_capability_receipt(
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    expected_opend_binding: Mapping[str, object],
) -> dict[str, object]:
    """Read and verify the current receipt without probing a provider."""

    market, account = _identity(market, account)
    expected_binding = _opend_binding(expected_opend_binding)
    ref = _capability_ref(market, account)
    path = private_path(artifact_root).joinpath(*ref.split("/"))
    try:
        with open_private_text(path) as handle:
            content = handle.read(MAX_CAPABILITY_RECEIPT_BYTES + 1)
        if len(content.encode("utf-8")) > MAX_CAPABILITY_RECEIPT_BYTES:
            raise ValueError("receipt is too large")
        payload = json.loads(content)
        if not isinstance(payload, dict) or content != render_json_text(payload):
            raise ValueError("receipt bytes are not canonical")
        if set(payload) != _RECEIPT_FIELDS:
            raise ValueError("receipt fields are invalid")
        if payload.get("schema_version") != CAPABILITY_RECEIPT_SCHEMA:
            raise ValueError("receipt schema is invalid")
        _identity(payload.get("market"), payload.get("account"))
        _timestamp(payload.get("observed_at_utc"), "observed_at_utc")
        if _opend_binding(payload.get("opend_binding")) != expected_binding:
            raise ValueError("OpenD binding changed")
        _fee_plan_receipt(payload.get("account_fee_plan_receipt"), recorded=True)
        expected_hash = canonical_sha256({key: value for key, value in payload.items() if key != "content_sha256"})
        if _sha256(payload.get("content_sha256"), "content_sha256") != expected_hash:
            raise ValueError("receipt content hash changed")
        validated = _validate_recorded_provider_receipts(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Top1CapabilityReceiptError(
            "top1_capability_receipt_unavailable",
            "current capability receipt is unavailable or invalid",
        ) from exc
    return {
        **validated,
        "receipt_ref": ref,
        "receipt_file_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _validate_recorded_provider_receipts(payload: Mapping[str, object]) -> dict[str, object]:
    quote = payload.get("quote_observation_receipt")
    terms = payload.get("exact_expiration_terms_receipt")
    quota = payload.get("history_kline_quota_receipt")
    close = payload.get("exact_expiration_close_receipt")
    if not isinstance(quote, Mapping) or set(quote) != {"contract_symbol", "bid", "ask"}:
        raise ValueError("quote receipt is invalid")
    contract = _text(quote.get("contract_symbol"), "contract_symbol")
    match = OPTION_CODE_RE.fullmatch(contract)
    if match is None or match.group("market") != "HK" or match.group("cp") != "P":
        raise ValueError("quote contract is invalid")
    bid = _nonnegative_number(quote.get("bid"), "bid")
    ask = _positive_number(quote.get("ask"), "ask")
    if ask < bid:
        raise ValueError("quote spread is invalid")
    if not isinstance(terms, Mapping) or set(terms) != _TERMS_FIELDS:
        raise ValueError("terms receipt is invalid")
    if terms.get("contract_symbol") != contract:
        raise ValueError("terms contract changed")
    owner = resolve_symbol_identity(terms.get("stock_owner"))
    if owner is None or owner.market != "HK" or owner.futu_code != terms.get("stock_owner"):
        raise ValueError("terms stock owner is invalid")
    _expiration(terms.get("expiration"), "terms.expiration")
    _positive_number(terms.get("strike"), "terms.strike")
    if (
        terms.get("option_type") != "PUT"
        or terms.get("option_standard_type") != "STANDARD"
        or type(terms.get("multiplier")) is not int
        or int(terms["multiplier"]) <= 0
        or terms.get("currency") != "HKD"
    ):
        raise ValueError("terms facts are invalid")
    if not isinstance(quota, Mapping) or set(quota) != {
        "used_quota",
        "remain_quota",
        "detail_count",
        "detail_sha256",
    }:
        raise ValueError("quota receipt is invalid")
    if (
        type(quota.get("used_quota")) is not int
        or int(quota["used_quota"]) < 0
        or type(quota.get("remain_quota")) is not int
        or int(quota["remain_quota"]) < 0
        or quota.get("detail_count") != quota.get("used_quota")
    ):
        raise ValueError("quota facts are invalid")
    _sha256(quota.get("detail_sha256"), "detail_sha256")
    if not isinstance(close, Mapping) or set(close) != {"code", "expiration", "close"}:
        raise ValueError("close receipt is invalid")
    if close.get("code") != terms.get("stock_owner"):
        raise ValueError("close owner changed")
    _expiration(close.get("expiration"), "close.expiration")
    _positive_number(close.get("close"), "close")
    return dict(payload)


def capability_facts_from_receipt(receipt: Mapping[str, object]) -> dict[str, bool]:
    return {name: isinstance(receipt.get(name), Mapping) for name in CAPABILITY_FACTS}


__all__ = [
    "ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA",
    "CAPABILITY_FACTS",
    "CAPABILITY_RECEIPT_SCHEMA",
    "MAX_CAPABILITY_RECEIPT_BYTES",
    "Top1CapabilityReceiptError",
    "capability_facts_from_receipt",
    "load_account_fee_plan_receipt",
    "read_top1_capability_receipt",
    "refresh_top1_capability_receipt",
]
