from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.fetch_market_data_opend import fetch_symbol, save_outputs


@dataclass(frozen=True)
class RequiredDataFetchRequest:
    symbol: str
    limit_expirations: int
    host: str = "127.0.0.1"
    port: int = 11111
    output_root: Path | None = None
    option_types: str = "put,call"
    min_strike: float | None = None
    max_strike: float | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    chain_cache: bool = True
    chain_cache_force_refresh: bool = False


def fetch_required_data_opend(*, base: Path, request: RequiredDataFetchRequest) -> tuple[Path, Path]:
    payload = fetch_symbol(
        request.symbol,
        limit_expirations=int(request.limit_expirations),
        host=str(request.host),
        port=int(request.port),
        base_dir=Path(base),
        chain_cache=bool(request.chain_cache),
        chain_cache_force_refresh=bool(request.chain_cache_force_refresh),
        option_types=str(request.option_types),
        min_strike=request.min_strike,
        max_strike=request.max_strike,
        min_dte=request.min_dte,
        max_dte=request.max_dte,
    )
    return save_outputs(
        Path(base),
        str(request.symbol),
        payload,
        output_root=(Path(request.output_root) if request.output_root is not None else None),
    )
