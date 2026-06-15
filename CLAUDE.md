# CLAUDE.md: Agentic Trading Research and Execution Harness

## What this project is, and is not
This is a research, validation, and execution harness for trading through Interactive Brokers (IBKR). It does NOT contain or generate a trading edge. The human operator supplies hypotheses. The system's job is to validate those hypotheses honestly and execute approved ones inside hard risk limits.

Treat every claimed strategy as unprofitable until it survives out-of-sample testing, walk-forward testing, realistic transaction costs, and a multiple-testing correction. A backtest equity curve is never, by itself, evidence of an edge.

## Non-negotiable invariants (never violate, never refactor away, never "optimize" past)
1. Paper trading is the default. Live trading requires BOTH an explicit environment flag (LIVE_TRADING=true) AND a typed runtime confirmation string. Absent either, the broker connects to the paper port only.
2. The risk guardrails module is the final gate. Every order, from every source (agent, strategy, or manual UI action), passes through it before submission. Guardrails can VETO an order but can never CREATE one. If the guardrails module fails to import or initialize, no orders are sent at all.
3. No order is a naked market order by default. Every entry is a bracket order (entry, protective stop, target) or carries an attached stop. A position without a stop is a bug.
4. No strategy is marked "approvable" unless it passes the validation gate (defined below). 
5. Nothing auto-executes a newly discovered signal. The path for any new signal is strictly: validation gate PASS, then human approval, then risk gate, then execution. There is no shortcut, including for the operator.
6. Every order, fill, veto, approval, rejection, and agent decision is written to an append-only audit log with UTC timestamps and the reason.
7. A global kill switch halts all new order submission immediately. It is reachable from the UI and from a CLI signal (a sentinel file the main loop checks every cycle). The kill switch does not auto-liquidate. Flattening is a separate, explicit human action.
8. The agents can read, research, propose, and explain. They cannot place, modify, or cancel orders directly. They emit proposals only.

## Anti-overfitting and honesty standards (the part that actually matters)
The validation gate is the heart of this system. A strategy passes only if ALL of the following hold:
- Out-of-sample holdout: the final 20 to 30 percent of the timeline is never touched during development or parameter selection. Results are reported separately on it.
- Walk-forward analysis: rolling train and test windows. Report the distribution of out-of-sample results across windows, not a single number.
- Purged and embargoed cross validation for any ML step, to prevent leakage across the train and test boundary (Lopez de Prado style).
- Realistic costs applied to every fill: commission, half-spread, a slippage model that scales with size versus average volume, and borrow cost for shorts. Report gross and net side by side.
- Multiple-testing correction: when N parameter sets or strategies were tried, the headline Sharpe is corrected. Implement the Deflated Sharpe Ratio (Bailey and Lopez de Prado) and report it. A nominal Sharpe that vanishes after deflation is a FAIL.
- Minimum sample: a hard floor on the number of independent trades and the calendar span. Too few trades is an automatic FAIL regardless of how good the curve looks.
- Parameter sensitivity: the strategy must not collapse when each key parameter is perturbed plus or minus a step. A knife-edge optimum is a FAIL.
- Regime-conditional reporting: show performance broken out by detected regime, so a strategy that only worked in one bull run is visible as such.

The gate returns a structured result: PASS or FAIL, the metric table (gross and net), and a list of the specific reasons for any FAIL. The UI and the approval flow consume this object. A FAIL cannot be approved.

## No lookahead bias (enforced, not hoped for)
- Any feature computed at time t uses only data available at or before t.
- Signals are shifted relative to the bar they act on. You trade the next bar's open, not the close you computed the signal from, unless explicitly modeling close execution with justification.
- Indicator warmup periods are dropped, not backfilled.
- When in doubt, write a test that would fail if a future value leaked in.

## Tech stack (intended; verify the latest stable version at build time and pin it)
- Python 3.11 or newer.
- Broker: ib_async (the maintained fork of ib_insync; drop-in API; requires a running TWS or IB Gateway with the API port enabled). Pin the latest 2.x. Ports: paper TWS 7497, live TWS 7496, IB Gateway paper 4002, IB Gateway live 4001.
- Orchestration and agents: the Claude Agent SDK (pip install claude-agent-sdk, Python 3.10+, bundles the Claude Code CLI). No heavy multi-agent framework. The three agents are plain async functions wired by a thin custom pipeline. Use ClaudeSDKClient for agents that need tools, MCP, and hooks; use query() for simple structured generations. Approval is decoupled from execution through the queue table, so no durable graph interrupt or resume is needed. Keep all LLM calls behind a thin provider interface so a non-Claude model can be swapped in for any single agent. Note: as of June 15 2026, Agent SDK usage on subscription plans draws from a separate monthly Agent SDK credit, so meter agent invocations.
- Data and ML: pandas, numpy, scikit-learn, hmmlearn, statsmodels, scipy, pandas_ta (prefer this over TA-Lib to avoid a C build dependency, unless TA-Lib is explicitly wanted).
- Backtesting: an event-driven engine (custom) with the mandatory cost model, or vectorbt if vectorized is acceptable for a given strategy. The cost model is not optional.
- UI: Streamlit, dark mode, with tabs and a visible kill switch.
- Infra: APScheduler for scheduling, pydantic for config and message schemas, python-dotenv for secrets, loguru or structlog for logging, plotly for charts.
- Data fallback: yfinance or polygon as a backup to IBKR historical data, behind a common data interface so the source is swappable.
- Tests: pytest.

## Coding standards
- No em dashes anywhere.
- Output complete files, not diffs or patches, unless the change is a single localized edit.
- Type hints and docstrings on every public function and class.
- Pydantic models for every message that crosses a module boundary (proposals, orders, validation results, risk decisions).
- Deterministic seeds for any randomness, set centrally.
- Secrets only in .env, which is gitignored. Never hardcode keys.
- Pure functions where possible. Side effects (orders, IO, network) isolated in clearly named modules.
- Each module ships with its own pytest tests.

## Project layout
agentic_trading_bot/
  main.py                  orchestrator and scheduler
  config.py                typed config loaded from .env
  .env.example
  requirements.txt
  agents/                  research, signal, validation proposing agents (Claude Agent SDK)
  models/regime_detector.py
  strategies/              strategy interface and reference strategies
  risk/guardrails.py       the independent final gate
  broker/ibkr_client.py    ib_async wrapper, paper and live toggle
  data/                    data interface, IBKR and fallback sources, caching
  backtest/validator.py    backtest engine and the validation gate
  discovery/research_pipeline.py   the thin async pipeline wiring the agents
  ui/dashboard.py          streamlit app
  utils/logging.py
  utils/audit.py           append-only audit trail
  journal/                 logs, approved strategies, audit db
  tests/

## Build order (do not skip ahead)
1 scaffolding and config and data interface
2 broker client
3 risk guardrails (with tests) before anything can trade
4 regime detector
5 backtest engine and the validation gate (before agents, so agents have a gate to pass)
6 strategy interface and two reference strategies
7 agentic discovery (Claude Agent SDK) with decoupled human-in-the-loop approval
8 streamlit UI
9 orchestrator, main loop, scheduler, end to end on paper

## When unsure
Ask the operator a specific question rather than guessing on anything touching money, risk, or order routing. Prefer a safe default (smaller size, paper, veto) over an unsafe one.