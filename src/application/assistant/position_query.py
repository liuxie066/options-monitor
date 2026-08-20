from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import re
from typing import Any, Literal, cast

from domain.domain.symbol_identity import canonical_symbol
from src.application.account_config import normalize_account_label, normalize_accounts
from src.application.agent_tool_contracts import AgentToolError
from src.application.payload_helpers import optional_text as _optional_text


PositionStatus = Literal["open", "close", "all"]
OptionType = Literal["put", "call"]
PositionSide = Literal["short", "long"]

_DEFAULT_QUERY_ACCOUNTS = ("lx", "sy")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])(?!\d)")
_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)")
_MONTH_CN_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月")
_WITHIN_DAYS_RE = re.compile(r"(?<!\d)(\d{1,3})\s*天(?:内|以内)")
_STRIKE_RE = re.compile(r"(?:strike|行权价)\s*[=:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_LIMIT_RE = re.compile(r"(?:limit|前)\s*(\d{1,3})\s*(?:条|个)?", re.IGNORECASE)
_SYMBOL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})(?![A-Za-z0-9_.])"
)
_CN_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}
_SYMBOL_STOP_WORDS = {
    "all",
    "call",
    "put",
    "short",
    "long",
    "open",
    "closed",
    "close",
    "positions",
    "position",
    "exp",
    "expiry",
    "limit",
    "lx",
    "sy",
    "持仓",
    "到期",
    "本月",
    "全部",
    "当前",
    "已平仓",
    "平仓",
}


@dataclass(frozen=True)
class PositionExpirationQuery:
    exact: str | None = None
    month: str | None = None
    before: str | None = None
    after: str | None = None
    within_days: int | None = None

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.exact:
            out["exact"] = self.exact
        if self.month:
            out["month"] = self.month
        if self.before:
            out["before"] = self.before
        if self.after:
            out["after"] = self.after
        if self.within_days is not None:
            out["within_days"] = int(self.within_days)
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> PositionExpirationQuery:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise AgentToolError(code="INPUT_ERROR", message="position expiration query must be an object")
        within_days = payload.get("within_days")
        return cls(
            exact=_optional_text(payload.get("exact")),
            month=_optional_text(payload.get("month")),
            before=_optional_text(payload.get("before")),
            after=_optional_text(payload.get("after")),
            within_days=_optional_int(within_days) if within_days not in (None, "") else None,
        )


@dataclass(frozen=True)
class PositionQuery:
    account: str | None = None
    status: PositionStatus = "open"
    symbol: str | None = None
    option_type: OptionType | None = None
    side: PositionSide | None = None
    strike: float | None = None
    expiration: PositionExpirationQuery = field(default_factory=PositionExpirationQuery)
    limit: int = 50

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "limit": int(self.limit),
        }
        if self.account:
            out["account"] = self.account
        if self.symbol:
            out["symbol"] = self.symbol
        if self.option_type:
            out["option_type"] = self.option_type
        if self.side:
            out["side"] = self.side
        if self.strike is not None:
            out["strike"] = float(self.strike)
        expiration = self.expiration.to_payload()
        if expiration:
            out["expiration"] = expiration
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> PositionQuery:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise AgentToolError(code="INPUT_ERROR", message="position query must be an object")
        expiration_payload = payload.get("expiration")
        expiration = PositionExpirationQuery.from_payload(expiration_payload)
        return cls(
            account=_normalize_account(payload.get("account")),
            status=_normalize_status(payload.get("status")),
            symbol=_normalize_symbol(payload.get("symbol")),
            option_type=_normalize_option_type(payload.get("option_type")),
            side=_normalize_side(payload.get("side")),
            strike=_optional_float(payload.get("strike")) if payload.get("strike") not in (None, "") else None,
            expiration=expiration,
            limit=_normalize_limit(payload.get("limit")),
        )


def parse_position_query_text(
    text: str,
    *,
    today: date,
    accounts: list[str] | tuple[str, ...] | None = None,
) -> PositionQuery:
    raw = str(text or "").strip()
    compact = re.sub(r"\s+", "", raw.lower())
    lower = raw.lower()
    account_values = normalize_accounts(accounts, fallback=_DEFAULT_QUERY_ACCOUNTS)
    expiration = _parse_expiration(raw, compact=compact, today=today)
    return PositionQuery(
        account=_extract_account(raw, accounts=account_values),
        status=_parse_status(compact, lower),
        symbol=_extract_symbol(raw, accounts=account_values),
        option_type=_parse_option_type(compact, lower),
        side=_parse_side(compact, lower),
        strike=_parse_strike(raw),
        expiration=expiration,
        limit=_parse_limit(raw),
    )


def position_query_intent_arguments(query: PositionQuery) -> dict[str, Any]:
    return query.to_payload()


def _extract_account(text: str, *, accounts: list[str]) -> str | None:
    match = _account_pattern(accounts).search(text)
    return match.group(1).lower() if match else None


def _account_pattern(accounts: list[str]) -> re.Pattern[str]:
    return re.compile(
        r"(?<![a-z0-9_-])(" + "|".join(re.escape(account) for account in accounts) + r")(?![a-z0-9_-])",
        re.IGNORECASE,
    )


def _parse_status(compact: str, lower: str) -> PositionStatus:
    if "全部" in compact or re.search(r"(?<![a-z0-9_])all(?![a-z0-9_])", lower):
        return "all"
    if "已平仓" in compact or "closed" in lower or "close" in lower:
        return "close"
    return "open"


def _parse_option_type(compact: str, lower: str) -> OptionType | None:
    if "call" in lower or "购" in compact:
        return "call"
    if "put" in lower or "沽" in compact:
        return "put"
    return None


def _parse_side(compact: str, lower: str) -> PositionSide | None:
    if "short" in lower or "卖" in compact or "空" in compact:
        return "short"
    if "long" in lower or "买" in compact or "多" in compact:
        return "long"
    return None


def _parse_strike(text: str) -> float | None:
    match = _STRIKE_RE.search(text)
    if not match:
        return None
    return float(match.group(1))


def _parse_limit(text: str) -> int:
    match = _LIMIT_RE.search(text)
    if not match:
        return 50
    return max(1, min(int(match.group(1)), 500))


def _parse_expiration(text: str, *, compact: str, today: date) -> PositionExpirationQuery:
    within_days = _parse_within_days(text)
    exact_match = _DATE_RE.search(text)
    exact = (
        f"{int(exact_match.group(1)):04d}-{int(exact_match.group(2)):02d}-{int(exact_match.group(3)):02d}"
        if exact_match
        else None
    )
    month = _parse_expiration_month(text, compact=compact, today=today)
    before = _parse_expiration_before(text, compact=compact, today=today, month=month, exact=exact)
    return PositionExpirationQuery(
        exact=exact if not before else None,
        month=month if not before and exact is None else None,
        before=before,
        within_days=within_days,
    )


def _parse_within_days(text: str) -> int | None:
    match = _WITHIN_DAYS_RE.search(text)
    if not match:
        return None
    return max(1, min(int(match.group(1)), 365))


def _parse_expiration_month(text: str, *, compact: str, today: date) -> str | None:
    if "本月" in compact:
        return f"{today.year:04d}-{today.month:02d}"
    match = _MONTH_RE.search(text)
    if match:
        year, month = match.groups()
        return f"{int(year):04d}-{int(month):02d}"
    cn_match = _MONTH_CN_RE.search(text)
    if not cn_match:
        return None
    raw_month = cn_match.group(1)
    month = _CN_MONTHS.get(raw_month) if not raw_month.isdigit() else int(raw_month)
    if month is None or month < 1 or month > 12:
        return None
    return f"{today.year:04d}-{month:02d}"


def _parse_expiration_before(
    text: str,
    *,
    compact: str,
    today: date,
    month: str | None,
    exact: str | None,
) -> str | None:
    if "前" not in compact and "before" not in text.lower():
        return None
    if exact:
        return exact
    if "月底前" in compact or "月末前" in compact or "底前" in compact:
        target_month = month or f"{today.year:04d}-{today.month:02d}"
        year, month_num = (int(part) for part in target_month.split("-", 1))
        return _last_day_of_month(year, month_num).isoformat()
    return None


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _extract_symbol(text: str, *, accounts: list[str]) -> str | None:
    text_without_accounts = _account_pattern(accounts).sub(" ", text)
    for match in _SYMBOL_RE.finditer(text_without_accounts):
        raw = match.group(1).strip()
        if not raw or raw.lower() in _SYMBOL_STOP_WORDS or raw in _SYMBOL_STOP_WORDS:
            continue
        before = text_without_accounts[match.start() - 1] if match.start() > 0 else ""
        after = text_without_accounts[match.end()] if match.end() < len(text_without_accounts) else ""
        if raw.isdigit() and (before in {"-", "/", "."} or after in {"-", "/", "."}):
            continue
        if raw.isdigit() and len(raw) < 3:
            continue
        resolved = canonical_symbol(raw)
        if resolved:
            return resolved
    return None


def _normalize_account(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return normalize_account_label(text)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"position query account is invalid: {exc}") from exc


def _normalize_status(value: Any) -> PositionStatus:
    text = str(value or "open").strip().lower()
    if text == "closed":
        text = "close"
    if text not in {"open", "close", "all"}:
        raise AgentToolError(code="INPUT_ERROR", message="position query status must be open, close, or all")
    return cast(PositionStatus, text)


def _normalize_symbol(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    return canonical_symbol(text) or text.upper()


def _normalize_option_type(value: Any) -> OptionType | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized not in {"put", "call"}:
        raise AgentToolError(code="INPUT_ERROR", message="position query option_type must be put or call")
    return cast(OptionType, normalized)


def _normalize_side(value: Any) -> PositionSide | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized not in {"short", "long"}:
        raise AgentToolError(code="INPUT_ERROR", message="position query side must be short or long")
    return cast(PositionSide, normalized)


def _normalize_limit(value: Any) -> int:
    if value in (None, ""):
        return 50
    return max(1, min(int(value), 500))


def _optional_int(value: Any) -> int:
    try:
        return int(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"expected integer value, got: {value}") from exc


def _optional_float(value: Any) -> float:
    try:
        return float(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"expected numeric value, got: {value}") from exc


__all__ = [
    "PositionExpirationQuery",
    "PositionQuery",
    "parse_position_query_text",
    "position_query_intent_arguments",
]
