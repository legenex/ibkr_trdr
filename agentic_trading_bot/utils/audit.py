"""Append-only audit trail backed by SQLite.

Every order, fill, veto, approval, rejection, and agent decision is recorded
here with a UTC timestamp and a human-readable reason. The table is append-only
at the database level: triggers block UPDATE and DELETE, so history can be added
to but never rewritten. There is intentionally no public API to modify or remove
an existing row.

Use the module-level `record_event(...)` for the default trail (at
settings.audit_db_path), or construct an `AuditTrail` against a specific path
(useful in tests).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    run_id       TEXT
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is not permitted');
END;
"""


def _utc_now_iso() -> str:
    """Return the current time as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class AuditEvent(BaseModel):
    """A single audited event. Crosses module boundaries, so it is a model."""

    id: Optional[int] = None
    ts_utc: str
    event_type: str
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    run_id: Optional[str] = None


class AuditTrail:
    """Append-only SQLite-backed audit log.

    The connection is shared across threads behind a lock. Closing the trail
    closes the connection; records already written are durable.
    """

    def __init__(self, db_path: Union[str, Path], run_id: Optional[str] = None) -> None:
        """Open (creating if needed) the audit database at db_path.

        Args:
            db_path: Path to the SQLite file. Parent directories are created.
            run_id: Optional run id stamped onto every event recorded here.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def record(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        reason: str = "",
    ) -> AuditEvent:
        """Append one event and return it with its assigned id and timestamp.

        Args:
            event_type: Short category, for example "ORDER_SUBMITTED" or "RISK_VETO".
            payload: JSON-serializable details of the event.
            reason: Human-readable explanation, always recorded.
        """
        event = AuditEvent(
            ts_utc=_utc_now_iso(),
            event_type=event_type,
            reason=reason,
            payload=payload or {},
            run_id=self.run_id,
        )
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO audit_events (ts_utc, event_type, reason, payload_json, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.ts_utc,
                    event.event_type,
                    event.reason,
                    json.dumps(event.payload, default=str),
                    event.run_id,
                ),
            )
            event.id = cursor.lastrowid
        return event

    def read_all(self) -> list[AuditEvent]:
        """Return every event in insertion order (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts_utc, event_type, reason, payload_json, run_id "
                "FROM audit_events ORDER BY id ASC"
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def count(self) -> int:
        """Return the number of recorded events."""
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()
        return int(n)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            ts_utc=row["ts_utc"],
            event_type=row["event_type"],
            reason=row["reason"],
            payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
            run_id=row["run_id"],
        )

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()


@lru_cache(maxsize=1)
def get_audit_trail() -> AuditTrail:
    """Return the process-wide audit trail at settings.audit_db_path."""
    from config import settings
    from utils.logging import RUN_ID

    return AuditTrail(settings.audit_db_path, run_id=RUN_ID)


def record_event(
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    reason: str = "",
) -> AuditEvent:
    """Append an event to the default (process-wide) audit trail."""
    return get_audit_trail().record(event_type, payload=payload, reason=reason)
