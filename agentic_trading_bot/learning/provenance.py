"""Trade-attribution layer: trace a closed paper trade back to what opened it.

This is the persistent, restart-safe bridge between execution and the learning
loop. It lives in journal/learning.db alongside the skills registry, the trial
ledger, and the holdout budget. Like the rest of the learning layer it only
RECORDS evidence (CLAUDE.md invariant 9): it never places, modifies, or cancels
an order, and it never touches the risk gate.

Three tables:

  position_provenance  one row per opened position lot. Accumulates partial
                       entry fills into filled_qty, a weighted-average
                       avg_entry_price, and summed entry_cost. Captures the
                       RegimeState AS A SNAPSHOT at entry (stored JSON), never
                       reconstructed later, so attribution cannot drift and no
                       re-fit model can leak in (the no-lookahead rule).

  provenance_fills     every fill we have attributed, keyed by exec_id. The
                       primary key makes replaying a fill a no-op, so a fill is
                       never double-counted across cycles or restarts.

  trade_traces         each assembled TradeTrace for a closed (or partially
                       closed) portion, with a `processed` flag the learning loop
                       flips exactly once so a trace is reflected on at most once.

The ledger detects nothing on its own; the orchestrator drives it each cycle and
asks it to assemble a trace for a closed portion. When a close cannot be cleanly
attributed the orchestrator audits TRACE_UNATTRIBUTED and the ledger fabricates
nothing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union
from uuid import uuid4

from core.contracts import Fill, OrderSide, RegimeState, TradeTrace

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_provenance (
    provenance_id           TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    entry_side              TEXT NOT NULL,
    intended_qty            REAL NOT NULL DEFAULT 0,
    filled_qty              REAL NOT NULL DEFAULT 0,
    avg_entry_price         REAL NOT NULL DEFAULT 0,
    entry_cost              REAL NOT NULL DEFAULT 0,
    originating_strategy_id TEXT,
    originating_skill_id    TEXT,
    originating_proposal_id TEXT,
    intended_stop           TEXT,
    entry_order_ids         TEXT,
    entry_regime_json       TEXT,
    opened_at               TEXT,
    status                  TEXT NOT NULL DEFAULT 'open',
    closed_at               TEXT
);
CREATE TABLE IF NOT EXISTS provenance_fills (
    exec_id        TEXT PRIMARY KEY,
    provenance_id  TEXT NOT NULL,
    kind           TEXT NOT NULL,          -- 'entry' or 'exit'
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            REAL NOT NULL,
    price          REAL NOT NULL,
    commission     REAL NOT NULL DEFAULT 0,
    ts_utc         TEXT,
    consumed       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trade_traces (
    trace_id       TEXT PRIMARY KEY,
    provenance_id  TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    net_pnl        REAL NOT NULL DEFAULT 0,
    gross_pnl      REAL NOT NULL DEFAULT 0,
    processed      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    trace_json     TEXT NOT NULL
);
"""

# Statuses for a provenance row.
STATUS_OPEN = "open"
STATUS_REDUCED = "reduced"
STATUS_CLOSED = "closed"

_QTY_TOL = 1e-9


@dataclass
class ProvenanceRow:
    """An opened position lot and its accumulated entry state."""

    provenance_id: str
    symbol: str
    entry_side: str  # OrderSide value: "BUY" / "SELL"
    intended_qty: float
    filled_qty: float
    avg_entry_price: float
    entry_cost: float
    originating_strategy_id: Optional[str]
    originating_skill_id: Optional[str]
    originating_proposal_id: Optional[str]
    intended_stop: Optional[str]
    entry_order_ids: list[int]
    entry_regime_json: Optional[str]
    opened_at: Optional[str]
    status: str
    closed_at: Optional[str]

    @property
    def entry_sign(self) -> int:
        """+1 for a long entry, -1 for a short entry."""
        return 1 if self.entry_side == OrderSide.BUY.value else -1

    def regime_state(self) -> Optional[RegimeState]:
        """The RegimeState captured at entry, or None if none was captured."""
        if not self.entry_regime_json:
            return None
        try:
            return RegimeState.model_validate_json(self.entry_regime_json)
        except Exception:  # noqa: BLE001
            return None


def _fill_key(fill: Fill) -> str:
    """A stable idempotency key for a fill.

    Prefers the broker exec_id; falls back to a composite when the broker did not
    supply one, so a replayed fill still maps to the same key.
    """
    if fill.exec_id:
        return str(fill.exec_id)
    return f"{fill.symbol}|{fill.side.value}|{fill.quantity}|{fill.price}|{fill.ts_utc}|{fill.order_id}"


class ProvenanceLedger:
    """SQLite-backed provenance ledger. Records evidence only; never trades."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (creating if needed) the provenance ledger in learning.db."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # --------------------------------------------------------------- opening

    def open_position(
        self,
        *,
        symbol: str,
        entry_side: OrderSide,
        intended_qty: float = 0.0,
        originating_strategy_id: Optional[str] = None,
        originating_skill_id: Optional[str] = None,
        originating_proposal_id: Optional[str] = None,
        intended_stop: Optional[str] = None,
        entry_order_ids: Optional[list[int]] = None,
        entry_regime: Optional[RegimeState] = None,
        opened_at: str,
    ) -> str:
        """Create a new open provenance row and return its id.

        The regime is stored as a snapshot of the RegimeState passed in; it is
        never recomputed from a later model fit.
        """
        provenance_id = uuid4().hex[:12]
        regime_json = entry_regime.model_dump_json() if entry_regime is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO position_provenance (provenance_id, symbol, entry_side, "
                "intended_qty, filled_qty, avg_entry_price, entry_cost, "
                "originating_strategy_id, originating_skill_id, originating_proposal_id, "
                "intended_stop, entry_order_ids, entry_regime_json, opened_at, status, closed_at) "
                "VALUES (?,?,?,?,0,0,0,?,?,?,?,?,?,?,?,NULL)",
                (
                    provenance_id, symbol, entry_side.value, float(intended_qty),
                    originating_strategy_id, originating_skill_id, originating_proposal_id,
                    intended_stop, json.dumps(list(entry_order_ids or [])),
                    regime_json, opened_at, STATUS_OPEN,
                ),
            )
        return provenance_id

    # ----------------------------------------------------------- fill record

    def has_fill(self, fill: Fill) -> bool:
        """True if this fill was already attributed (idempotency check)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM provenance_fills WHERE exec_id = ?", (_fill_key(fill),)
            ).fetchone()
        return row is not None

    def record_entry_fill(self, provenance_id: str, fill: Fill) -> bool:
        """Attribute an entry fill to a row, accumulating qty/price/cost.

        Returns True if the fill was newly recorded, False if it was a replay
        (already seen exec_id) and thus ignored. Weighted-average entry price and
        summed entry cost are recomputed atomically.
        """
        key = _fill_key(fill)
        commission = float(fill.commission or 0.0)
        with self._lock, self._conn:
            seen = self._conn.execute(
                "SELECT 1 FROM provenance_fills WHERE exec_id = ?", (key,)
            ).fetchone()
            if seen is not None:
                return False
            row = self._conn.execute(
                "SELECT filled_qty, avg_entry_price, entry_cost FROM position_provenance "
                "WHERE provenance_id = ?", (provenance_id,)
            ).fetchone()
            if row is None:
                return False
            old_qty = float(row["filled_qty"])
            old_avg = float(row["avg_entry_price"])
            new_qty = old_qty + float(fill.quantity)
            new_avg = (
                (old_avg * old_qty + float(fill.price) * float(fill.quantity)) / new_qty
                if new_qty > _QTY_TOL else 0.0
            )
            self._conn.execute(
                "UPDATE position_provenance SET filled_qty = ?, avg_entry_price = ?, "
                "entry_cost = entry_cost + ? WHERE provenance_id = ?",
                (new_qty, new_avg, commission, provenance_id),
            )
            self._conn.execute(
                "INSERT INTO provenance_fills (exec_id, provenance_id, kind, symbol, side, "
                "qty, price, commission, ts_utc, consumed) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (key, provenance_id, "entry", fill.symbol, fill.side.value,
                 float(fill.quantity), float(fill.price), commission, fill.ts_utc),
            )
        return True

    def record_exit_fill(self, provenance_id: str, fill: Fill) -> bool:
        """Attribute an exit (closing) fill to a row. Idempotent by exec_id."""
        key = _fill_key(fill)
        commission = float(fill.commission or 0.0)
        with self._lock, self._conn:
            seen = self._conn.execute(
                "SELECT 1 FROM provenance_fills WHERE exec_id = ?", (key,)
            ).fetchone()
            if seen is not None:
                return False
            self._conn.execute(
                "INSERT INTO provenance_fills (exec_id, provenance_id, kind, symbol, side, "
                "qty, price, commission, ts_utc, consumed) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (key, provenance_id, "exit", fill.symbol, fill.side.value,
                 float(fill.quantity), float(fill.price), commission, fill.ts_utc),
            )
        return True

    # ------------------------------------------------------------- queries

    def _row_from(self, r: sqlite3.Row) -> ProvenanceRow:
        return ProvenanceRow(
            provenance_id=r["provenance_id"], symbol=r["symbol"], entry_side=r["entry_side"],
            intended_qty=float(r["intended_qty"]), filled_qty=float(r["filled_qty"]),
            avg_entry_price=float(r["avg_entry_price"]), entry_cost=float(r["entry_cost"]),
            originating_strategy_id=r["originating_strategy_id"],
            originating_skill_id=r["originating_skill_id"],
            originating_proposal_id=r["originating_proposal_id"],
            intended_stop=r["intended_stop"],
            entry_order_ids=json.loads(r["entry_order_ids"] or "[]"),
            entry_regime_json=r["entry_regime_json"], opened_at=r["opened_at"],
            status=r["status"], closed_at=r["closed_at"],
        )

    def get_row(self, provenance_id: str) -> Optional[ProvenanceRow]:
        """Return one provenance row by id, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM position_provenance WHERE provenance_id = ?", (provenance_id,)
            ).fetchone()
        return self._row_from(r) if r else None

    def open_rows(self) -> list[ProvenanceRow]:
        """All rows still carrying risk (status open or reduced), oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM position_provenance WHERE status IN (?, ?) ORDER BY opened_at ASC",
                (STATUS_OPEN, STATUS_REDUCED),
            ).fetchall()
        return [self._row_from(r) for r in rows]

    def open_rows_for(self, symbol: str) -> list[ProvenanceRow]:
        """Open/reduced rows for one symbol (used to detect ambiguity)."""
        return [r for r in self.open_rows() if r.symbol == symbol]

    def all_rows(self) -> list[ProvenanceRow]:
        """Every provenance row regardless of status (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM position_provenance ORDER BY opened_at ASC"
            ).fetchall()
        return [self._row_from(r) for r in rows]

    def unconsumed_exit_fills(self, provenance_id: str) -> list[Fill]:
        """Exit fills attributed to a row that have not yet been folded into a trace."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM provenance_fills WHERE provenance_id = ? AND kind = 'exit' "
                "AND consumed = 0 ORDER BY ts_utc ASC", (provenance_id,)
            ).fetchall()
        return [self._fill_from(r) for r in rows]

    def entry_fills(self, provenance_id: str) -> list[Fill]:
        """All entry fills attributed to a row (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM provenance_fills WHERE provenance_id = ? AND kind = 'entry' "
                "ORDER BY ts_utc ASC", (provenance_id,)
            ).fetchall()
        return [self._fill_from(r) for r in rows]

    @staticmethod
    def _fill_from(r: sqlite3.Row) -> Fill:
        return Fill(
            symbol=r["symbol"], side=OrderSide(r["side"]), quantity=float(r["qty"]),
            price=float(r["price"]), ts_utc=r["ts_utc"] or "", exec_id=r["exec_id"],
            commission=float(r["commission"]),
        )

    # --------------------------------------------------------- mutations

    def consume_exit_fills(self, provenance_id: str) -> None:
        """Mark a row's exit fills as folded into a trace (so they are not reused)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE provenance_fills SET consumed = 1 WHERE provenance_id = ? AND kind = 'exit'",
                (provenance_id,),
            )

    def reduce_position(self, provenance_id: str, closed_qty: float) -> None:
        """Reduce a row's filled_qty by closed_qty, scaling entry_cost pro-rata.

        avg_entry_price is unchanged (it is per share). The status becomes
        'reduced' while a remainder is still open.
        """
        with self._lock, self._conn:
            r = self._conn.execute(
                "SELECT filled_qty, entry_cost FROM position_provenance WHERE provenance_id = ?",
                (provenance_id,),
            ).fetchone()
            if r is None:
                return
            old_qty = float(r["filled_qty"])
            old_cost = float(r["entry_cost"])
            remaining = max(0.0, old_qty - closed_qty)
            new_cost = old_cost * (remaining / old_qty) if old_qty > _QTY_TOL else 0.0
            self._conn.execute(
                "UPDATE position_provenance SET filled_qty = ?, entry_cost = ?, status = ? "
                "WHERE provenance_id = ?",
                (remaining, new_cost, STATUS_REDUCED, provenance_id),
            )

    def close_row(self, provenance_id: str, closed_at: str) -> None:
        """Mark a row fully closed; it can never re-emit a trace."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE position_provenance SET status = ?, closed_at = ? WHERE provenance_id = ?",
                (STATUS_CLOSED, closed_at, provenance_id),
            )

    # ----------------------------------------------------------- traces

    def record_trace(self, trace: TradeTrace, provenance_id: str) -> str:
        """Persist an assembled TradeTrace (unprocessed) and return its trace id."""
        trace_id = uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trade_traces (trace_id, provenance_id, symbol, net_pnl, gross_pnl, "
                "processed, created_at, trace_json) VALUES (?,?,?,?,?,0,?,?)",
                (trace_id, provenance_id, trace.extra.get("symbol", ""),
                 float(trace.net_pnl), float(trace.gross_pnl),
                 trace.extra.get("closed_at", ""), trace.model_dump_json()),
            )
        return trace_id

    def list_unprocessed_traces(self) -> list[tuple[str, TradeTrace]]:
        """Return (trace_id, TradeTrace) pairs not yet reflected on (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT trace_id, trace_json FROM trade_traces WHERE processed = 0 "
                "ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        return [(r["trace_id"], TradeTrace.model_validate_json(r["trace_json"])) for r in rows]

    def mark_trace_processed(self, trace_id: str) -> None:
        """Flip a trace's processed flag so it is reflected on at most once."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE trade_traces SET processed = 1 WHERE trace_id = ?", (trace_id,)
            )

    def all_traces(self) -> list[TradeTrace]:
        """Every recorded trace (oldest first), for inspection and tests."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT trace_json FROM trade_traces ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        return [TradeTrace.model_validate_json(r["trace_json"]) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
