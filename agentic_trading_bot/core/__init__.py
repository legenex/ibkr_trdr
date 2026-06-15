"""Core message contracts shared across module boundaries.

Every model here is a stable interface. Orders, risk decisions, fills, and
positions all flow between the broker, the risk gate, strategies, and the UI as
these pydantic types, so a change here is a cross-cutting change.
"""
