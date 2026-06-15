"""Thin async discovery pipeline: research -> signal -> validation, then queue.

One shot and linear. It produces proposals plus their ValidationResults, writes
them to the approval queue, and stops. Approval does NOT happen here: a human
approves later in the UI, fully decoupled. Every proposal (including failures)
and every agent run is written to the audit trail.

This module uses the Claude Agent SDK indirectly, through the LLMProvider
interface, with no LangGraph and no CrewAI. The agents are plain async functions
wired together below.

Approval grants permission to execute on PAPER only. Promotion to live is a
separate manual step gated by the CLAUDE.md invariants.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any, Optional

from agents.guardrail_hook import make_pretooluse_hook
from agents.provider import LLMProvider, ScriptedProvider
from agents.research_agent import run_research
from agents.signal_agent import StrategyProposalList, run_signal
from agents.validation_agent import run_validation
from config import settings as default_settings
from core.contracts import (
    AppliedSkill,
    Proposal,
    ResearchBrief,
    Skill,
    SkillType,
    Source,
    StrategyProposal,
)
from discovery.approval_queue import ApprovalQueue
from utils.logging import get_logger

_log = get_logger(__name__)


def _build_hooks(audit: Any) -> Optional[dict[str, Any]]:
    """Build the PreToolUse guardrail hooks mapping, if the SDK is available."""
    try:
        from claude_agent_sdk import HookMatcher

        hook = make_pretooluse_hook(audit)
        return {"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}
    except Exception:  # noqa: BLE001 - SDK not installed; scripted provider ignores hooks
        return None


class ResearchPipeline:
    """Wires the three agents and the approval queue into one linear run."""

    def __init__(
        self,
        provider: LLMProvider,
        data_source: Any,
        gate: Any,
        queue: ApprovalQueue,
        audit: Any,
        mcp_servers: Optional[dict[str, Any]] = None,
        detector: Any = None,
        skill_registry: Any = None,
        use_skills: bool = False,
        skill_top_k: int = 3,
    ) -> None:
        """Create the pipeline from its (injected) collaborators.

        Skills are additive and OFF by default. With `use_skills=True` and a
        registry that returns nothing, behavior is identical to the no-skills
        path (no prompt changes, no provenance, no SKILLS_APPLIED audit).
        """
        self.provider = provider
        self.data_source = data_source
        self.gate = gate
        self.queue = queue
        self.audit = audit
        self.mcp_servers = mcp_servers or {}
        self.detector = detector
        self.skill_registry = skill_registry
        self.use_skills = use_skills
        self.skill_top_k = skill_top_k
        self.log = get_logger(__name__)

    def _select_skills(
        self, theme: str, regime: Optional[str]
    ) -> tuple[list[Skill], list[Skill], list[AppliedSkill]]:
        """Query the registry for promoted analysis and signal-shaping skills.

        Returns ([], [], []) when skills are disabled or the registry is empty,
        which keeps the run byte-for-byte identical to the no-skills path.
        """
        if not self.use_skills or self.skill_registry is None:
            return [], [], []
        analysis = self.skill_registry.top_skills(
            skill_type=SkillType.ANALYSIS, regime=regime, theme=theme, k=self.skill_top_k
        )
        signal = self.skill_registry.top_skills(
            skill_type=SkillType.SIGNAL_SHAPING, regime=regime, theme=theme, k=self.skill_top_k
        )
        applied = [s.as_applied() for s in (analysis + signal)]
        return analysis, signal, applied

    async def run(
        self,
        theme: str,
        symbols: Optional[list[str]] = None,
        start: str = "2023-06-15",
        end: str = "2026-06-16",
        n_trials: int = 1,
        current_regime: Optional[str] = None,
    ) -> list[Proposal]:
        """Run research -> signal -> validation and enqueue the proposals."""
        self.audit.record("PIPELINE_START", {"theme": theme, "symbols": symbols}, "Discovery pipeline started")
        hooks = _build_hooks(self.audit)

        analysis_skills, signal_skills, applied_skills = self._select_skills(theme, current_regime)
        if applied_skills:
            # Invariant: a SKILLS_APPLIED event per run lists the skill ids/versions.
            self.audit.record(
                "SKILLS_APPLIED",
                {
                    "regime": current_regime,
                    "theme": theme,
                    "skills": [
                        {"skill_id": s.skill_id, "version": s.version, "skill_type": s.skill_type}
                        for s in applied_skills
                    ],
                },
                f"{len(applied_skills)} promoted skill(s) applied this run",
            )

        brief = await run_research(
            self.provider,
            theme,
            self.audit,
            watchlist=symbols,
            mcp_servers=self.mcp_servers,
            hooks=hooks,
            analysis_skills=analysis_skills,
        )
        specs = await run_signal(
            self.provider,
            brief,
            self.audit,
            universe=symbols,
            analysis_skills=analysis_skills,
            signal_skills=signal_skills,
        )

        proposals: list[Proposal] = []
        for spec in specs:
            proposal = await run_validation(
                self.provider,
                spec,
                self.data_source,
                self.gate,
                self.audit,
                start=start,
                end=end,
                detector=self.detector,
                n_trials=n_trials,
                applied_skills=applied_skills,
            )
            self.queue.enqueue(proposal)
            self.audit.record(
                "PROPOSAL_ENQUEUED",
                {"proposal_id": proposal.proposal_id, "name": spec.name, "passed": proposal.passed},
                f"Proposal '{spec.name}' enqueued for human approval (passed={proposal.passed})",
            )
            proposals.append(proposal)

        n_passed = sum(1 for p in proposals if p.passed)
        self.audit.record(
            "PIPELINE_COMPLETE",
            {"theme": theme, "n_proposals": len(proposals), "n_passed": n_passed},
            "Discovery pipeline complete; proposals queued for human approval",
        )
        self.log.info("pipeline_complete", proposals=len(proposals), passed=n_passed)
        return proposals


# ---------------------------------------------------------------------------
# Offline provider for the CLI/tests (no network, no API credential)
# ---------------------------------------------------------------------------


def offline_provider(theme: str, symbols: list[str]) -> ScriptedProvider:
    """A ScriptedProvider with a canned brief and two concrete specs.

    The LLM steps are stubbed; the validation gate that follows is REAL and runs
    on whatever data the data source returns.
    """
    brief = ResearchBrief(
        theme=theme,
        summary=(
            "OFFLINE STUB brief. No live model or web search was used. This exists "
            "so the pipeline plumbing and the real validation gate can run without "
            "API access."
        ),
        key_points=[
            "These specs are illustrative test subjects, not claimed edges.",
            "Validation below is real and is expected to FAIL most candidates.",
        ],
        watchlist=list(symbols),
        sources=[Source(title="(offline stub: no sources fetched)", kind="web")],
    )
    proposals = StrategyProposalList(
        proposals=[
            StrategyProposal(
                name="trend-breakout-donchian",
                hypothesis="Persistent trends can be ridden via channel breakouts.",
                template="trend_breakout",
                parameters={"channel": 20, "atr_window": 14, "atr_mult": 3.0},
                universe=list(symbols),
                intended_regimes=["Bull", "Bear"],
                intended_stop="ATR(14) x 3.0 from entry (below for longs, above for shorts).",
                rationale="Illustrative trend subject.",
            ),
            StrategyProposal(
                name="mean-reversion-zscore",
                hypothesis="Short-term dislocations revert toward the rolling mean.",
                template="mean_reversion",
                parameters={"lookback": 20, "entry_z": 1.5, "exit_z": 0.3, "stop_pct": 0.05},
                universe=list(symbols),
                intended_regimes=["Neutral"],
                intended_stop="Fixed 5% from entry; flat in Crash/Euphoria regimes.",
                rationale="Illustrative reversion subject.",
            ),
        ]
    )
    return ScriptedProvider(
        name="offline",
        responses={"ResearchBrief": brief, "StrategyProposalList": proposals},
        summary="(offline stub summary) See the attached ValidationResult for the gate's verdict.",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_results(theme: str, proposals: list[Proposal], live: bool) -> None:
    print("#" * 78)
    print("# AGENTIC DISCOVERY - proposals only. Agents cannot place, modify, or cancel")
    print("# orders (enforced by a PreToolUse guardrail hook). Nothing here is approved")
    print("# or executed. A human approves later in the UI; approval grants PAPER")
    print("# execution ONLY. These reference strategies are illustrative, not edges.")
    if not live:
        print("# NOTE: offline stub mode - the LLM steps were canned; the gate results are REAL.")
    print("#" * 78)
    print(f"\nTheme: {theme}\nProposals queued: {len(proposals)}\n")
    for p in proposals:
        verdict = "PASS (eligible for human approval)" if p.passed else "FAIL (cannot be approved)"
        print(f"  - {p.spec.name}  [{p.spec.template}]  -> {verdict}   id={p.proposal_id}")
        print(f"      intended stop: {p.spec.intended_stop}")
        for v in p.validations:
            oos = v.result.metrics.get("out_of_sample", {})
            g = oos.get("gross", {}).get("sharpe")
            n = oos.get("net", {}).get("sharpe")
            print(
                f"      {v.symbol}: gate {'PASS' if v.result.passed else 'FAIL'}  "
                f"OOS Sharpe gross {g:+.2f} | net {n:+.2f}  DSR {v.result.deflated_sharpe:.3f}"
            )
            if v.result.reasons:
                print(f"        reasons: {v.result.reasons}")
    print("\nProposals were written to the approval queue. Approval is a separate human")
    print("step in the UI and is gated by ValidationResult.passed AND a human decision.")


async def _amain(args: argparse.Namespace) -> None:
    from backtest.validator import ValidationGate
    from data.yfinance_source import YFinanceDataSource
    from models.regime_detector import RegimeDetector
    from utils.audit import get_audit_trail

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    audit = get_audit_trail()

    if args.live:
        from agents.provider import ClaudeProvider

        provider: LLMProvider = ClaudeProvider(model=args.model)
    else:
        provider = offline_provider(args.theme, symbols)

    queue = ApprovalQueue(default_settings.journal_path / "approval_queue.db")
    detector = RegimeDetector(n_iter=30, window=20, random_seed=default_settings.random_seed)
    pipeline = ResearchPipeline(
        provider=provider,
        data_source=YFinanceDataSource(),
        gate=ValidationGate(),
        queue=queue,
        audit=audit,
        detector=detector,
    )
    proposals = await pipeline.run(args.theme, symbols=symbols, start=args.start, end=args.end)
    _print_results(args.theme, proposals, live=args.live)
    queue.close()


def main() -> None:
    """CLI entry: run the discovery pipeline on a theme and print the results."""
    parser = argparse.ArgumentParser(description="Run the agentic discovery pipeline on a theme.")
    parser.add_argument("--theme", required=True, help="Theme or question to research.")
    parser.add_argument("--symbols", default="AAPL,MSFT,SPY", help="Comma-separated universe.")
    parser.add_argument("--start", default="2023-06-15", help="Backtest data start date.")
    parser.add_argument("--end", default="2026-06-16", help="Backtest data end date.")
    parser.add_argument("--model", default=None, help="Model id for the live Claude provider.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the Claude Agent SDK (requires API access). Default is the offline stub.",
    )
    asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    main()
