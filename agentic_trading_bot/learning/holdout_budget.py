"""Consumable holdout budget (invariant 12).

Truly-unseen data is a budgeted, consumable resource. The most recent data is
reserved as a vault and released in tranches. Each tranche may be served (and
thus evaluated) only a fixed number of times before it is BURNED and refuses to
serve again. When the budget is exhausted, no promotion can happen until new
data accrues. `remaining_budget()` powers the UI meter.

The bars for each tranche are stored in the budget database so a served tranche
is reproducible and the vault is self-contained.
"""
from __future__ import annotations

import pickle
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tranches (
    tranche_id      TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    start_ts        TEXT,
    end_ts          TEXT,
    n_bars          INTEGER NOT NULL,
    evaluations     INTEGER NOT NULL DEFAULT 0,
    max_evaluations INTEGER NOT NULL,
    burned          INTEGER NOT NULL DEFAULT 0,
    bars_blob       BLOB NOT NULL,
    created_ts      TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Tranche:
    """A served holdout tranche: its bars plus its consumption state."""

    tranche_id: str
    bars: pd.DataFrame
    evaluations: int
    max_evaluations: int
    burned: bool

    @property
    def remaining(self) -> int:
        """Evaluations left before this tranche burns."""
        return 0 if self.burned else max(0, self.max_evaluations - self.evaluations)


class BudgetExhaustedError(Exception):
    """Raised when a burned (or unknown) tranche is requested."""


class HoldoutBudget:
    """Vault of recent unseen data, released in burn-after-N tranches."""

    def __init__(self, db_path: Union[str, Path], max_evaluations: int = 3) -> None:
        """Open (creating if needed) the budget database.

        Args:
            db_path: Path to the budget SQLite file.
            max_evaluations: Default times a tranche may be served before burning.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_max_evaluations = max_evaluations
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def reserve(
        self,
        data: pd.DataFrame,
        n_tranches: int,
        label: str = "v1",
        max_evaluations: Optional[int] = None,
    ) -> list[str]:
        """Split the most recent unseen `data` into contiguous tranches.

        The data is divided oldest-to-newest into `n_tranches` equal chunks, each
        stored with a zeroed evaluation count. Returns the tranche ids in order.
        """
        if n_tranches < 1:
            raise ValueError("n_tranches must be >= 1")
        cap = max_evaluations if max_evaluations is not None else self.default_max_evaluations
        chunks = [c for c in _split(data, n_tranches) if len(c) > 0]
        ids: list[str] = []
        with self._lock, self._conn:
            for i, chunk in enumerate(chunks):
                tranche_id = f"{label}-{i}"
                start = str(chunk.index[0])
                end = str(chunk.index[-1])
                self._conn.execute(
                    "INSERT OR REPLACE INTO tranches "
                    "(tranche_id, label, idx, start_ts, end_ts, n_bars, evaluations, "
                    " max_evaluations, burned, bars_blob, created_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tranche_id,
                        label,
                        i,
                        start,
                        end,
                        len(chunk),
                        0,
                        cap,
                        0,
                        pickle.dumps(chunk),
                        _utc_now_iso(),
                    ),
                )
                ids.append(tranche_id)
        return ids

    def serve(self, tranche_id: str) -> Tranche:
        """Serve a tranche, recording one evaluation and burning it at the cap.

        Raises:
            BudgetExhaustedError: If the tranche is unknown or already burned.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM tranches WHERE tranche_id = ?", (tranche_id,)
            ).fetchone()
            if row is None:
                raise BudgetExhaustedError(f"unknown tranche {tranche_id!r}")
            if row["burned"]:
                raise BudgetExhaustedError(
                    f"tranche {tranche_id!r} is burned: its evaluation budget is exhausted"
                )
            evaluations = int(row["evaluations"]) + 1
            burned = 1 if evaluations >= int(row["max_evaluations"]) else 0
            self._conn.execute(
                "UPDATE tranches SET evaluations = ?, burned = ? WHERE tranche_id = ?",
                (evaluations, burned, tranche_id),
            )
            bars = pickle.loads(row["bars_blob"])
        return Tranche(
            tranche_id=tranche_id,
            bars=bars,
            evaluations=evaluations,
            max_evaluations=int(row["max_evaluations"]),
            burned=bool(burned),
        )

    def is_burned(self, tranche_id: str) -> bool:
        """True if the tranche is burned or unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT burned FROM tranches WHERE tranche_id = ?", (tranche_id,)
            ).fetchone()
        return True if row is None else bool(row["burned"])

    def remaining_budget(self) -> dict[str, Any]:
        """Return the budget meter: total remaining evaluations and per-tranche."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tranche_id, evaluations, max_evaluations, burned, n_bars, "
                "start_ts, end_ts FROM tranches ORDER BY label ASC, idx ASC"
            ).fetchall()
        tranches = []
        total_remaining = 0
        for r in rows:
            remaining = 0 if r["burned"] else max(0, int(r["max_evaluations"]) - int(r["evaluations"]))
            total_remaining += remaining
            tranches.append(
                {
                    "tranche_id": r["tranche_id"],
                    "evaluations": int(r["evaluations"]),
                    "max_evaluations": int(r["max_evaluations"]),
                    "remaining": remaining,
                    "burned": bool(r["burned"]),
                    "n_bars": int(r["n_bars"]),
                    "start_ts": r["start_ts"],
                    "end_ts": r["end_ts"],
                }
            )
        return {
            "total_remaining": total_remaining,
            "any_available": total_remaining > 0,
            "tranches": tranches,
        }

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()


def _split(data: pd.DataFrame, n: int) -> list[pd.DataFrame]:
    """Split a frame into n contiguous chunks, oldest first."""
    length = len(data)
    size = length // n
    chunks: list[pd.DataFrame] = []
    for i in range(n):
        start = i * size
        end = length if i == n - 1 else (i + 1) * size
        chunks.append(data.iloc[start:end])
    return chunks
