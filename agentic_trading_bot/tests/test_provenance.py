"""Trade-attribution tests: provenance ledger + close detection (Parts A & B).

FakeIB is driven through the REAL IBKRClient, so recent_fills() and positions()
are exercised for real; only the socket is faked. These tests assert the
attribution invariants from CLAUDE.md: entries accumulate with a weighted-average
price and summed cost, the entry regime is captured as a snapshot, closes emit
exactly one trace for the closed portion, nothing is fabricated when a close
cannot be attributed, and replays never double-count.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from backtest.validator import ValidationGate
from broker.ibkr_client import IBKRClient
from config import Settings
from core.contracts import OrderSide, Proposal, Regime, RegimeState, StrategyProposal
from discovery.approval_queue import ApprovalQueue
from learning.provenance import ProvenanceLedger, STATUS_CLOSED, STATUS_REDUCED
from main import Orchestrator
from models.regime_detector import RegimeDetector
from risk.guardrails import RiskGate
from testsupport.fakes import FakeIB
from utils.audit import AuditTrail


# --------------------------------------------------------------- fixtures/helpers


class FakeDataSource:
    def __init__(self, frames):
        self._frames = frames

    def get_historical_bars(self, symbol, start, end, bar_size="1 day"):
        return self._frames[symbol]


def trending_bars(n: int = 500, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    mu = np.concatenate([np.full(40, rng.choice([1, -1]) * 0.0015) for _ in range(n // 40 + 1)])[:n]
    close = 100.0 * np.exp(np.cumsum(mu + rng.normal(0, 0.004, n)))
    idx = pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close,
         "volume": rng.uniform(5e6, 2e7, n)},
        index=idx,
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, journal_dir=str(tmp_path / "journal"),
        data_cache_dir=str(tmp_path / "cache"), kill_switch_file=str(tmp_path / "KILL"),
        regime_proxy_symbol="AAPL", risk_per_trade_pct=1.0, max_single_name_weight_pct=100.0,
        max_gross_exposure_pct=400.0, max_leverage=10.0, max_correlated_cluster_exposure_pct=100.0,
        min_liquidity_adv=1000, max_adv_participation_pct=50.0,
        max_daily_drawdown_pct=50.0, max_weekly_drawdown_pct=80.0,
    )


def _connected_broker(settings, audit, ib):
    broker = IBKRClient(settings=settings, audit=audit, ib_factory=lambda: ib, auto_reconnect=False)
    broker.connect(base_backoff=0)
    broker._risk_evaluate = RiskGate(settings).evaluate
    return broker


def _orch(tmp_path, settings, audit, ib):
    prov = ProvenanceLedger(tmp_path / "learning.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    orch = Orchestrator(
        settings=settings, broker=_connected_broker(settings, audit, ib), gate=ValidationGate(),
        queue=queue, data_source=FakeDataSource({"AAPL": trending_bars()}),
        detector=RegimeDetector(n_iter=10, window=10, random_seed=42), audit=audit, provenance=prov,
    )
    return orch, prov, queue


def _approve_trend(queue, audit):
    spec = StrategyProposal(name="approved-trend", hypothesis="trend persists",
                            template="trend_breakout", parameters={}, universe=["AAPL"],
                            intended_stop="ATR(14) x 3.0")
    pid = queue.enqueue(Proposal(spec=spec, passed=True))
    queue.approve(pid, "alice", audit, note="paper only")
    return pid


def _exec_fill(symbol, side_str, qty, price, exec_id, commission=0.0, order_id=1):
    """A raw ib_async-style fill the real IBKRClient.recent_fills() can parse."""
    return SimpleNamespace(
        execution=SimpleNamespace(side=side_str, shares=qty, price=price,
                                  time="2026-01-02T15:00:00", execId=exec_id, orderId=order_id),
        contract=SimpleNamespace(symbol=symbol),
        commissionReport=SimpleNamespace(commission=commission),
    )


def _pos(symbol, qty, avg_cost=50.0):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty,
                           avgCost=avg_cost, account="DU1")


def _bull_regime() -> RegimeState:
    return RegimeState(ts_utc="2026-01-01T00:00:00+00:00", regime=Regime.BULL,
                       state_index=3, probabilities={"Bull": 0.8, "Neutral": 0.2})


def _open_long_lot(orch, ib, prov, symbol="AAPL", qty=100.0, price=50.0, comm=1.0):
    """Open a filled long lot directly, then attribute its entry fill via the broker."""
    pid = prov.open_position(
        symbol=symbol, entry_side=OrderSide.BUY, intended_qty=qty,
        originating_strategy_id="trend_breakout", originating_proposal_id="P1",
        entry_order_ids=[1001], entry_regime=_bull_regime(), opened_at="2026-01-01T00:00:01+00:00",
    )
    ib._fills = [_exec_fill(symbol, "BOT", qty, price, "entry-1", commission=comm)]
    ib._positions = [_pos(symbol, qty, price)]
    orch._attribute_and_detect()
    return pid


def _events(audit, kind):
    return [e for e in audit.read_all() if e.event_type == kind]


# ------------------------------------------------------------------ PART A: entry


def test_entry_full_path_writes_one_row_with_origin_and_captured_regime(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, queue = _orch(tmp_path, settings, audit, ib)
    pid = _approve_trend(queue, audit)

    orch.run_trading_cycle()  # submits the bracket and opens a provenance row

    rows = prov.open_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "AAPL"
    assert row.originating_proposal_id == pid
    assert row.originating_strategy_id == "trend_breakout"
    # The entry regime was captured as a snapshot, not reconstructed later.
    assert row.regime_state() is not None
    assert row.entry_order_ids  # the parent (entry) order id was stored


def test_partial_entry_fills_accumulate_weighted_average_and_summed_cost(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, queue = _orch(tmp_path, settings, audit, ib)
    pid = prov.open_position(
        symbol="AAPL", entry_side=OrderSide.BUY, originating_proposal_id="P1",
        entry_regime=_bull_regime(), opened_at="2026-01-01T00:00:01+00:00",
    )
    # Two partial entry fills: 60 @ 50 and 40 @ 55, commissions 0.5 + 0.5.
    ib._fills = [
        _exec_fill("AAPL", "BOT", 60, 50.0, "e1", commission=0.5),
        _exec_fill("AAPL", "BOT", 40, 55.0, "e2", commission=0.5),
    ]
    ib._positions = [_pos("AAPL", 100, 52.0)]
    orch._attribute_and_detect()

    row = prov.get_row(pid)
    assert row.filled_qty == 100
    assert abs(row.avg_entry_price - (60 * 50.0 + 40 * 55.0) / 100) < 1e-9  # 52.0
    assert abs(row.entry_cost - 1.0) < 1e-9


# ------------------------------------------------------------------ PART B: close


def test_open_then_full_close_yields_one_correct_trace(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    pid = _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)

    # Sell 100 @ 55 closes the lot; the position goes flat.
    ib._fills = ib._fills + [_exec_fill("AAPL", "SLD", 100, 55.0, "exit-1", commission=1.0)]
    ib._positions = []
    traces = orch._attribute_and_detect()

    assert len(traces) == 1
    trace = traces[0]
    assert abs(trace.gross_pnl - 500.0) < 1e-9          # (55 - 50) * 100
    assert abs(trace.net_pnl - 498.0) < 1e-9            # minus 1 entry + 1 exit
    assert abs(trace.cost_breakdown["total"] - 2.0) < 1e-9
    assert trace.regime_at_entry == "Bull"
    assert trace.originating_proposal_id == "P1"
    assert prov.get_row(pid).status == STATUS_CLOSED
    assert _events(audit, "TRACE_RECORDED")


def test_open_then_partial_close_emits_partial_trace_and_keeps_remainder_open(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    pid = _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)

    # Sell 60 @ 55; 40 remain open.
    ib._fills = ib._fills + [_exec_fill("AAPL", "SLD", 60, 55.0, "exit-1", commission=1.0)]
    ib._positions = [_pos("AAPL", 40, 50.0)]
    traces = orch._attribute_and_detect()

    assert len(traces) == 1
    trace = traces[0]
    assert abs(trace.closed_qty - 60) < 1e-9
    assert abs(trace.gross_pnl - 300.0) < 1e-9          # (55 - 50) * 60
    # entry cost portion 1*(60/100)=0.6, exit cost 1.0 -> net 300 - 1.6
    assert abs(trace.net_pnl - 298.4) < 1e-9
    row = prov.get_row(pid)
    assert row.status == STATUS_REDUCED
    assert abs(row.filled_qty - 40) < 1e-9


def test_second_cycle_does_not_re_emit_a_closed_trace(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)
    ib._fills = ib._fills + [_exec_fill("AAPL", "SLD", 100, 55.0, "exit-1", commission=1.0)]
    ib._positions = []
    assert len(orch._attribute_and_detect()) == 1

    # A second cycle with the same fills and flat book emits nothing more.
    assert orch._attribute_and_detect() == []
    assert len(prov.all_traces()) == 1


def test_unattributable_close_logs_event_and_emits_nothing(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)

    # Position drops to flat with NO exit fill recorded (a gap / restart).
    ib._positions = []
    traces = orch._attribute_and_detect()

    assert traces == []
    assert prov.all_traces() == []
    assert _events(audit, "TRACE_UNATTRIBUTED")


def test_disconnected_broker_yields_nothing(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)

    orch.broker.disconnect()
    ib._positions = []  # would look like a close if we read it
    assert orch._attribute_and_detect() == []
    assert prov.all_traces() == []


def test_replaying_a_fill_never_double_counts(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, prov, _ = _orch(tmp_path, settings, audit, ib)
    pid = _open_long_lot(orch, ib, prov, qty=100, price=50.0, comm=1.0)

    # The broker keeps returning the same entry fill every cycle.
    orch._attribute_and_detect()
    orch._attribute_and_detect()

    row = prov.get_row(pid)
    assert row.filled_qty == 100      # not 300
    assert abs(row.entry_cost - 1.0) < 1e-9
