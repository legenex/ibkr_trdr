# CLAUDE.md: Agentic Trading Research and Execution Harness

## What this project is, and is not
This is a research, validation, and execution harness for trading through Interactive Brokers (IBKR). It does NOT contain or generate a trading edge. The human operator supplies hypotheses. The system's job is to validate those hypotheses honestly and execute approved ones inside hard risk limits.

Treat every claimed strategy as unprofitable until it survives out-of-sample testing, walk-forward testing, realistic transaction costs, and a multiple-testing correction. A backtest equity curve is never, by itself, evidence of an edge.

The system also learns from its own outcomes over time (see Self-learning discipline below). That capability raises the stakes on honesty, not lowers them: a loop that improves itself can also fool itself faster than any human, because it can try more things. The learning layer's job is to make the system harder to fool, never to chase a better backtest number. If the loop is promoting most of what it tries, it is overfitting, not learning. A healthy loop rejects the large majority of its own ideas.

## Non-negotiable invariants (never violate, never refactor away, never "optimize" past)
1. Paper trading is the default. Live trading requires BOTH an explicit environment flag (LIVE_TRADING=true) AND a typed runtime confirmation string. Absent either, the broker connects to the paper port only.
2. The risk guardrails module is the final gate. Every order, from every source (agent, strategy, or manual UI action), passes through it before submission. Guardrails can VETO an order but can never CREATE one. If the guardrails module fails to import or initialize, no orders are sent at all.
3. No order is a naked market order by default. Every entry is a bracket order (entry, protective stop, target) or carries an attached stop. A position without a stop is a bug.
4. No strategy is marked "approvable" unless it passes the validation gate (defined below). 
5. Nothing auto-executes a newly discovered signal. The path for any new signal is strictly: validation gate PASS, then human approval, then risk gate, then execution. There is no shortcut, including for the operator.
6. Every order, fill, veto, approval, rejection, and agent decision is written to an append-only audit log with UTC timestamps and the reason.
7. A global kill switch halts all new order submission immediately. It is reachable from the UI and from a CLI signal (a sentinel file the main loop checks every cycle). The kill switch does not auto-liquidate. Flattening is a separate, explicit human action.
8. The agents can read, research, propose, and explain. They cannot place, modify, or cancel orders directly. They emit proposals only.
9. The learning loop proposes only. It never executes an order, never edits the risk gate, never edits the execution path, and never auto-deploys a strategy. Risk and execution code are outside its reach entirely. The loop writes to the skills registry, the approval queue, and the audit log, and nowhere else.
10. Automatic actions are allowed only when they REDUCE risk or reliance: demote a skill, pause, suggest a flatten. Any automatic action that INCREASES risk or reliance (promote a signal-shaping skill, deploy a strategy, change a default the trader uses) requires the full validation gate PLUS paper-forward confirmation PLUS human approval. This asymmetry is the core safety rule of the learning layer.
11. Every hypothesis ever tested is charged to a persistent cumulative trial ledger. All overfitting corrections (deflated Sharpe, and PBO if implemented) use the CUMULATIVE trial count for the relevant strategy family, not the per-run count. Selecting a skill because it beat the holdout is itself a use of the holdout and is charged.
12. Truly-unseen data is a budgeted, consumable resource. The learning loop may evaluate against a given holdout tranche only a fixed number of times before that tranche is burned and rotated out. When the budget is exhausted, no promotion can happen until new data accrues.
13. Pre-registration: the success criteria (metric and threshold) for any experiment are fixed and stored BEFORE the test runs. Promotion logic reads the pre-registered criteria, never a metric computed after seeing the result. One variable changes per experiment versus a frozen baseline.
14. No black-box mutations. An LLM never writes free-form executable strategy or risk code. A signal-shaping skill is expressed only as parameters or templates that the existing strategy registry already validates, so an LLM proposal becomes a runnable strategy with zero code execution. Unknown templates and params are dropped, exactly as in the Stage 7 pipeline.
15. Analysis-only skills (research prompt refinements, summarization templates, how a brief is framed) may auto-apply after a shadow A/B comparison and must remain reversible and audited. Nothing that shapes what gets traded may ever auto-apply.

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

## Self-learning discipline (how the system improves without fooling itself)
The system reflects on its own outcomes, proposes small improvements as reusable skills, tests them, and promotes only the ones that earn it. The danger is that an automated loop is an overfitting engine unless disciplined harder than a human would be. The rules:

- Skill taxonomy by blast radius, with different bars:
  - Analysis-only skills (research prompt or summary refinements): lowest blast radius. May auto-apply after winning a shadow A/B on held-out cases. Fully reversible and audited. Never touch what gets traded.
  - Signal-shaping skills (new signal rules, parameter heuristics, invalidation conditions, regime features): change what gets proposed and possibly traded. Treated exactly like a new strategy. Must pass the full validation gate with cumulative-trial-corrected deflated Sharpe, then paper-forward confirmation, then human approval through the same queue. No auto-promotion, ever.
  - Risk and execution skills: not auto-generated and not auto-applied at all. The loop may write a logged SUGGESTION for a human, and that is the entire extent of its reach into risk or execution.
- The scientific method, enforced: snapshot a frozen baseline, change exactly one variable, pre-register the success metric and threshold, run the controlled test on the next unburned holdout tranche, then accept or reject against the pre-registered criteria only. Record baseline, the single delta, criteria, trials charged, holdout consumed, and verdict.
- Cumulative trial accounting via a persistent TrialLedger. The deflated Sharpe for any promotion uses the running total of every hypothesis tried against that family, so the loop cannot launder selection bias by spreading trials across many runs.
- Holdout budget. Reserve the most recent data as a vault released in tranches. Each tranche may be evaluated against a small fixed number of times, then it is burned. A budget meter is visible in the UI. Out of budget means no promotions.
- Paper-forward confirmation. Nothing that shapes trades is promoted on backtest evidence alone. It must beat its baseline on genuinely new incoming paper data for a minimum period and trade count first. Backtests propose, forward paper disposes, mirroring agents propose and the gate disposes.
- Regression guard. A promotion requires improvement on the target metric AND no material degradation on the others (other regimes, drawdown, profit factor). Multi-objective, not single-metric.
- Asymmetric automation. Auto-demote is liberal and fast (it reduces reliance, which is safe). Auto-promote is strict and rare. A skill whose live performance drifts below its baseline over a rolling window is demoted automatically and audited.
- Everything reversible and auditable. Every skill carries a version, provenance (the reflection and experiment that produced it), and a one-click rollback (rollback equals demote, a safe reducing action).

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
- UI: a React and TypeScript operator console (web/, built with Vite and Tailwind) talking to a FastAPI service (agentic_trading_bot/api/). A Streamlit dashboard (agentic_trading_bot/ui/) remains as a lightweight operator view. Dark mode, with a visible kill switch.
- Infra: APScheduler for scheduling, pydantic for config and message schemas, python-dotenv for secrets, loguru or structlog for logging, plotly for charts.
- Data fallback: yfinance or polygon as a backup to IBKR historical data, behind a common data interface so the source is swappable.
- Learning storage: a separate SQLite db (journal/learning.db) for skills, reflections, hypotheses, experiments, the trial ledger, and the holdout budget. The append-only audit db stays as is. Skill and reflection retrieval is a structured query first (filter by type, regime, recency, rank by live performance). Embeddings or a vector index are an optional later enhancement, not a launch dependency.
- Tests: pytest.

## Coding standards
- No em dashes anywhere.
- Output complete files, not diffs or patches, unless the change is a single localized edit.
- Type hints and docstrings on every public function and class.
- Every data shape that crosses a module boundary lives as a pydantic model in ONE shared contracts module (core/contracts.py). Import from there. Never redefine a shared model inside another module. This single rule is what lets stages be built in parallel without merge conflicts.
- Deterministic seeds for any randomness, set centrally.
- Secrets only in .env, which is gitignored. Never hardcode keys.
- Pure functions where possible. Side effects (orders, IO, network) isolated in clearly named modules.
- Each module ships with its own pytest tests.

## Project layout
agentic_trading_bot/         the Python backend (harness)
  main.py                  orchestrator and scheduler
  config.py                typed config loaded from .env
  pyproject.toml
  core/contracts.py        ALL cross-module pydantic models (the parallel-safe interface)
  .env.example
  requirements.txt
  agents/                  research, signal, validation, learning, meta-reviewer agents (Claude Agent SDK)
  models/regime_detector.py
  strategies/              strategy interface and reference strategies
  risk/guardrails.py       the independent final gate
  broker/ibkr_client.py    ib_async wrapper, paper and live toggle
  data/                    data interface, IBKR and fallback sources, caching
  backtest/validator.py    backtest engine and the validation gate
  discovery/               research_pipeline.py (agent wiring) and approval_queue.py (decoupled approval)
  learning/                skills registry, trial ledger, holdout budget, experiment store, budget meter
  api/                     FastAPI service for the web console (thin layer over the modules)
    server.py              create_app factory: localhost bind, shared-token gate, lifespan poller
    auth.py                shared-token check
    state.py               shared handles, read producers, and snapshot builders
    events.py              in-process pub/sub bus feeding the websocket
    schemas.py             request bodies for the action endpoints
    routes_read.py         read (GET) endpoints
    routes_actions.py      gated action (POST) endpoints, each routed through an existing gated path
    ws.py                  websocket channel and background poller
  ui/                      Streamlit operator dashboard (dashboard.py) and its pure logic (dashboard_helpers.py)
  utils/logging.py
  utils/audit.py           append-only audit trail
  journal/                 logs, approved strategies, audit db, learning db
  testsupport/             test fakes (fake ib_async IB) shared across tests
  tests/

web/                         the operator console (Vite, React, TypeScript, Tailwind)
  index.html, vite.config.ts, package.json, tsconfig.json, .env.example
  src/                     app shell, pages, components, hooks, lib, styles
  design/command_console_mockup.html   the Command-page mockup, visual source of truth for the console

## Build order and tracks
Stage 1 is the foundation and runs first and alone. After it, Track A and Track B are independent and may be built in parallel in separate worktrees. They converge at the UI.
1 scaffolding, config, data interface, and core/contracts.py        (foundation, solo)
Track A (execution and safety):
2 broker client                                                     (depends on 1)
3 risk guardrails, with tests, before anything can trade            (depends on 1; parallel with 2)
M1 wire the real risk gate into the broker                          (depends on 2 and 3)
Track B (research and validation):
4 regime detector                                                   (depends on 1)
5 backtest engine and the validation gate                           (depends on 1 and 4)
6 strategy interface and two reference strategies                   (depends on 5)
7 agentic discovery, Claude Agent SDK, decoupled approval           (depends on 5 and 6)
7.5 self-learning loop: reflection, skills, experiments, meta-review (depends on 5, 6, 7)
Convergence:
8 operator console: FastAPI service (api/) and the web/ React app, plus the Streamlit dashboard (ui/)   (depends on 2, 3, 4, 5, 7, 7.5, M1)
9 orchestrator, main loop, scheduler, learning loop, end to end     (depends on everything)
A stage may assume only the modules listed in its dependencies exist. Code against core/contracts.py for anything from a stage you do not depend on.

## When unsure
Ask the operator a specific question rather than guessing on anything touching money, risk, or order routing. Prefer a safe default (smaller size, paper, veto) over an unsafe one.