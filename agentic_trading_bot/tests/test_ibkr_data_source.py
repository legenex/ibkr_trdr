"""Tests for IBKRDataSource against a fake ib_async session (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.ibkr_source import IBKRDataSource
from testsupport.fakes import FakeIB


def test_historical_bars_normalized():
    ib = FakeIB()
    ib._bars = [
        SimpleNamespace(date="2020-01-02", open=1.0, high=2.0, low=0.5, close=1.5, volume=100),
        SimpleNamespace(date="2020-01-03", open=1.5, high=2.5, low=1.0, close=2.0, volume=200),
    ]
    source = IBKRDataSource(ib=ib, cache=None)
    df = source.get_historical_bars("AAPL", "2020-01-01", "2020-02-01", "1 day")

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing


def test_get_quote_reads_ticker():
    ib = FakeIB()
    source = IBKRDataSource(ib=ib, cache=None)
    quote = source.get_quote("AAPL")
    assert quote.symbol == "AAPL"
    assert quote.bid == 10.0
    assert quote.ask == 10.1
    assert quote.mid == pytest.approx(10.05)


def test_requires_ib_session():
    source = IBKRDataSource(ib=None)
    with pytest.raises(RuntimeError):
        source.get_historical_bars("AAPL", "2020-01-01", "2020-02-01")
