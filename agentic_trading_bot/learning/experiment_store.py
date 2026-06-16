"""Durable store for Experiments produced by the self-learning loop.

The learning loop pre-registers an experiment, runs the controlled test on an
unburned holdout tranche, and records a verdict. Promotion reads the stored
experiment back as evidence; it is never re-derived from a metric computed after
the fact (CLAUDE.md invariant 13). This store is the experiment record that the
promote path consults, so a skill cannot be promoted without an experiment that
already exists and already PASSED.

The store is append-and-replace by experiment_id and lives in the same learning
database as the skills registry (journal/learning.db). It holds no risk or
execution state; like the rest of the learning layer it only records evidence.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

from core.contracts import Experiment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     TEXT PRIMARY KEY,
    hypothesis_id     TEXT,
    candidate_skill_id TEXT,
    verdict           TEXT NOT NULL,
    created_at        TEXT,
    experiment_json   TEXT NOT NULL
);
"""


class ExperimentStore:
    """SQLite-backed store of pre-registered, run Experiments."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (creating if needed) the experiment store."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def save(self, experiment: Experiment) -> str:
        """Persist (insert or replace) an experiment and return its id."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO experiments "
                "(experiment_id, hypothesis_id, candidate_skill_id, verdict, created_at, "
                " experiment_json) VALUES (?,?,?,?,?,?)",
                (
                    experiment.experiment_id,
                    experiment.hypothesis_id,
                    experiment.candidate_skill_id,
                    experiment.verdict.value,
                    experiment.created_at,
                    experiment.model_dump_json(),
                ),
            )
        return experiment.experiment_id

    def get(self, experiment_id: str) -> Optional[Experiment]:
        """Return an experiment by id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT experiment_json FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return Experiment.model_validate_json(row["experiment_json"]) if row else None

    def list_for_skill(self, candidate_skill_id: str) -> list[Experiment]:
        """Return experiments recorded for a given candidate skill (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT experiment_json FROM experiments WHERE candidate_skill_id = ? "
                "ORDER BY created_at ASC",
                (candidate_skill_id,),
            ).fetchall()
        return [Experiment.model_validate_json(r["experiment_json"]) for r in rows]

    def list_all(self) -> list[Experiment]:
        """Return all stored experiments (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT experiment_json FROM experiments ORDER BY created_at ASC"
            ).fetchall()
        return [Experiment.model_validate_json(r["experiment_json"]) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
