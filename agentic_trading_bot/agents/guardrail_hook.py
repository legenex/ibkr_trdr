"""PreToolUse guardrail: agents are structurally barred from order/broker tools.

The no-orders invariant is enforced here, not by instruction. A Claude Agent SDK
PreToolUse hook runs before every tool call and hard-blocks anything not on a
read-only allowlist. The default is DENY: only explicitly allowed read-only
tools (web search/fetch, file read, and the operator's research MCP connectors)
pass. Anything resembling an order or broker tool is denied outright, even if it
were somehow allowlisted, and the denial is written to the audit trail.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# Read-only tools agents may use.
DEFAULT_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {"WebSearch", "WebFetch", "Read", "Grep", "Glob"}
)

# Research MCP connector prefixes that are allowed (news, filings, research).
DEFAULT_RESEARCH_MCP_PREFIXES: tuple[str, ...] = (
    "mcp__news__",
    "mcp__filings__",
    "mcp__research__",
)

# Substrings that mark a tool as order/broker related. These are ALWAYS denied,
# even if a name somehow appears on an allowlist (defense in depth).
BROKER_DENY_SUBSTRINGS: tuple[str, ...] = (
    "order",
    "broker",
    "place",
    "cancel",
    "modify",
    "execute",
    "trade",
    "submit",
    "ibkr",
    "liquidate",
    "flatten",
    "bracket",
)


def tool_decision(
    tool_name: str,
    allowlist: frozenset[str] = DEFAULT_READ_ONLY_TOOLS,
    mcp_prefixes: tuple[str, ...] = DEFAULT_RESEARCH_MCP_PREFIXES,
) -> tuple[bool, str]:
    """Decide whether a tool may run. Pure and unit-testable.

    Returns (allowed, reason). Order/broker tools are denied first; then the
    explicit read-only allowlist and research MCP prefixes; everything else is
    denied by default.
    """
    name = tool_name or ""
    lowered = name.lower()
    if any(token in lowered for token in BROKER_DENY_SUBSTRINGS):
        return False, "matches order/broker denylist (agents cannot touch orders)"
    if name in allowlist:
        return True, "on read-only allowlist"
    if any(name.startswith(prefix) for prefix in mcp_prefixes):
        return True, "allowed research MCP connector"
    return False, "not on the read-only allowlist (default deny)"


def make_pretooluse_hook(
    audit: Any,
    allowlist: frozenset[str] = DEFAULT_READ_ONLY_TOOLS,
    mcp_prefixes: tuple[str, ...] = DEFAULT_RESEARCH_MCP_PREFIXES,
) -> Callable[..., Any]:
    """Build a Claude Agent SDK PreToolUse hook bound to an audit trail.

    The returned coroutine matches the SDK HookCallback signature
    (input_data, tool_use_id, context) and returns a deny decision for any tool
    that is not read-only, auditing the denial.
    """

    async def hook(input_data: dict[str, Any], tool_use_id: Optional[str], context: Any) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        allowed, reason = tool_decision(tool_name, allowlist, mcp_prefixes)
        if allowed:
            return {}  # allow: defer to the normal permission flow
        tool_input = input_data.get("tool_input") or {}
        audit.record(
            "AGENT_TOOL_DENIED",
            {
                "tool": tool_name,
                "reason": reason,
                "tool_input_keys": sorted(tool_input.keys()) if isinstance(tool_input, dict) else [],
                "tool_use_id": tool_use_id,
            },
            f"PreToolUse guardrail denied tool '{tool_name}': {reason}",
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Blocked by the read-only research guardrail: {reason}. Agents may "
                    "research and propose, but can never place, modify, or cancel orders."
                ),
            }
        }

    return hook


def build_hooks(audit: Any, **kwargs: Any) -> dict[str, Any]:
    """Build the SDK `hooks` mapping wiring the guardrail to PreToolUse events."""
    from claude_agent_sdk import HookMatcher

    hook = make_pretooluse_hook(audit, **kwargs)
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}
