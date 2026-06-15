"""Tests for the append-only audit trail.

These verify that events round-trip correctly and, critically, that the table
cannot be rewritten: UPDATE and DELETE are blocked at the database level.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from utils.audit import AuditTrail


def test_record_and_read_round_trip(tmp_path) -> None:
    trail = AuditTrail(tmp_path / "audit.db", run_id="run-123")
    e1 = trail.record("ORDER_SUBMITTED", {"symbol": "AAPL", "qty": 10}, "entry bracket")
    e2 = trail.record("RISK_VETO", {"symbol": "AAPL"}, "exceeds single name weight")

    rows = trail.read_all()
    assert len(rows) == 2
    assert trail.count() == 2

    assert rows[0].event_type == "ORDER_SUBMITTED"
    assert rows[0].payload["qty"] == 10
    assert rows[0].reason == "entry bracket"
    assert rows[0].run_id == "run-123"
    assert rows[1].event_type == "RISK_VETO"

    # Ids are assigned and strictly increasing in insertion order.
    assert e1.id is not None and e2.id is not None
    assert e2.id > e1.id

    # Timestamp is a parseable UTC ISO-8601 string.
    parsed = datetime.fromisoformat(rows[0].ts_utc)
    assert parsed.tzinfo is not None
    trail.close()


def test_update_is_blocked(tmp_path) -> None:
    db = tmp_path / "audit.db"
    trail = AuditTrail(db)
    trail.record("X", {}, "original reason")
    trail.close()

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.Error) as exc:
            conn.execute("UPDATE audit_events SET reason = 'tampered' WHERE id = 1")
            conn.commit()
        assert "append-only" in str(exc.value)
    finally:
        conn.close()

    # The original row is intact and unchanged.
    rows = AuditTrail(db).read_all()
    assert len(rows) == 1
    assert rows[0].reason == "original reason"


def test_delete_is_blocked(tmp_path) -> None:
    db = tmp_path / "audit.db"
    trail = AuditTrail(db)
    trail.record("X", {}, "keep me")
    trail.close()

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.Error) as exc:
            conn.execute("DELETE FROM audit_events WHERE id = 1")
            conn.commit()
        assert "append-only" in str(exc.value)
    finally:
        conn.close()

    # The row survives the blocked delete.
    assert AuditTrail(db).count() == 1


def test_payload_defaults_to_empty_dict(tmp_path) -> None:
    trail = AuditTrail(tmp_path / "audit.db")
    event = trail.record("HEARTBEAT")
    assert event.payload == {}
    assert trail.read_all()[0].payload == {}
    trail.close()
