"""SkillRegistry over journal/learning.db: CRUD, retrieval, and status transitions.

This is the single registry for skills. Retrieval (`top_skills`) powers the
skill-aware agents; the status transitions enforce the asymmetric automation
rule (invariant 10):

  - `demote` is ALWAYS allowed: it reduces reliance, which is safe.
  - `promote` is strict. It refuses unless given an Experiment whose verdict is
    PASS. For SIGNAL_SHAPING skills it additionally requires a human approval
    record from the queue AND a passing paper-forward result. RISK_SUGGESTION
    skills can never be promoted at all.

Every transition is audited with the before and after status. The registry never
executes an order, edits the risk gate, or edits the execution path.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from core.contracts import (
    Experiment,
    ExperimentVerdict,
    Proposal,
    ProposalStatus,
    Skill,
    SkillStatus,
    SkillType,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id                TEXT NOT NULL,
    version                 INTEGER NOT NULL,
    skill_type              TEXT NOT NULL,
    name                    TEXT NOT NULL,
    description             TEXT,
    status                  TEXT NOT NULL,
    regimes_json            TEXT NOT NULL DEFAULT '[]',
    theme_tags_json         TEXT NOT NULL DEFAULT '[]',
    prompt_addendum         TEXT NOT NULL DEFAULT '',
    template                TEXT,
    content_or_template     TEXT,
    params_json             TEXT NOT NULL DEFAULT '{}',
    live_performance        REAL NOT NULL DEFAULT 0,
    trials                  INTEGER NOT NULL DEFAULT 0,
    provenance              TEXT NOT NULL DEFAULT '',
    provenance_reflection_id TEXT,
    performance_metrics_json TEXT NOT NULL DEFAULT '{}',
    created_ts              TEXT NOT NULL,
    updated_ts              TEXT NOT NULL,
    PRIMARY KEY (skill_id, version)
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningError(Exception):
    """Raised when a learning-layer transition is not permitted."""


class SkillRegistry:
    """SQLite-backed registry of versioned skills (in journal/learning.db)."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open (creating if needed) the skills table."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ CRUD

    def upsert(self, skill: Skill) -> None:
        """Insert or replace a skill by (skill_id, version)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO skills "
                "(skill_id, version, skill_type, name, description, status, regimes_json, "
                " theme_tags_json, prompt_addendum, template, content_or_template, params_json, "
                " live_performance, trials, provenance, provenance_reflection_id, "
                " performance_metrics_json, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    skill.skill_id,
                    skill.version,
                    skill.skill_type.value,
                    skill.name,
                    skill.description,
                    skill.status.value,
                    json.dumps(skill.regimes),
                    json.dumps(skill.theme_tags),
                    skill.prompt_addendum,
                    skill.template,
                    skill.content_or_template,
                    json.dumps(skill.params),
                    skill.live_performance,
                    skill.trials,
                    skill.provenance,
                    skill.provenance_reflection_id,
                    json.dumps(skill.performance_metrics),
                    skill.created_ts,
                    _utc_now_iso(),
                ),
            )

    def get(self, skill_id: str, version: Optional[int] = None) -> Optional[Skill]:
        """Return a skill by id (latest version unless one is specified)."""
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT * FROM skills WHERE skill_id = ? ORDER BY version DESC LIMIT 1",
                    (skill_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM skills WHERE skill_id = ? AND version = ?",
                    (skill_id, version),
                ).fetchone()
        return self._row_to_skill(row) if row else None

    def all_skills(self) -> list[Skill]:
        """Return every stored skill row."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM skills").fetchall()
        return [self._row_to_skill(r) for r in rows]

    def list_by_status(self, status: SkillStatus) -> list[Skill]:
        """Return the latest version of every skill currently in `status`."""
        return [s for s in self._latest_per_id() if s.status is status]

    # ------------------------------------------------------------- retrieval

    def top_skills(
        self,
        skill_type: Optional[SkillType] = None,
        regime: Optional[str] = None,
        theme: Optional[str] = None,
        k: int = 5,
        status: SkillStatus = SkillStatus.PROMOTED,
    ) -> list[Skill]:
        """Top-K matching skills, ranked by live performance then recency.

        Structured filter by type, regime, and theme; only the latest version of
        each skill is considered. A skill with no regimes/theme tags applies
        everywhere.
        """
        skills = [s for s in self._latest_per_id() if s.status is status]
        if skill_type is not None:
            skills = [s for s in skills if s.skill_type is skill_type]
        if regime is not None:
            skills = [s for s in skills if not s.regimes or regime in s.regimes]
        if theme:
            theme_l = theme.lower()
            skills = [
                s for s in skills if not s.theme_tags or any(t.lower() in theme_l for t in s.theme_tags)
            ]
        skills.sort(key=lambda s: (s.live_performance, s.updated_ts), reverse=True)
        return skills[:k]

    def _latest_per_id(self) -> list[Skill]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM skills").fetchall()
        latest: dict[str, Skill] = {}
        for row in rows:
            skill = self._row_to_skill(row)
            current = latest.get(skill.skill_id)
            if current is None or skill.version > current.version:
                latest[skill.skill_id] = skill
        return list(latest.values())

    # ----------------------------------------------------------- transitions

    def set_shadow(self, skill_id: str, audit: Any) -> Skill:
        """Move a skill into shadow (analysis-only, before promotion)."""
        return self._transition(skill_id, SkillStatus.SHADOW, audit, "SKILL_SHADOWED")

    def demote(self, skill_id: str, audit: Any, reason: str = "") -> Skill:
        """Demote a skill. Always allowed: it reduces reliance, which is safe."""
        skill = self.get(skill_id)
        if skill is None:
            raise LearningError(f"unknown skill {skill_id}")
        before = skill.status
        skill.status = SkillStatus.DEMOTED
        self.upsert(skill)
        audit.record(
            "SKILL_DEMOTED",
            {"skill_id": skill_id, "before": before.value, "after": SkillStatus.DEMOTED.value,
             "reason": reason},
            f"Skill {skill_id} demoted ({before.value} -> demoted): {reason}",
        )
        return skill

    def promote(
        self,
        skill_id: str,
        experiment: Experiment,
        audit: Any,
        approval: Optional[Proposal] = None,
    ) -> Skill:
        """Promote a skill, enforcing the asymmetric automation rule.

        Raises LearningError unless the experiment PASSED and, for signal-shaping
        skills, a human approval from the queue AND a passing forward result are
        supplied. Risk-suggestion skills are never promotable.
        """
        skill = self.get(skill_id)
        if skill is None:
            raise LearningError(f"unknown skill {skill_id}")

        if experiment.verdict is not ExperimentVerdict.PASS:
            raise LearningError(
                f"cannot promote {skill_id}: experiment verdict is {experiment.verdict.value}, not pass"
            )
        if skill.skill_type is SkillType.RISK_SUGGESTION:
            raise LearningError(
                f"cannot promote {skill_id}: risk/execution skills are suggestion-only and never promoted"
            )
        if skill.skill_type is SkillType.SIGNAL_SHAPING:
            if approval is None or approval.status is not ProposalStatus.APPROVED:
                raise LearningError(
                    f"cannot promote signal-shaping skill {skill_id}: a human approval from the "
                    "queue is required"
                )
            if experiment.forward_result is None or not experiment.forward_result.passed:
                raise LearningError(
                    f"cannot promote signal-shaping skill {skill_id}: a passing paper-forward "
                    "result is required"
                )

        before = skill.status
        skill.status = SkillStatus.PROMOTED
        self.upsert(skill)
        audit.record(
            "SKILL_PROMOTED",
            {
                "skill_id": skill_id,
                "skill_type": skill.skill_type.value,
                "before": before.value,
                "after": SkillStatus.PROMOTED.value,
                "experiment_id": experiment.experiment_id,
                "verdict": experiment.verdict.value,
                "approval": approval.proposal_id if approval else None,
                "forward_passed": bool(experiment.forward_result and experiment.forward_result.passed),
            },
            f"Skill {skill_id} promoted ({before.value} -> promoted)",
        )
        return skill

    def _transition(self, skill_id: str, status: SkillStatus, audit: Any, event: str) -> Skill:
        skill = self.get(skill_id)
        if skill is None:
            raise LearningError(f"unknown skill {skill_id}")
        before = skill.status
        skill.status = status
        self.upsert(skill)
        audit.record(
            event,
            {"skill_id": skill_id, "before": before.value, "after": status.value},
            f"Skill {skill_id} {before.value} -> {status.value}",
        )
        return skill

    @staticmethod
    def _row_to_skill(row: sqlite3.Row) -> Skill:
        return Skill(
            skill_id=row["skill_id"],
            version=int(row["version"]),
            skill_type=SkillType(row["skill_type"]),
            name=row["name"],
            description=row["description"] or "",
            status=SkillStatus(row["status"]),
            regimes=json.loads(row["regimes_json"]),
            theme_tags=json.loads(row["theme_tags_json"]),
            prompt_addendum=row["prompt_addendum"] or "",
            template=row["template"],
            content_or_template=row["content_or_template"],
            params=json.loads(row["params_json"]),
            live_performance=float(row["live_performance"]),
            trials=int(row["trials"]),
            provenance=row["provenance"] or "",
            provenance_reflection_id=row["provenance_reflection_id"],
            performance_metrics=json.loads(row["performance_metrics_json"]),
            created_ts=row["created_ts"],
            updated_ts=row["updated_ts"],
        )

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
