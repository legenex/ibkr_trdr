"""Helpers that fold promoted skills into agent prompt context, additively.

These turn Skill objects into prompt text. They are deliberately pure and depend
only on the shared contracts, so the agents do not import the learning store.
Empty skill lists produce empty strings, so an empty registry leaves every
prompt byte-for-byte unchanged.

A skill can only ADD framing (analysis) or SUGGEST a known template+params
(signal-shaping). It cannot relax the gate, flip a FAIL, or add a tool.
"""
from __future__ import annotations

from typing import Iterable

from core.contracts import Skill


def analysis_addendum(skills: Iterable[Skill]) -> str:
    """Render analysis-only skills as a framing addendum, or '' if none apply."""
    lines = [
        f"- [{s.skill_id} v{s.version}] {s.prompt_addendum.strip()}"
        for s in skills
        if s.prompt_addendum and s.prompt_addendum.strip()
    ]
    if not lines:
        return ""
    return (
        "\n\nApplied analysis skills (framing/prompt refinements only; these never "
        "change what gets traded and never relax validation):\n" + "\n".join(lines)
    )


def signal_candidate_addendum(skills: Iterable[Skill], known_templates: list[str]) -> str:
    """Render signal-shaping skills as additional candidate templates, or ''.

    Only skills whose template is already in the registry are offered (invariant
    14); unknown templates are dropped here and any unknown params are dropped
    later when the strategy is built.
    """
    lines = []
    for s in skills:
        if s.template and s.template in known_templates:
            lines.append(
                f"- template={s.template} params={s.params} "
                f"(skill {s.skill_id} v{s.version}, live_performance={s.live_performance:.3f})"
            )
    if not lines:
        return ""
    return (
        "\n\nPromoted signal-shaping skills available as additional candidate "
        "templates (you MAY start from these template+params; they still must pass "
        "the full validation gate, and unknown templates or params are dropped):\n"
        + "\n".join(lines)
    )
