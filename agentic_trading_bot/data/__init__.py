"""Swappable market-data interface plus IBKR and yfinance implementations.

See data.base for the no-lookahead contract every source must honor.
"""
from data.base import BAR_COLUMNS, DataSource, DateLike, Quote, normalize_bars
from data.cache import DiskCache
from data.ibkr_source import IBKRDataSource
from data.yfinance_source import YFinanceDataSource

__all__ = [
    "BAR_COLUMNS",
    "DataSource",
    "DateLike",
    "Quote",
    "normalize_bars",
    "DiskCache",
    "IBKRDataSource",
    "YFinanceDataSource",
]
