"""Validation agent: mostly deterministic Python, not an LLM.

It runs each candidate spec through the stage-5 validation gate and attaches the
ValidationResult verbatim. The pass/fail verdict is computed ONLY from the gate
output. The LLM is used solely to write a plain-language summary of what the gate
already decided; that summary can never override or soften a FAIL.
"""
from __future__ import annotations

from typing import Any, Optional

from agents.provider import LLMProvider, audit_agent_usage
from core.contracts import AppliedSkill, Proposal, ProposalValidation, StrategyProposal
from strategies.base import GateAdapter
from strategies.registry import build_strategy
from utils.logging import get_logger

_log = get_logger(__name__)

_SUMMARY_SYSTEM = (
    "You write a short, plain-language summary of a validation gate's verdict for "
    "a human reviewer. You report what the gate decided; you NEVER override, "
    "soften, or dispute a FAIL. If it failed, say plainly that it failed and why."
)


async def run_validation(
    provider: LLMProvider,
    spec: StrategyProposal,
    data_source: Any,
    gate: Any,
    audit: Any,
    start: str,
    end: str,
    bar_size: str = "1 day",
    detector: Any = None,
    n_trials: int = 1,
    max_symbols: int = 3,
    applied_skills: Optional[list[AppliedSkill]] = None,
) -> Proposal:
    """Validate a spec on its universe and return a queued (PENDING) Proposal.

    Args:
        provider: LLM backend (used only for the cosmetic summary).
        spec: The candidate strategy spec.
        data_source: Bars source (get_historical_bars).
        gate: A ValidationGate.
        audit: Audit trail.
        start, end: Date range for the backtest data.
        detector: Optional shared regime detector passed to the gate.
        n_trials: Trials count fed to the Deflated Sharpe Ratio.
        max_symbols: Cap on how many universe symbols to validate.
        applied_skills: Skills active during this run, recorded on the proposal
            as provenance so the human approver sees them in the queue. Skills
            never change the verdict, which comes only from the gate.
    """
    validations: list[ProposalValidation] = []
    symbols = (spec.universe or [])[:max_symbols]
    for symbol in symbols:
        try:
            bars = data_source.get_historical_bars(symbol, start, end, bar_size)
        except Exception as exc:  # noqa: BLE001
            audit.record(
                "VALIDATION_DATA_ERROR",
                {"symbol": symbol, "template": spec.template, "error": str(exc)},
                f"Could not fetch data for {symbol}",
            )
            continue
        strategy = build_strategy(spec.template, spec.parameters, symbol=symbol)
        result = gate.validate(GateAdapter(strategy), bars, n_trials=n_trials, detector=detector)
        validations.append(ProposalValidation(symbol=symbol, result=result))

    # The verdict is purely the gate's: passed only if it validated at least one
    # symbol and EVERY validated symbol passed. The LLM cannot touch this.
    passed = bool(validations) and all(v.result.passed for v in validations)

    summary = await _summarize(provider, spec, validations, passed, audit)

    proposal = Proposal(
        spec=spec,
        validations=validations,
        passed=passed,
        summary=summary,
        applied_skills=list(applied_skills or []),
    )
    audit.record(
        "PROPOSAL_VALIDATED",
        {
            "proposal_id": proposal.proposal_id,
            "template": spec.template,
            "passed": passed,
            "symbols": [v.symbol for v in validations],
            "per_symbol_passed": {v.symbol: v.result.passed for v in validations},
            "applied_skills": [
                {"skill_id": s.skill_id, "version": s.version} for s in proposal.applied_skills
            ],
        },
        f"Proposal '{spec.name}' validated: passed={passed}",
    )
    _log.info("proposal_validated", name=spec.name, passed=passed, symbols=len(validations))
    return proposal


async def _summarize(
    provider: LLMProvider,
    spec: StrategyProposal,
    validations: list[ProposalValidation],
    passed: bool,
    audit: Any,
) -> str:
    """Ask the LLM for a plain-language summary of the gate's verdict only."""
    facts = {
        "name": spec.name,
        "template": spec.template,
        "passed": passed,
        "per_symbol": {
            v.symbol: {
                "passed": v.result.passed,
                "oos_net_sharpe": v.result.metrics.get("out_of_sample", {})
                .get("net", {})
                .get("sharpe"),
                "deflated_sharpe": v.result.deflated_sharpe,
                "reasons": v.result.reasons,
            }
            for v in validations
        },
    }
    try:
        response = await provider.text(
            agent="validation",
            system=_SUMMARY_SYSTEM,
            prompt=f"Summarize this validation verdict for a reviewer:\n{facts}",
        )
        audit_agent_usage(audit, "validation", response.usage, extra={"proposal": spec.name})
        return response.text
    except Exception as exc:  # noqa: BLE001
        # If the summary call fails, fall back to a deterministic sentence. The
        # verdict itself never depends on the LLM.
        _log.warning("validation_summary_failed", error=str(exc))
        verdict = "PASSED" if passed else "FAILED"
        return f"Gate {verdict} for '{spec.name}'. See the attached ValidationResult for details."
