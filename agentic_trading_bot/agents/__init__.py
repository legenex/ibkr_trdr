"""Discovery agents: research, signal, and validation.

Each agent is a plain async function that emits structured pydantic proposals
only. No agent can place, modify, or cancel orders; that is enforced
structurally by the PreToolUse guardrail hook, not by instruction. All model
calls go through the LLMProvider interface so any single agent can be pointed at
a non-Claude model.
"""
