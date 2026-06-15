"""yfinance-backed data source: the swappable fallback to IBKR historical data.

This implements the DataSource protocol. Historical requests are served from the
DiskCache when one is provided, so repeated backtests do not refetch. Quotes are
live and never cached.

No-lookahead: yfinance daily bars are right-labeled and only fully formed at the
close, consistent with the contract in data.base. The caller still owns warmup
dropping and the next-bar execution rule.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from data.base import DataSource, DateLike, Quote, normalize_bars
from data.cache import DiskCache

# Map IB-style bar size strings to yfinance interval codes.
_BAR_SIZE_TO_INTERVAL: dict[str, str] = {
    "1 min": "1m",
    "2 mins": "2m",
    "5 mins": "5m",
    "15 mins": "15m",
    "30 mins": "30m",
    "1 hour": "1h",
    "1 day": "1d",
    "1 week": "1wk",
    "1 month": "1mo",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class YFinanceDataSource(DataSource):
    """Fallback market-data source built on yfinance."""

    SOURCE = "yfinance"

    def __init__(self, cache: Optional[DiskCache] = None) -> None:
        """Create the source.

        Args:
            cache: Optional DiskCache for historical bars. If None, every
                historical request hits the network.
        """
        self.cache = cache

    def get_historical_bars(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        bar_size: str = "1 day",
    ) -> pd.DataFrame:
        """Return normalized OHLCV bars for symbol, using the cache when present."""
        interval = _BAR_SIZE_TO_INTERVAL.get(bar_size, "1d")

        key: Optional[str] = None
        if self.cache is not None:
            key = DiskCache.make_key(self.SOURCE, symbol, start, end, bar_size)
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        import yfinance as yf

        raw = yf.download(
            symbol,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            progress=False,
        )
        bars = normalize_bars(raw)

        if self.cache is not None and key is not None and len(bars) > 0:
            self.cache.set(key, bars)
        return bars

    def get_quote(self, symbol: str) -> Quote:
        """Return the latest available quote for symbol (live, not cached)."""
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        bid = ask = last = None
        try:
            info = ticker.fast_info
            last = getattr(info, "last_price", None)
            bid = getattr(info, "bid", None)
            ask = getattr(info, "ask", None)
        except Exception:
            # fast_info can fail offline or for unknown symbols; return a quote
            # with whatever was obtained rather than raising.
            pass

        return Quote(
            symbol=symbol,
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            last=float(last) if last is not None else None,
            ts_utc=_utc_now_iso(),
        )
