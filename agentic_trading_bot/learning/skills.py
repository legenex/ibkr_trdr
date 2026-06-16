"""Backward-compatible alias for the skill registry.

The authoritative implementation lives in learning.registry. The skill-aware
agents (Stage 7.5a) import SkillRegistry from here; keep this re-export so that
import path stays valid.
"""
from learning.registry import LearningError, SkillRegistry

__all__ = ["SkillRegistry", "LearningError"]
