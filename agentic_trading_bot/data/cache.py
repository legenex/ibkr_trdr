"""Simple on-disk cache for historical bars so repeated backtests do not refetch.

Historical bars are immutable once formed, so cached frames have no TTL by
default. The cache key incorporates source, symbol, date range, and bar size, so
any change in those produces a distinct cache file. Frames are stored as pickle
to preserve dtypes and the timezone-aware index without a parquet dependency.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from data.base import DateLike


class DiskCache:
    """Filesystem-backed cache mapping a key to a pickled DataFrame."""

    def __init__(self, cache_dir: Union[str, Path]) -> None:
        """Create the cache rooted at cache_dir (created if missing)."""
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(
        source: str,
        symbol: str,
        start: DateLike,
        end: DateLike,
        bar_size: str,
    ) -> str:
        """Return a stable, filesystem-safe key for these request parameters."""
        raw = f"{source}|{symbol}|{start}|{end}|{bar_size}".lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pkl"

    def get(self, key: str) -> Optional[pd.DataFrame]:
        """Return the cached frame for key, or None on a miss or read error."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return pd.read_pickle(path)
        except Exception:
            # A corrupt cache file is treated as a miss, never as a hard error.
            return None

    def set(self, key: str, df: pd.DataFrame) -> None:
        """Write frame df under key."""
        df.to_pickle(self._path(key))

    def clear(self) -> None:
        """Delete every cached frame."""
        for path in self.cache_dir.glob("*.pkl"):
            path.unlink()
