"""Idempotent upsert of KEY=value lines into the .env the backend loads.

Settings the operator changes in the console are persisted here so the backend
enforces them on the next load (fixing the old session-only behavior). Existing
keys are replaced in place; new keys are appended; comments and unrelated keys
are preserved.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union


def upsert_env(env_path: Union[str, Path], updates: dict[str, str]) -> None:
    """Write/replace the given KEY=value pairs in the .env file."""
    if not updates:
        return
    path = Path(env_path)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().upper()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
