"""Provider interface for LLM calls, so any agent can swap models.

Every model call in the discovery pipeline goes through `LLMProvider`. Two
implementations are shipped:

  - `ClaudeProvider`: uses the Claude Agent SDK. Tool-using agents go through
    ClaudeSDKClient (the tool loop); simple structured generations go through
    query(). Usage and cost are read from the SDK ResultMessage.
  - `ScriptedProvider`: returns canned structured objects with no network. Used
    by tests and by the CLI when no API access is available, so the pipeline
    plumbing and the (real) validation gate can be exercised offline.

Usage is metered to the audit trail via `audit_agent_usage`, because Agent SDK
usage on subscription plans draws from a separate monthly credit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Type

from pydantic import BaseModel

from utils.logging import get_logger

_log = get_logger(__name__)


@dataclass
class ProviderUsage:
    """Token and credit usage for one agent run."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None  # Agent SDK credit proxy (subscription credit)

    def as_payload(self) -> dict[str, Any]:
        """Serializable usage payload for the audit trail."""
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class ProviderResponse:
    """Result of a provider call: parsed data (if any), raw text, and usage."""

    text: str
    usage: ProviderUsage
    data: Any = None


class LLMProvider(Protocol):
    """Swappable LLM backend. Any single agent can be pointed at a different one."""

    name: str

    async def structured(
        self,
        *,
        agent: str,
        system: str,
        prompt: str,
        schema: Type[BaseModel],
        allowed_tools: Optional[list[str]] = None,
        mcp_servers: Optional[dict[str, Any]] = None,
        hooks: Optional[dict[str, Any]] = None,
        max_turns: int = 1,
    ) -> ProviderResponse: ...

    async def text(self, *, agent: str, system: str, prompt: str) -> ProviderResponse: ...


def audit_agent_usage(audit: Any, agent: str, usage: ProviderUsage, extra: Optional[dict] = None) -> None:
    """Record one metered agent run to the audit trail."""
    payload = {"agent": agent, **usage.as_payload()}
    if extra:
        payload.update(extra)
    audit.record(
        "AGENT_RUN",
        payload,
        f"Agent '{agent}' run metered: {usage.total_tokens} tokens, cost_usd={usage.cost_usd}",
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from model text."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON object found in model output")


# ---------------------------------------------------------------------------
# Scripted / offline provider
# ---------------------------------------------------------------------------


@dataclass
class ScriptedProvider:
    """Deterministic provider returning canned objects. No network.

    Matches a requested schema by class name: supply a `responses` map from
    schema class name to a pydantic instance, plus a `summary` string for plain
    text calls. Records every call in `calls` for assertions.
    """

    name: str = "scripted"
    responses: dict[str, BaseModel] = field(default_factory=dict)
    summary: str = "Plain-language summary (scripted)."
    usage: ProviderUsage = field(default_factory=lambda: ProviderUsage(model="scripted", total_tokens=0))
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def structured(
        self,
        *,
        agent: str,
        system: str,
        prompt: str,
        schema: Type[BaseModel],
        allowed_tools: Optional[list[str]] = None,
        mcp_servers: Optional[dict[str, Any]] = None,
        hooks: Optional[dict[str, Any]] = None,
        max_turns: int = 1,
    ) -> ProviderResponse:
        self.calls.append({"agent": agent, "schema": schema.__name__, "kind": "structured"})
        data = self.responses.get(schema.__name__)
        if data is None:
            raise KeyError(f"ScriptedProvider has no canned response for {schema.__name__}")
        return ProviderResponse(text=data.model_dump_json(), usage=self.usage, data=data)

    async def text(self, *, agent: str, system: str, prompt: str) -> ProviderResponse:
        self.calls.append({"agent": agent, "kind": "text"})
        return ProviderResponse(text=self.summary, usage=self.usage, data=None)


# ---------------------------------------------------------------------------
# Claude Agent SDK provider
# ---------------------------------------------------------------------------


class ClaudeProvider:
    """LLMProvider backed by the Claude Agent SDK.

    Tool-using structured calls (research) go through ClaudeSDKClient so the
    agent gets the full tool loop; tool-free structured calls (signal) and plain
    text calls (validation summary) go through query(). The SDK and an API
    credential are required at call time; the import is lazy so this module loads
    without them.
    """

    name = "claude"

    def __init__(self, model: Optional[str] = None) -> None:
        """Create the provider, optionally pinning a model id."""
        self.model = model

    def _options(
        self,
        system: str,
        allowed_tools: Optional[list[str]],
        mcp_servers: Optional[dict[str, Any]],
        hooks: Optional[dict[str, Any]],
        max_turns: int,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            system_prompt=system,
            allowed_tools=allowed_tools or [],
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
            mcp_servers=mcp_servers or {},
            hooks=hooks or {},
            max_turns=max_turns,
            model=self.model,
            permission_mode="default",
        )

    @staticmethod
    def _usage_from_result(result: Any, model: str) -> ProviderUsage:
        usage = getattr(result, "usage", None) or {}
        return ProviderUsage(
            model=getattr(result, "model", None) or model or "claude",
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            total_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            cost_usd=getattr(result, "total_cost_usd", None),
        )

    async def _drain_query(self, prompt: str, options: Any) -> tuple[str, Any, Any]:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

        text_parts: list[str] = []
        result_msg = None
        structured = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                result_msg = message
                structured = getattr(message, "structured_output", None)
                if getattr(message, "result", None):
                    text_parts.append(message.result)
        return "".join(text_parts), structured, result_msg

    async def _drain_client(self, prompt: str, options: Any) -> tuple[str, Any, Any]:
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock

        text_parts: list[str] = []
        result_msg = None
        structured = None
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result_msg = message
                    structured = getattr(message, "structured_output", None)
                    if getattr(message, "result", None):
                        text_parts.append(message.result)
        return "".join(text_parts), structured, result_msg

    async def structured(
        self,
        *,
        agent: str,
        system: str,
        prompt: str,
        schema: Type[BaseModel],
        allowed_tools: Optional[list[str]] = None,
        mcp_servers: Optional[dict[str, Any]] = None,
        hooks: Optional[dict[str, Any]] = None,
        max_turns: int = 1,
    ) -> ProviderResponse:
        schema_json = json.dumps(schema.model_json_schema())
        full_prompt = (
            f"{prompt}\n\nReturn ONLY a JSON object matching this schema (no prose):\n{schema_json}"
        )
        options = self._options(system, allowed_tools, mcp_servers, hooks, max_turns)

        uses_tools = bool(allowed_tools) or bool(mcp_servers)
        if uses_tools:
            text, structured, result = await self._drain_client(full_prompt, options)
        else:
            text, structured, result = await self._drain_query(full_prompt, options)

        payload = structured if isinstance(structured, dict) else _extract_json(text)
        data = schema.model_validate(payload)
        return ProviderResponse(text=text, usage=self._usage_from_result(result, self.model or ""), data=data)

    async def text(self, *, agent: str, system: str, prompt: str) -> ProviderResponse:
        options = self._options(system, None, None, None, 1)
        text, _structured, result = await self._drain_query(prompt, options)
        return ProviderResponse(text=text, usage=self._usage_from_result(result, self.model or ""))
