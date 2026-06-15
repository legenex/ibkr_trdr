"""Persistent cumulative trial ledger (invariant 11).

Every hypothesis ever tested is charged here, keyed by strategy family. All
overfitting corrections (the Deflated Sharpe, and PBO if added later) use the
CUMULATIVE family count, not a per-run N, so the loop cannot launder selection
bias by spreading trials across many runs. The ledger is monotonic: counts only
ever increase.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_ledger (
    family     TEXT PRIMARY KEY,
    trials     INTEGER NOT NULL DEFAULT 0,
    updated_ts TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TrialLedger:
    """Monotonic, persistent count of hypotheses tried per strategy family."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (creating if needed) the ledger database."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def charge(self, family: str, n: int = 1) -> int:
        """Add `n` trials to a family's running total and return the new total.

        Args:
            family: Strategy family key.
            n: Number of trials to charge (must be non-negative).
        """
        if n < 0:
            raise ValueError("cannot charge a negative number of trials")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trial_ledger (family, trials, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(family) DO UPDATE SET "
                "trials = trials + excluded.trials, updated_ts = excluded.updated_ts",
                (family, n, _utc_now_iso()),
            )
            row = self._conn.execute(
                "SELECT trials FROM trial_ledger WHERE family = ?", (family,)
            ).fetchone()
        return int(row["trials"])

    def count(self, family: str) -> int:
        """Return the cumulative trial count for a family (0 if never charged)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT trials FROM trial_ledger WHERE family = ?", (family,)
            ).fetchone()
        return int(row["trials"]) if row else 0

    def all_counts(self) -> dict[str, int]:
        """Return the full family -> cumulative count map."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT family, trials FROM trial_ledger ORDER BY family ASC"
            ).fetchall()
        return {r["family"]: int(r["trials"]) for r in rows}

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
