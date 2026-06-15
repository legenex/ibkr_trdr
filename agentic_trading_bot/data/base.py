"""Common data interface for historical bars and quotes.

No-lookahead contract (this is enforced by convention here and by tests in the
backtest stage; read it before implementing a source):

  - A historical bar timestamped t is RIGHT-labeled: it represents the interval
    ENDING at t and is only fully known at the bar's close (time t). Anything
    that computes a signal from a bar must therefore act no earlier than the
    NEXT bar (trade the next bar's open), never the same bar's close, unless
    close execution is explicitly modeled and justified.
  - Returned frames contain only COMPLETED bars. A source must not return a
    partially formed, still-updating bar.
  - Indicator warmup is the caller's responsibility, and warmup rows are dropped,
    never backfilled.
  - The index is a timezone-aware UTC DatetimeIndex, sorted ascending, with no
    duplicate timestamps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, Union, runtime_checkable

import pandas as pd
from pydantic import BaseModel

# Canonical, lowercase OHLCV column names every source normalizes to.
BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]

# A start/end argument may be a date string ("2020-01-01") or a datetime.
DateLike = Union[str, datetime]


class Quote(BaseModel):
    """A point-in-time quote. Crosses module boundaries, so it is a model."""

    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    ts_utc: str

    @property
    def mid(self) -> Optional[float]:
        """Midpoint of bid and ask, or None if either side is missing."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@runtime_checkable
class DataSource(Protocol):
    """Swappable market-data source. IBKR and fallbacks both implement this."""

    def get_historical_bars(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        bar_size: str = "1 day",
    ) -> pd.DataFrame:
        """Return completed OHLCV bars for symbol in [start, end].

        The result has lowercase columns (a subset of BAR_COLUMNS) and a UTC
        DatetimeIndex sorted ascending. See the module no-lookahead contract.
        """
        ...

    def get_quote(self, symbol: str) -> Quote:
        """Return the latest available quote for symbol."""
        ...


def normalize_bars(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Coerce a raw provider frame into the canonical bar shape.

    Lowercases and flattens columns, keeps only known OHLCV fields, converts the
    index to a sorted UTC DatetimeIndex, and drops duplicate or all-NaN rows.
    Returns an empty frame with BAR_COLUMNS if df is None or empty.
    """
    if df is None or len(df) == 0:
        empty = pd.DataFrame(columns=BAR_COLUMNS)
        empty.index = pd.DatetimeIndex([], tz="UTC")
        return empty

    out = df.copy()

    # yfinance can return a column MultiIndex (field, ticker) for one symbol.
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    out.columns = [str(c).lower() for c in out.columns]
    keep = [c for c in BAR_COLUMNS if c in out.columns]
    out = out[keep]

    out.index = pd.to_datetime(out.index, utc=True)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(how="all")
    out.index.name = "ts_utc"
    return out
