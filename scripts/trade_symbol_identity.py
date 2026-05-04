from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


OPTION_CODE_RE = re.compile(
    r"^(?P<market>[A-Z]{2})\.(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d+)$"
)
_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9\.-]{0,10}$")
REPO_ROOT = Path(__file__).resolve().parents[1]
_UNDERLIER_ALIAS_FALLBACKS = {
    "腾讯": "0700.HK",
    "腾讯控股": "0700.HK",
    "POP": "9992.HK",
    "泡泡玛特": "9992.HK",
    "美团": "3690.HK",
    "美团W": "3690.HK",
    "美团-W": "3690.HK",
    "中海油": "0883.HK",
    "中国海洋石油": "0883.HK",
}


@dataclass(frozen=True)
class SymbolIdentity:
    raw: str
    canonical: str
    market: str
    currency: str
    futu_code: str
    source_kind: str


def _load_runtime_symbol_aliases(base_dir: Path | None = None) -> dict[str, str]:
    root = Path(base_dir).resolve() if base_dir is not None else REPO_ROOT
    out: dict[str, str] = {}
    for name in ("config.us.json", "config.hk.json"):
        path = root / name
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        intake = cfg.get("intake") if isinstance(cfg, dict) else None
        aliases = intake.get("symbol_aliases") if isinstance(intake, dict) else None
        if not isinstance(aliases, dict):
            continue
        for alias, symbol in aliases.items():
            alias_key = str(alias or "").strip().upper()
            symbol_value = str(symbol or "").strip()
            if not alias_key or not symbol_value:
                continue
            out[alias_key] = symbol_value
    return out


def _normalize_hk_symbol(raw: str) -> str | None:
    upper = str(raw or "").strip().upper()
    if not upper:
        return None
    if upper.endswith(".HK"):
        num = upper[:-3]
        if num.isdigit():
            return f"{int(num):04d}.HK"
    if upper.startswith("HK."):
        num = upper[3:]
        if num.isdigit():
            return f"{int(num):04d}.HK"
    if upper.isdigit() and len(upper) <= 5:
        return f"{int(upper):04d}.HK"
    return None


def _display_name_candidates(raw: str) -> list[str]:
    out: list[str] = []
    text = str(raw or "").strip()
    if not text:
        return out

    token = re.split(r"[\s,，]+", text, maxsplit=1)[0].strip()
    if token:
        out.append(token)

    date_match = re.search(r"(?:20)?\d{6}", text)
    if date_match:
        prefix = text[: date_match.start()].strip(" -_/，,")
        if prefix:
            out.append(prefix)

    return list(dict.fromkeys(out))


def _identity_from_canonical(*, raw: str, candidate: str, source_kind: str) -> SymbolIdentity | None:
    upper = str(candidate or "").strip().upper()
    if not upper:
        return None

    hk = _normalize_hk_symbol(upper)
    if hk:
        num = hk[:-3].zfill(5)
        return SymbolIdentity(
            raw=raw,
            canonical=hk,
            market="HK",
            currency="HKD",
            futu_code=f"HK.{num}",
            source_kind=source_kind,
        )

    if upper.startswith("SH.") or upper.startswith("SZ."):
        prefix = upper[:2]
        num = upper[3:]
        if num:
            return SymbolIdentity(
                raw=raw,
                canonical=f"{prefix}.{num}",
                market="CN",
                currency="CNY",
                futu_code=f"{prefix}.{num}",
                source_kind=source_kind,
            )

    if _US_SYMBOL_RE.fullmatch(upper):
        return SymbolIdentity(
            raw=raw,
            canonical=upper,
            market="US",
            currency="USD",
            futu_code=f"US.{upper}",
            source_kind=source_kind,
        )
    return None


def _identity_from_alias(*, raw: str, candidate: str, base_dir: Path | None, source_kind: str) -> SymbolIdentity | None:
    aliases = _load_runtime_symbol_aliases(base_dir=base_dir)
    alias_key = str(candidate or "").strip().upper()
    mapped = aliases.get(alias_key) or _UNDERLIER_ALIAS_FALLBACKS.get(candidate) or _UNDERLIER_ALIAS_FALLBACKS.get(alias_key)
    if not mapped:
        return None
    return _identity_from_canonical(raw=raw, candidate=str(mapped), source_kind=source_kind)


def resolve_symbol_identity(value: Any, *, base_dir: Path | None = None) -> SymbolIdentity | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    option_code_match = OPTION_CODE_RE.match(upper)
    if option_code_match:
        root = option_code_match.group("root")
        return (
            _identity_from_alias(raw=raw, candidate=root, base_dir=base_dir, source_kind="option_code")
            or _identity_from_canonical(raw=raw, candidate=root, source_kind="option_code")
        )
    if upper.startswith("US."):
        return (
            _identity_from_alias(raw=raw, candidate=upper[3:], base_dir=base_dir, source_kind="futu_code")
            or _identity_from_canonical(raw=raw, candidate=upper[3:], source_kind="futu_code")
        )
    if upper.startswith("HK."):
        digits = "".join(ch for ch in upper[3:] if ch.isdigit())
        if digits:
            return _identity_from_canonical(raw=raw, candidate=f"{int(digits):04d}.HK", source_kind="futu_code")
        return None

    if upper.endswith(".US"):
        return (
            _identity_from_alias(raw=raw, candidate=upper[:-3], base_dir=base_dir, source_kind="market_suffix")
            or _identity_from_canonical(raw=raw, candidate=upper[:-3], source_kind="market_suffix")
        )

    alias = _identity_from_alias(raw=raw, candidate=raw, base_dir=base_dir, source_kind="alias")
    if alias:
        return alias

    direct = _identity_from_canonical(raw=raw, candidate=raw, source_kind="canonical")
    if direct:
        return direct

    for candidate in _display_name_candidates(raw):
        alias = _identity_from_alias(raw=raw, candidate=candidate, base_dir=base_dir, source_kind="display_name")
        if alias:
            return alias
    return None


def canonical_symbol(value: Any, *, base_dir: Path | None = None) -> str | None:
    identity = resolve_symbol_identity(value, base_dir=base_dir)
    return identity.canonical if identity else None


def futu_underlier_code(value: Any, *, base_dir: Path | None = None) -> str | None:
    identity = resolve_symbol_identity(value, base_dir=base_dir)
    return identity.futu_code if identity else None


def symbol_market(value: Any, *, base_dir: Path | None = None) -> str | None:
    identity = resolve_symbol_identity(value, base_dir=base_dir)
    return identity.market if identity else None


def symbol_currency(value: Any, *, base_dir: Path | None = None) -> str | None:
    identity = resolve_symbol_identity(value, base_dir=base_dir)
    return identity.currency if identity else None


def is_hk_symbol(value: Any, *, base_dir: Path | None = None) -> bool:
    return symbol_market(value, base_dir=base_dir) == "HK"


def canonical_symbol_aliases(value: Any, *, base_dir: Path | None = None) -> list[str]:
    identity = resolve_symbol_identity(value, base_dir=base_dir)
    if identity is None:
        raw = str(value or "").strip().upper()
        return [raw] if raw else []
    out = [identity.canonical]
    if identity.market == "HK":
        code = identity.canonical[:-3]
        if code.isdigit():
            out.append(f"{int(code):05d}.HK")
    return list(dict.fromkeys(out))


def resolve_underlier_alias(symbol: str, *, base_dir: Path | None = None) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    return canonical_symbol(raw, base_dir=base_dir) or raw.upper()


def normalize_symbol_candidate(value: Any) -> str | None:
    return canonical_symbol(value)


def pick_first_normalized_symbol(src: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = normalize_symbol_candidate(src.get(key))
        if value:
            return value
    return None
