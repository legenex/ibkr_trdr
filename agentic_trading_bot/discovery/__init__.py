"""Agentic discovery: the thin async pipeline wiring the three agents.

research -> signal -> validation, then enqueue for human approval. Approval is
fully decoupled and happens later in the UI; nothing here auto-approves and no
agent can execute orders.
"""
