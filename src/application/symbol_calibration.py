from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from domain.domain.symbol_identity import SymbolIdentity, resolve_symbol_identity
from src.application.symbol_aliases import symbol_aliases_from_config


SymbolCalibrationStatus = Literal["ok", "unknown", "ambiguous", "conflict"]


@dataclass(frozen=True)
class SymbolCalibrationResult:
    raw_input: str
    canonical_symbol: str | None
    market: str | None
    currency: str | None
    futu_code: str | None
    source_kind: str
    changed: bool
    status: SymbolCalibrationStatus
    message: str

    def public_payload(self) -> dict[str, Any]:
        return asdict(self)


def calibrate_symbol(
    value: Any,
    *,
    config: Mapping[str, Any] | None = None,
    symbol_aliases: Mapping[str, Any] | None = None,
) -> SymbolCalibrationResult:
    raw = str(value or "").strip()
    if not raw:
        return SymbolCalibrationResult(raw, None, None, None, None, "empty", False, "unknown", "symbol is required")
    identity = resolve_symbol_identity(raw, symbol_aliases=_merged_aliases(config=config, symbol_aliases=symbol_aliases))
    if identity is None:
        return SymbolCalibrationResult(raw, None, None, None, None, "unknown", False, "unknown", f"无法识别监控标的：{raw}")
    return _result_from_identity(raw=raw, identity=identity)


def require_calibrated_symbol(
    value: Any,
    *,
    config: Mapping[str, Any] | None = None,
    symbol_aliases: Mapping[str, Any] | None = None,
    error_factory: Any = ValueError,
) -> SymbolCalibrationResult:
    result = calibrate_symbol(value, config=config, symbol_aliases=symbol_aliases)
    if result.status != "ok" or not result.canonical_symbol:
        raise error_factory(result.message)
    return result


def canonical_symbol_for_write(
    value: Any,
    *,
    config: Mapping[str, Any] | None = None,
    symbol_aliases: Mapping[str, Any] | None = None,
    error_factory: Any = ValueError,
) -> str:
    return str(
        require_calibrated_symbol(
            value,
            config=config,
            symbol_aliases=symbol_aliases,
            error_factory=error_factory,
        ).canonical_symbol
    )


def _merged_aliases(
    *,
    config: Mapping[str, Any] | None,
    symbol_aliases: Mapping[str, Any] | None,
) -> dict[str, Any]:
    aliases: dict[str, Any] = {}
    aliases.update(symbol_aliases_from_config(config))
    if isinstance(symbol_aliases, Mapping):
        for key, value in symbol_aliases.items():
            alias = str(key or "").strip()
            symbol = str(value or "").strip()
            if alias and symbol:
                aliases[alias.upper()] = symbol
    return aliases


def _result_from_identity(*, raw: str, identity: SymbolIdentity) -> SymbolCalibrationResult:
    canonical = identity.canonical
    source_kind = "hk_numeric" if identity.market == "HK" and raw.strip().isdigit() else identity.source_kind
    changed = raw != canonical or source_kind != "canonical"
    return SymbolCalibrationResult(
        raw_input=raw,
        canonical_symbol=canonical,
        market=identity.market,
        currency=identity.currency,
        futu_code=identity.futu_code,
        source_kind=source_kind,
        changed=changed,
        status="ok",
        message=("已校准监控标的" if changed else "监控标的已是标准格式"),
    )
