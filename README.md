# Agentic Trading Research and Execution Harness

A research, validation, and execution harness for trading through Interactive
Brokers (IBKR). It does **not** contain or generate a trading edge. The operator
supplies hypotheses; the system validates them honestly and executes approved
ones inside hard risk limits. Paper trading is the default. Read
[CLAUDE.md](CLAUDE.md) for the full design and the non-negotiable invariants.

All code lives under `agentic_trading_bot/`. Run commands from there.

```
cd agentic_trading_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q          # 159 tests should pass
```

---

## End-to-end paper-trading runbook

This walks from a cold start to watching a paper bracket order get placed.
**Nothing here touches live money.** Live trading is gated separately (see the
checklist at the bottom) and is never the default.

### 1. Start paper TWS or IB Gateway and enable the API port

1. Install and launch **Trader Workstation (TWS)** or **IB Gateway** and log in
   to a **paper** account (the app shows "Paper Trading" in the title bar).
2. Enable the API:
   - TWS: `File > Global Configuration > API > Settings`.
   - Check **Enable ActiveX and Socket Clients**.
   - Confirm the **Socket port**: paper TWS `7497`, paper IB Gateway `4002`
     (live ports `7496` / `4001` are intentionally not used by default).
   - Add `127.0.0.1` to **Trusted IPs**.
   - Leave **Read-Only API** checked for read-only browsing; uncheck it only when
     you actually want to place test orders.
   - Apply and OK. Keep TWS/Gateway running.

### 2. Environment setup

```
cd agentic_trading_bot
cp .env.example .env
```

Edit `.env`:
- Keep `LIVE_TRADING=false` (the default; do not change it yet).
- Set `IBKR_PAPER_PORT` to match your app (`7497` TWS, or `4002` with
  `USE_IB_GATEWAY=true`).
- Optionally set `ANTHROPIC_API_KEY` to use the live Claude Agent SDK for
  research; without it the discovery pipeline runs an offline stub and the
  **validation gate still runs for real**.
- Review the risk parameters. The defaults are conservative
  (`RISK_PER_TRADE_PCT=0.5`, `MAX_DAILY_DRAWDOWN_PCT=3`, ...). Config fails loudly
  on an out-of-range value.

Secrets live only in `.env`, which is gitignored. Never commit it.

### 3. Launch the dashboard (UI)

```
cd agentic_trading_bot
streamlit run ui/dashboard.py
```

Open `http://localhost:8501`. It opens in dark mode with an always-visible
**KILL SWITCH** at the top and a green **PAPER** indicator in the sidebar.
- Connect the broker on the **Positions & Orders** tab (it connects to the paper
  port). If it cannot reach TWS/Gateway it tells you to enable the API port.
- The **Dashboard** tab shows the current regime and, once connected, the
  portfolio and circuit-breaker status.

### 4. Get a strategy into the approval queue

Either:
- **Research Chat** tab: enter a theme and a small universe, click **Run
  discovery**. The Stage 7 pipeline (research -> signal -> validation) produces
  proposals and queues them. Agents propose only; they cannot trade.
- or run it headless: `python -m discovery.research_pipeline --theme "..." --symbols "AAPL,MSFT"`.

Reference strategies are honest test subjects, not claimed edges, so most
candidates will (correctly) **FAIL** the validation gate.

### 5. Approve a strategy (PAPER only)

On the **Signals & Approvals** tab, open a pending proposal. You see its full
`ValidationResult`: gross and net metrics, deflated Sharpe, walk-forward
distribution, sensitivity, and regime breakdown.
- **Approve is disabled when the proposal FAILED**, with the failing reasons
  shown. A FAIL can never be approved.
- For a PASS, click **Approve (PAPER)**. A warning reminds you this grants
  **paper execution only**. The approval and a risk warning are written to the
  audit trail.

### 6. Run the orchestrator and watch a paper bracket order

Start the scheduler (separate terminal, same venv):

```
cd agentic_trading_bot
python -m main
```

Each cycle (default 60s) the orchestrator:
1. checks the kill switch and circuit breakers first,
2. refreshes the regime,
3. for each **approved** strategy generates a signal, sizes it through the risk
   gate, and submits a **bracket order** (entry, protective stop, target) on the
   paper account,
4. reconciles against IBKR and logs any drift,
5. writes a per-cycle summary to the audit trail.

Watch it land: the order appears in TWS/Gateway and on the **Positions &
Orders** tab; the **Audit** tab shows `CYCLE_SUMMARY`, `RISK_DECISION`, and
`ORDER_SUBMITTED` events. Every entry carries a stop; there is no naked-market
path.

### 7. The kill switch and manual flatten

- Click **ENGAGE KILL SWITCH** at any time: new order submission halts
  immediately (the orchestrator and the risk gate both honor the sentinel file).
  It does **not** liquidate.
- To close a position, use the **Flatten** control on Positions & Orders. It
  routes a closing order through the risk gate. While the kill switch is on, a
  flatten is vetoed by the gate; release the switch to flatten.

### 8. The self-learning loop (optional, paper only)

Set `LEARNING_CADENCE=daily` (or `after_trades`) in `.env` to let the loop
reflect on closed paper trades, run controlled experiments on an unburned
holdout tranche, and either auto-promote a low-blast-radius analysis skill or
**enqueue** a signal-shaping skill for human approval. It never places or
modifies an order, it is paused while the kill switch is on, and its LLM steps
are bounded by `LEARNING_TOKEN_BUDGET` / `LEARNING_COST_BUDGET_USD`. The UI tabs
**Skill Registry**, **Learning History**, **Why Promoted**, and **Holdout
Budget** make all of it auditable.

---

## Before anyone even considers flipping LIVE_TRADING

**Weeks of clean paper operation come first.** There is no shortcut, including
for the operator. Live trading requires BOTH `LIVE_TRADING=true` in `.env` AND a
typed runtime confirmation phrase; absent either, the broker connects to the
paper port only. Do not change `LIVE_TRADING` until **every** box below is green:

- [ ] At least several **weeks** of continuous paper operation with no
      unexplained behavior.
- [ ] Every strategy you intend to run has **passed the validation gate**
      (out-of-sample, walk-forward, realistic costs, deflated Sharpe that
      survives the cumulative-trial correction, minimum trades and span, stable
      parameter sensitivity).
- [ ] Paper results match backtest expectations within reason; **net** (after
      costs), not gross, is what you judged.
- [ ] The **kill switch** has been tested from both the UI and the CLI sentinel
      file and verifiably halts new submission.
- [ ] **Manual flatten** has been exercised on paper and routes through the risk
      gate as expected.
- [ ] The **circuit breakers** (daily and weekly drawdown) have been observed to
      veto new entries when tripped.
- [ ] Every order in the paper run carried a **protective stop**; no naked market
      orders ever appeared in the audit log.
- [ ] **Reconciliation** shows no unexplained drift between local intent and
      IBKR-reported state.
- [ ] The **audit trail** is complete: every order, fill, veto, approval,
      rejection, and agent decision is present with a reason.
- [ ] Risk limits in `.env` are set to amounts you can afford to lose, with
      `RISK_PER_TRADE_PCT`, exposure caps, and drawdown limits reviewed.
- [ ] You understand that approval grants **paper** execution only and that
      promotion to live is a **separate, deliberate, manual** step.
- [ ] You have read [CLAUDE.md](CLAUDE.md) and accept the non-negotiable
      invariants.

Even then: start live with the smallest possible size, keep the kill switch
within reach, and treat the first live weeks as a continuation of testing.
