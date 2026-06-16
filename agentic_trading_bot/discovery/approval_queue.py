"""Approval queue: decoupled human-in-the-loop store for proposals.

The pipeline only ENQUEUES proposals here. Approval is a separate human action
performed later in the UI. Two hard rules are enforced structurally:

  1. A proposal can move to the approved-strategies store only if its
     ValidationResult passed (a FAIL is never approvable), AND
  2. a human approver id is required (there is no auto-approve path).

Approval grants permission to execute on PAPER only. Promotion to live is a
separate manual step gated by the CLAUDE.md invariants. Every enqueue, approval,
and rejection is written to the audit trail by the caller; the queue itself is
the durable store of record.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from core.contracts import Proposal, ProposalStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id   TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    name          TEXT NOT NULL,
    template      TEXT NOT NULL,
    created_ts    TEXT NOT NULL,
    decided_by    TEXT,
    decided_ts    TEXT,
    decision_reason TEXT,
    proposal_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_strategies (
    proposal_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    template      TEXT NOT NULL,
    approved_by   TEXT NOT NULL,
    approved_ts   TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'PAPER',
    spec_json     TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalError(Exception):
    """Raised when an approval/rejection is not permitted."""


class ApprovalQueue:
    """SQLite-backed, mutable proposal queue with an approved-strategies store."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (creating if needed) the queue database."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Additive, idempotent migrations for older databases.

        Adds the `enabled` flag to the approved-strategies store. SQLite has no
        ADD COLUMN IF NOT EXISTS, so a duplicate-column error is treated as a
        no-op. Approved strategies default to enabled.
        """
        try:
            self._conn.execute(
                "ALTER TABLE approved_strategies ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass  # column already present

    # ------------------------------------------------------------- enqueue

    def enqueue(self, proposal: Proposal) -> str:
        """Persist a PENDING proposal and return its id."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO proposals "
                "(proposal_id, status, passed, name, template, created_ts, "
                " decided_by, decided_ts, decision_reason, proposal_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal.proposal_id,
                    proposal.status.value,
                    1 if proposal.passed else 0,
                    proposal.spec.name,
                    proposal.spec.template,
                    proposal.created_ts,
                    proposal.decided_by,
                    proposal.decided_ts,
                    proposal.decision_reason,
                    proposal.model_dump_json(),
                ),
            )
        return proposal.proposal_id

    # ------------------------------------------------------------- reads

    def get(self, proposal_id: str) -> Optional[Proposal]:
        """Return a proposal by id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT proposal_json FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return Proposal.model_validate_json(row["proposal_json"]) if row else None

    def list_pending(self) -> list[Proposal]:
        """Return all PENDING proposals (oldest first)."""
        return self._list("WHERE status = ?", (ProposalStatus.PENDING.value,))

    def list_all(self) -> list[Proposal]:
        """Return all proposals (oldest first)."""
        return self._list("", ())

    def list_approved_strategies(self) -> list[dict[str, Any]]:
        """Return the approved-strategies store rows (newest enable flag included)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT proposal_id, name, template, approved_by, approved_ts, mode, "
                "enabled, spec_json FROM approved_strategies ORDER BY approved_ts ASC"
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["enabled"] = bool(record.get("enabled", 1))
            record["spec"] = json.loads(record.pop("spec_json"))
            out.append(record)
        return out

    def set_strategy_enabled(
        self, proposal_id: str, enabled: bool, audit: Any, who: str = "ui"
    ) -> dict[str, Any]:
        """Enable or disable an already-approved strategy.

        This toggles whether the orchestrator may act on the approved strategy.
        It cannot approve, create, or execute anything; disabling only reduces
        reliance. Raises ApprovalError if the strategy is not in the approved
        store. The change is audited.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE approved_strategies SET enabled = ? WHERE proposal_id = ?",
                (1 if enabled else 0, proposal_id),
            )
            if cur.rowcount == 0:
                raise ApprovalError(f"unknown approved strategy {proposal_id}")
        audit.record(
            "STRATEGY_ENABLED" if enabled else "STRATEGY_DISABLED",
            {"proposal_id": proposal_id, "by": who, "enabled": enabled},
            f"{who} {'enabled' if enabled else 'disabled'} approved strategy {proposal_id}",
        )
        rows = [r for r in self.list_approved_strategies() if r["proposal_id"] == proposal_id]
        return rows[0] if rows else {"proposal_id": proposal_id, "enabled": enabled}

    def _list(self, where: str, params: tuple) -> list[Proposal]:
        sql = "SELECT proposal_json FROM proposals " + where + " ORDER BY created_ts ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Proposal.model_validate_json(r["proposal_json"]) for r in rows]

    # ------------------------------------------------------------- decisions

    def approve(self, proposal_id: str, approver: str, audit: Any, note: str = "") -> Proposal:
        """Approve a passed proposal (PAPER execution only).

        Raises ApprovalError unless the proposal exists, is pending, PASSED the
        gate, and a non-empty human approver id is supplied.
        """
        if not approver or not approver.strip():
            raise ApprovalError("approval requires a human approver id")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise ApprovalError(f"unknown proposal {proposal_id}")
        if proposal.status is not ProposalStatus.PENDING:
            raise ApprovalError(f"proposal {proposal_id} is already {proposal.status.value}")
        if not proposal.passed:
            # Structural enforcement: a FAIL can never be approved.
            audit.record(
                "APPROVAL_DENIED",
                {"proposal_id": proposal_id, "approver": approver},
                "Refused to approve a proposal that did not pass the validation gate",
            )
            raise ApprovalError("cannot approve a proposal that failed the validation gate")

        proposal.status = ProposalStatus.APPROVED
        proposal.decided_by = approver
        proposal.decided_ts = _utc_now_iso()
        proposal.decision_reason = note
        self.enqueue(proposal)

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO approved_strategies "
                "(proposal_id, name, template, approved_by, approved_ts, mode, spec_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    proposal.proposal_id,
                    proposal.spec.name,
                    proposal.spec.template,
                    approver,
                    proposal.decided_ts,
                    "PAPER",  # approval grants PAPER execution only
                    proposal.spec.model_dump_json(),
                ),
            )
        audit.record(
            "APPROVAL",
            {"proposal_id": proposal_id, "approver": approver, "mode": "PAPER", "note": note},
            f"Human {approver} approved proposal {proposal_id} for PAPER execution only",
        )
        return proposal

    def reject(self, proposal_id: str, approver: str, audit: Any, reason: str = "") -> Proposal:
        """Reject a pending proposal and audit the human decision."""
        if not approver or not approver.strip():
            raise ApprovalError("rejection requires a human approver id")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise ApprovalError(f"unknown proposal {proposal_id}")
        if proposal.status is not ProposalStatus.PENDING:
            raise ApprovalError(f"proposal {proposal_id} is already {proposal.status.value}")

        proposal.status = ProposalStatus.REJECTED
        proposal.decided_by = approver
        proposal.decided_ts = _utc_now_iso()
        proposal.decision_reason = reason
        self.enqueue(proposal)
        audit.record(
            "REJECTION",
            {"proposal_id": proposal_id, "approver": approver, "reason": reason},
            f"Human {approver} rejected proposal {proposal_id}",
        )
        return proposal

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
