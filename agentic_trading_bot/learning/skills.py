"""Skill registry: structured store and retrieval of promoted learning skills.

Per CLAUDE.md, skill retrieval is a structured query first: filter by type,
regime, and recency, then rank by live performance. Embeddings are a later
optional enhancement, not a launch dependency. The registry only stores and
serves skills; it never relaxes the gate and never reaches an order tool.
Signal-shaping skills are template + params that the strategy registry already
validates (invariant 14); analysis skills are prompt refinements (invariant 15).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from core.contracts import Skill, SkillStatus, SkillType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id         TEXT NOT NULL,
    version          INTEGER NOT NULL,
    skill_type       TEXT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    status           TEXT NOT NULL,
    regimes_json     TEXT NOT NULL DEFAULT '[]',
    theme_tags_json  TEXT NOT NULL DEFAULT '[]',
    prompt_addendum  TEXT NOT NULL DEFAULT '',
    template         TEXT,
    params_json      TEXT NOT NULL DEFAULT '{}',
    live_performance REAL NOT NULL DEFAULT 0,
    trials           INTEGER NOT NULL DEFAULT 0,
    provenance       TEXT NOT NULL DEFAULT '',
    created_ts       TEXT NOT NULL,
    updated_ts       TEXT NOT NULL,
    PRIMARY KEY (skill_id, version)
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def upsert(self, skill: Skill) -> None:
        """Insert or replace a skill by (skill_id, version)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO skills "
                "(skill_id, version, skill_type, name, description, status, regimes_json, "
                " theme_tags_json, prompt_addendum, template, params_json, live_performance, "
                " trials, provenance, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    json.dumps(skill.params),
                    skill.live_performance,
                    skill.trials,
                    skill.provenance,
                    skill.created_ts,
                    _utc_now_iso(),
                ),
            )

    def set_status(self, skill_id: str, status: SkillStatus) -> None:
        """Set the status of every version of a skill (demote is a safe action)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE skills SET status = ?, updated_ts = ? WHERE skill_id = ?",
                (status.value, _utc_now_iso(), skill_id),
            )

    def all_skills(self) -> list[Skill]:
        """Return every stored skill row."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM skills").fetchall()
        return [self._row_to_skill(r) for r in rows]

    def top_skills(
        self,
        skill_type: Optional[SkillType] = None,
        regime: Optional[str] = None,
        theme: Optional[str] = None,
        k: int = 5,
        status: SkillStatus = SkillStatus.PROMOTED,
    ) -> list[Skill]:
        """Return the top-K matching skills, ranked by live performance then recency.

        Args:
            skill_type: Restrict to a skill type, or None for any.
            regime: Detected regime; a skill matches if it lists this regime or
                lists none (applies everywhere). None means do not filter by regime.
            theme: Theme text; a skill matches if any of its tags appears in the
                theme, or it has no tags. None means do not filter by theme.
            k: Maximum number of skills to return.
            status: Required status (promoted by default).
        """
        clauses = ["status = ?"]
        params: list[Any] = [status.value]
        if skill_type is not None:
            clauses.append("skill_type = ?")
            params.append(skill_type.value)
        sql = "SELECT * FROM skills WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # Keep only the latest version of each skill_id.
        latest: dict[str, Skill] = {}
        for row in rows:
            skill = self._row_to_skill(row)
            current = latest.get(skill.skill_id)
            if current is None or skill.version > current.version:
                latest[skill.skill_id] = skill
        skills = list(latest.values())

        if regime is not None:
            skills = [s for s in skills if not s.regimes or regime in s.regimes]
        if theme:
            theme_l = theme.lower()
            skills = [
                s for s in skills if not s.theme_tags or any(t.lower() in theme_l for t in s.theme_tags)
            ]

        skills.sort(key=lambda s: (s.live_performance, s.updated_ts), reverse=True)
        return skills[:k]

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
            params=json.loads(row["params_json"]),
            live_performance=float(row["live_performance"]),
            trials=int(row["trials"]),
            provenance=row["provenance"] or "",
            created_ts=row["created_ts"],
            updated_ts=row["updated_ts"],
        )

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
