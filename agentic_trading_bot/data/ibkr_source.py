"""IBKR-backed data source (ib_async).

Implements the DataSource protocol against a connected ib_async IB object. The
broker owns the connection and hands its `ib` to this source, so historical bars
and quotes flow through the same session as orders. Historical requests are
served from the DiskCache when one is provided.

No-lookahead: IBKR historical bars are right-labeled and only fully formed at the
bar's close, consistent with the contract in data.base. The caller still owns
warmup dropping and the next-bar execution rule.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from data.base import DataSource, DateLike, Quote, normalize_bars
from data.cache import DiskCache


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ib_duration_str(start: DateLike, end: DateLike) -> str:
    """Translate a [start, end] window into an IBKR durationStr.

    IBKR accepts durations like "30 D" or "2 Y". Days up to one year are sent as
    days; longer spans are rounded up to whole years.
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    days = max(1, int((end_ts - start_ts).days))
    if days > 365:
        years = math.ceil(days / 365)
        return f"{years} Y"
    return f"{days} D"


def _ib_end_datetime(end: DateLike) -> str:
    """Format the end of the window for IBKR's endDateTime, or '' for now."""
    if end is None:
        return ""
    return pd.Timestamp(end).strftime("%Y%m%d %H:%M:%S")


class IBKRDataSource(DataSource):
    """Market-data source backed by a connected ib_async IB session."""

    SOURCE = "ibkr"

    def __init__(
        self,
        ib: Optional[Any] = None,
        cache: Optional[DiskCache] = None,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> None:
        """Create the source.

        Args:
            ib: A connected ib_async IB object. May be set later by the broker.
            cache: Optional DiskCache for historical bars.
            exchange: Default routing exchange for qualified contracts.
            currency: Default contract currency.
        """
        self.ib = ib
        self.cache = cache
        self.exchange = exchange
        self.currency = currency

    def _require_ib(self) -> Any:
        if self.ib is None:
            raise RuntimeError(
                "IBKRDataSource has no ib session. Connect the broker first "
                "(IBKRClient.connect) so the data source is wired to it."
            )
        return self.ib

    def _stock(self, symbol: str) -> Any:
        from ib_async import Stock

        contract = Stock(symbol, self.exchange, self.currency)
        self._require_ib().qualifyContracts(contract)
        return contract

    def get_historical_bars(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        bar_size: str = "1 day",
    ) -> pd.DataFrame:
        """Return normalized OHLCV bars for symbol, using the cache when present."""
        key: Optional[str] = None
        if self.cache is not None:
            key = DiskCache.make_key(self.SOURCE, symbol, start, end, bar_size)
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        ib = self._require_ib()
        contract = self._stock(symbol)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=_ib_end_datetime(end),
            durationStr=_ib_duration_str(start, end),
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )

        rows = [
            {
                "date": getattr(bar, "date", None),
                "open": getattr(bar, "open", None),
                "high": getattr(bar, "high", None),
                "low": getattr(bar, "low", None),
                "close": getattr(bar, "close", None),
                "volume": getattr(bar, "volume", None),
            }
            for bar in (bars or [])
        ]
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame = frame.set_index("date")
        df = normalize_bars(frame)

        if self.cache is not None and key is not None and len(df) > 0:
            self.cache.set(key, df)
        return df

    def get_quote(self, symbol: str) -> Quote:
        """Return the latest available quote for symbol (live, not cached)."""
        ib = self._require_ib()
        contract = self._stock(symbol)
        ticker = ib.reqMktData(contract, "", False, False)
        # Give the snapshot a moment to populate before reading it.
        try:
            ib.sleep(0.2)
        except Exception:
            pass

        def _num(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                f = float(value)
                return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None

        quote = Quote(
            symbol=symbol,
            bid=_num(getattr(ticker, "bid", None)),
            ask=_num(getattr(ticker, "ask", None)),
            last=_num(getattr(ticker, "last", None)),
            ts_utc=_utc_now_iso(),
        )
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
        return quote
