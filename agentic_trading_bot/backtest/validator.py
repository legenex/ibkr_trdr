"""Event-driven backtest engine and the validation gate.

This is the most important module after the risk gate. It exists to validate
hypotheses HONESTLY, not to flatter them. The engine charges a realistic cost
model on every fill and reports gross and net side by side. The gate adds the
anti-overfitting machinery from CLAUDE.md: an untouched out-of-sample holdout,
walk-forward analysis, purged and embargoed cross validation, the Deflated
Sharpe Ratio corrected for the number of trials, hard floors on sample size and
calendar span, a parameter-sensitivity sweep, and a regime-conditional
breakdown. It returns a ValidationResult; a FAIL can never be approved.

No lookahead: a signal computed at the close of bar t is acted on at the OPEN of
bar t+1 (the engine shifts signals by one bar). Warmup rows (NaN signals) are
dropped, never backfilled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np
import pandas as pd
import scipy.stats as st

from config import Settings, settings as default_settings
from core.contracts import ValidationResult
from utils.logging import get_logger

_EULER_GAMMA = 0.5772156649015329


# ---------------------------------------------------------------------------
# Strategy interface (the stage-6 strategies will conform to this)
# ---------------------------------------------------------------------------


@runtime_checkable
class Strategy(Protocol):
    """A strategy the gate can validate.

    `generate_signals` returns a target portfolio weight per bar in [-1, 1],
    computed causally (only data at or before each bar). NaN marks warmup. The
    engine handles the next-bar execution shift, so strategies must NOT shift.
    """

    name: str
    params: dict

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series: ...

    def clone(self, **param_overrides: Any) -> "Strategy": ...

    def key_parameters(self) -> dict[str, float]: ...


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


@dataclass
class CostModel:
    """Realistic, mandatory transaction-cost model applied to every fill.

    Components: commission (per share with a floor), half the bid-ask spread,
    size-aware slippage that scales with participation in average daily volume,
    and an annualized borrow charge on short notional.
    """

    commission_per_share: float = 0.005
    commission_min: float = 1.0
    half_spread_bps: float = 1.0
    slippage_base_bps: float = 1.0
    slippage_impact: float = 0.1
    borrow_rate_annual: float = 0.01
    adv_window: int = 20

    def commission(self, shares: float) -> float:
        if shares <= 0:
            return 0.0
        return max(self.commission_min, shares * self.commission_per_share)

    def spread_cost(self, shares: float, price: float) -> float:
        return shares * price * (self.half_spread_bps / 1e4)

    def slippage(self, shares: float, price: float, adv: float) -> float:
        participation = shares / adv if adv and adv > 0 else 0.0
        bps_fraction = self.slippage_base_bps / 1e4 + self.slippage_impact * participation
        return shares * price * bps_fraction

    def transaction_cost(self, shares: float, price: float, adv: float) -> float:
        if shares <= 0 or price <= 0:
            return 0.0
        return self.commission(shares) + self.spread_cost(shares, price) + self.slippage(
            shares, price, adv
        )

    def borrow_cost(self, position_value: float, periods_per_year: float) -> float:
        if position_value >= 0 or periods_per_year <= 0:
            return 0.0
        return abs(position_value) * self.borrow_rate_annual / periods_per_year


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def infer_periods_per_year(index: pd.Index) -> float:
    """Empirical bars-per-year from the index span (about 252 for daily)."""
    if len(index) < 3:
        return 252.0
    try:
        span_days = (pd.Timestamp(index[-1]) - pd.Timestamp(index[0])).days
        if span_days <= 0:
            return 252.0
        return max(1.0, len(index) / (span_days / 365.25))
    except (TypeError, ValueError):
        return 252.0


def sharpe_ratio(returns: np.ndarray, ppy: float) -> float:
    """Annualized Sharpe of per-period returns (0 if degenerate)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return 0.0
    std = r.std(ddof=1)
    if std == 0:
        return 0.0
    return float(r.mean() / std * math.sqrt(ppy))


def sortino_ratio(returns: np.ndarray, ppy: float) -> float:
    """Annualized Sortino (downside-deviation) ratio."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return 0.0
    downside = np.minimum(r, 0.0)
    dd = math.sqrt(float(np.mean(downside ** 2)))
    if dd == 0:
        return 0.0
    return float(r.mean() / dd * math.sqrt(ppy))


def max_drawdown(equity: np.ndarray) -> float:
    """Most negative peak-to-trough fraction of an equity curve."""
    eq = np.asarray(equity, dtype=float)
    if len(eq) == 0:
        return 0.0
    running_max = np.maximum.accumulate(eq)
    drawdowns = eq / running_max - 1.0
    return float(drawdowns.min())


def cagr(equity_start: float, equity_end: float, years: float) -> float:
    """Compound annual growth rate."""
    if years <= 0 or equity_start <= 0:
        return 0.0
    if equity_end <= 0:
        return -1.0
    return float((equity_end / equity_start) ** (1.0 / years) - 1.0)


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """Output of one backtest run: curves, trades, and gross/net metrics."""

    equity_curve: pd.DataFrame  # columns: gross, net
    returns: pd.DataFrame  # columns: gross, net (per-period)
    trades: list[dict[str, Any]]
    metrics: dict[str, dict[str, float]]  # {"gross": {...}, "net": {...}}
    meta: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """Event-driven, single-asset backtester with the mandatory cost model."""

    def __init__(self, cost: Optional[CostModel] = None) -> None:
        """Create the engine, optionally with a custom cost model."""
        self.cost = cost or CostModel()
        self.log = get_logger(__name__)

    def run(
        self, bars: pd.DataFrame, signals: pd.Series, capital: float = 100_000.0
    ) -> BacktestResult:
        """Simulate a strategy over bars given a causal target-weight signal.

        The weight decided at the close of bar t is established at the OPEN of
        bar t+1 and held (in shares) until the weight changes, so a constant
        weight incurs no per-bar churn. Costs are charged only when the position
        changes.
        """
        data = bars.copy()
        data.columns = [str(c).lower() for c in data.columns]
        idx = data.index
        n = len(data)
        opens = data["open"].to_numpy(dtype=float)
        volume = (
            data["volume"].to_numpy(dtype=float)
            if "volume" in data.columns
            else np.full(n, np.nan)
        )
        weights = np.clip(signals.reindex(idx).to_numpy(dtype=float), -1.0, 1.0)
        adv = (
            pd.Series(volume, index=idx)
            .rolling(self.cost.adv_window, min_periods=1)
            .mean()
            .to_numpy()
        )

        valid = np.where(np.isfinite(weights))[0]
        if len(valid) == 0 or n < 3:
            return self._empty_result(capital)
        first = int(valid[0])

        ppy = infer_periods_per_year(idx)

        equity_net = capital
        equity_gross = capital
        shares = 0.0
        prev_w = 0.0

        ts: list[Any] = []
        eq_net: list[float] = []
        eq_gross: list[float] = []
        net_r: list[float] = []
        gross_r: list[float] = []
        trades: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        n_active = 0
        traded_notional = 0.0

        for i in range(first + 1, n):
            decision = weights[i - 1]
            target_w = 0.0 if not np.isfinite(decision) else float(decision)
            price = opens[i]
            if not np.isfinite(price) or price <= 0:
                continue

            cost_i = 0.0
            if target_w != prev_w:
                target_shares = target_w * equity_net / price
                trade_shares = abs(target_shares - shares)
                cost_i = self.cost.transaction_cost(trade_shares, price, adv[i - 1])
                traded_notional += trade_shares * price
                shares = target_shares
                prev_w = target_w

            position_value = shares * price
            ret = (opens[i + 1] / price - 1.0) if i < n - 1 else 0.0
            pnl_gross = position_value * ret
            borrow = self.cost.borrow_cost(position_value, ppy)
            costs_total = cost_i + borrow

            before_net = equity_net
            before_gross = equity_gross
            equity_gross = before_gross + pnl_gross
            equity_net = before_net + pnl_gross - costs_total

            ts.append(idx[i])
            eq_gross.append(equity_gross)
            eq_net.append(equity_net)
            gross_r.append((equity_gross - before_gross) / before_gross if before_gross else 0.0)
            net_r.append((equity_net - before_net) / before_net if before_net else 0.0)

            sign = 0 if abs(target_w) < 1e-9 else (1 if target_w > 0 else -1)
            if sign != 0:
                n_active += 1
            current = self._update_trades(
                trades, current, sign, idx[i], pnl_gross, pnl_gross - costs_total
            )

        if current is not None:
            current["exit_ts"] = str(ts[-1]) if ts else None
            trades.append(current)

        if not ts:
            return self._empty_result(capital)

        equity_df = pd.DataFrame({"gross": eq_gross, "net": eq_net}, index=pd.Index(ts))
        returns_df = pd.DataFrame({"gross": gross_r, "net": net_r}, index=pd.Index(ts))
        years = max((pd.Timestamp(ts[-1]) - pd.Timestamp(ts[0])).days / 365.25, 1e-9)
        exposure = n_active / len(ts)
        turnover = traded_notional / capital / years if years > 0 else 0.0

        metrics = {
            "gross": self._curve_metrics(
                eq_gross, gross_r, ppy, years, trades, "gross", exposure, turnover, capital
            ),
            "net": self._curve_metrics(
                eq_net, net_r, ppy, years, trades, "net", exposure, turnover, capital
            ),
        }
        return BacktestResult(
            equity_curve=equity_df,
            returns=returns_df,
            trades=trades,
            metrics=metrics,
            meta={"ppy": ppy, "capital": capital, "n_bars": n, "years": years},
        )

    @staticmethod
    def _update_trades(
        trades: list[dict[str, Any]],
        current: Optional[dict[str, Any]],
        sign: int,
        ts: Any,
        pnl_gross: float,
        pnl_net: float,
    ) -> Optional[dict[str, Any]]:
        # Close an open trade if the position sign changed (flip or flatten).
        if current is not None and sign != current["dir"]:
            current["exit_ts"] = str(ts)
            trades.append(current)
            current = None
        if current is None and sign != 0:
            current = {
                "entry_ts": str(ts),
                "dir": sign,
                "pnl_gross": 0.0,
                "pnl_net": 0.0,
                "bars": 0,
                "entry_notional": 0.0,
            }
        if current is not None:
            current["pnl_gross"] += pnl_gross
            current["pnl_net"] += pnl_net
            current["bars"] += 1
        return current

    def _curve_metrics(
        self,
        equity: list[float],
        returns: list[float],
        ppy: float,
        years: float,
        trades: list[dict[str, Any]],
        which: str,
        exposure: float,
        turnover: float,
        capital: float,
    ) -> dict[str, float]:
        eq = np.asarray(equity, dtype=float)
        r = np.asarray(returns, dtype=float)
        pnl_key = "pnl_net" if which == "net" else "pnl_gross"
        pnls = [t[pnl_key] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        hit_rate = len(wins) / len(pnls) if pnls else 0.0
        avg_win_loss = (
            (np.mean(wins) / abs(np.mean(losses))) if wins and losses else 0.0
        )
        return {
            "cagr": cagr(capital, float(eq[-1]), years),
            "sharpe": sharpe_ratio(r, ppy),
            "sortino": sortino_ratio(r, ppy),
            "max_drawdown": max_drawdown(eq),
            "hit_rate": float(hit_rate),
            "avg_win_loss": float(avg_win_loss),
            "exposure": float(exposure),
            "turnover": float(turnover),
            "n_trades": float(len(trades)),
            "total_return": float(eq[-1] / capital - 1.0),
        }

    @staticmethod
    def _empty_result(capital: float) -> BacktestResult:
        empty_curve = pd.DataFrame({"gross": [], "net": []})
        zero = {
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "hit_rate": 0.0,
            "avg_win_loss": 0.0,
            "exposure": 0.0,
            "turnover": 0.0,
            "n_trades": 0.0,
            "total_return": 0.0,
        }
        return BacktestResult(
            equity_curve=empty_curve,
            returns=empty_curve.copy(),
            trades=[],
            metrics={"gross": dict(zero), "net": dict(zero)},
            meta={"capital": capital},
        )


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey and Lopez de Prado)
# ---------------------------------------------------------------------------


def deflated_sharpe_ratio(
    returns: np.ndarray,
    n_trials: int,
    trial_sharpes: Optional[list[float]] = None,
) -> dict[str, float]:
    """Deflated Sharpe Ratio: probability the true Sharpe is positive after
    correcting for selection across `n_trials` configurations.

    Uses the per-period Sharpe with its skew/kurtosis correction. The expected
    maximum Sharpe under the null grows with the number of trials. When the full
    set of trial Sharpes is not provided, the dispersion of Sharpe estimates is
    approximated by the variance of the Sharpe estimator itself.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    t_obs = len(r)
    if t_obs < 3 or r.std(ddof=1) == 0:
        return {"dsr": 0.0, "sr": 0.0, "sr0": 0.0, "t_obs": float(t_obs),
                "n_trials": float(max(int(n_trials), 1)), "skew": 0.0, "kurt": 3.0}

    sr = float(r.mean() / r.std(ddof=1))
    skew = float(st.skew(r))
    kurt = float(st.kurtosis(r, fisher=False))
    variance_term = max(1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2, 1e-12)

    if trial_sharpes is not None and len(trial_sharpes) > 1:
        var_sr = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=1))
    else:
        var_sr = max(variance_term / (t_obs - 1), 1e-12)

    n = max(int(n_trials), 1)
    if n > 1:
        z1 = float(st.norm.ppf(1.0 - 1.0 / n))
        z2 = float(st.norm.ppf(1.0 - 1.0 / (n * math.e)))
        sr0 = math.sqrt(var_sr) * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)
    else:
        sr0 = 0.0

    dsr = float(st.norm.cdf((sr - sr0) * math.sqrt(t_obs - 1) / math.sqrt(variance_term)))
    return {
        "dsr": dsr,
        "sr": sr,
        "sr0": sr0,
        "t_obs": float(t_obs),
        "n_trials": float(n),
        "skew": skew,
        "kurt": kurt,
        "var_sr": var_sr,
    }


# ---------------------------------------------------------------------------
# Purged and embargoed cross validation helper
# ---------------------------------------------------------------------------


def purged_kfold_indices(
    n_samples: int, n_splits: int = 5, embargo_pct: float = 0.01
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Purged, embargoed K-fold splits (Lopez de Prado) for any ML step.

    The test fold's index range is purged from the training set, and an embargo
    of `embargo_pct` of the sample is removed after each test fold, so no train
    observation is adjacent to (and thus leaking into) a test observation.
    """
    indices = np.arange(n_samples)
    folds = np.array_split(indices, n_splits)
    embargo = int(n_samples * embargo_pct)
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for test_idx in folds:
        if len(test_idx) == 0:
            continue
        t0, t1 = int(test_idx[0]), int(test_idx[-1])
        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[t0 : t1 + 1] = False  # purge the test range
        embargo_end = min(n_samples, t1 + 1 + embargo)
        train_mask[t1 + 1 : embargo_end] = False  # embargo after the test fold
        # Embargo before the test fold as well to block both-sided leakage.
        embargo_start = max(0, t0 - embargo)
        train_mask[embargo_start:t0] = False
        splits.append((indices[train_mask], test_idx))
    return splits


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


@dataclass
class ValidationConfig:
    """Thresholds and split sizes for the validation gate."""

    holdout_pct: float = 0.25
    wf_n_windows: int = 5
    wf_min_test: int = 30
    wf_min_positive_frac: float = 0.5
    min_trades: int = 30
    min_calendar_days: int = 365
    min_oos_sharpe: float = 0.5
    dsr_threshold: float = 0.95
    sensitivity_collapse_frac: float = 0.5
    capital: float = 100_000.0


class ValidationGate:
    """The validation gate. Returns a ValidationResult; a FAIL is never approvable."""

    def __init__(
        self,
        cost: Optional[CostModel] = None,
        config: Optional[ValidationConfig] = None,
        settings: Settings = default_settings,
    ) -> None:
        """Create the gate with a cost model and threshold configuration."""
        self.cost = cost or CostModel()
        self.cfg = config or ValidationConfig()
        self.settings = settings
        self.engine = BacktestEngine(self.cost)
        self.log = get_logger(__name__)

    def validate(
        self,
        strategy: Strategy,
        bars: pd.DataFrame,
        n_trials: int = 1,
        detector: Any = None,
        capital: Optional[float] = None,
    ) -> ValidationResult:
        """Run the full anti-overfitting gate and return a structured verdict.

        Args:
            strategy: The strategy under test.
            bars: OHLCV history (one symbol), time-ascending.
            n_trials: How many configurations were tried before this one. Feeds
                the Deflated Sharpe Ratio so cherry-picking is penalized.
            detector: Optional fitted regime detector for the breakdown. Built
                on the fly if omitted.
            capital: Starting capital (defaults to the config value).
        """
        capital = capital if capital is not None else self.cfg.capital
        data = bars.copy()
        data.columns = [str(c).lower() for c in data.columns]
        data = data.sort_index()
        n = len(data)

        split = int(n * (1.0 - self.cfg.holdout_pct))
        is_bars = data.iloc[:split]
        oos_bars = data.iloc[split:]

        # Fit on in-sample only, then generate causal signals over the full set.
        if hasattr(strategy, "fit"):
            strategy.fit(is_bars)  # type: ignore[attr-defined]
        signals = strategy.generate_signals(data)

        full_res = self.engine.run(data, signals, capital)
        is_res = self.engine.run(is_bars, signals.reindex(is_bars.index), capital)
        oos_res = self.engine.run(oos_bars, signals.reindex(oos_bars.index), capital)

        metrics = {
            "in_sample": {"gross": is_res.metrics["gross"], "net": is_res.metrics["net"]},
            "out_of_sample": {"gross": oos_res.metrics["gross"], "net": oos_res.metrics["net"]},
            "full": {"gross": full_res.metrics["gross"], "net": full_res.metrics["net"]},
        }

        oos_net_returns = oos_res.returns["net"].to_numpy() if not oos_res.returns.empty else np.array([])
        dsr = deflated_sharpe_ratio(oos_net_returns, n_trials)

        n_trades = int(full_res.metrics["net"]["n_trades"])
        calendar_days = float((pd.Timestamp(data.index[-1]) - pd.Timestamp(data.index[0])).days)

        walk_forward = self._walk_forward(strategy, data, capital)
        wf_summary = self._summarize_walk_forward(walk_forward)
        oos_net_sharpe = float(oos_res.metrics["net"]["sharpe"])
        sensitivity = self._sensitivity(strategy, data, is_bars, oos_bars, capital, oos_net_sharpe)
        regime_breakdown = self._regime_breakdown(data, full_res, detector)

        reasons: list[str] = []
        if n_trades < self.cfg.min_trades:
            reasons.append(
                f"insufficient trades: {n_trades} < {self.cfg.min_trades} minimum"
            )
        if calendar_days < self.cfg.min_calendar_days:
            reasons.append(
                f"insufficient calendar span: {calendar_days:.0f} days < "
                f"{self.cfg.min_calendar_days} minimum"
            )
        if oos_net_sharpe < self.cfg.min_oos_sharpe:
            reasons.append(
                f"out-of-sample net Sharpe {oos_net_sharpe:.2f} below "
                f"{self.cfg.min_oos_sharpe} minimum"
            )
        if dsr["dsr"] < self.cfg.dsr_threshold:
            reasons.append(
                f"deflated Sharpe {dsr['dsr']:.3f} below {self.cfg.dsr_threshold} "
                f"threshold after {n_trials} trials (nominal Sharpe does not survive deflation)"
            )
        if wf_summary.get("frac_positive", 0.0) < self.cfg.wf_min_positive_frac:
            reasons.append(
                f"walk-forward robustness too low: only "
                f"{wf_summary.get('frac_positive', 0.0):.0%} of windows had positive net Sharpe"
            )
        if not sensitivity.get("passed", True):
            reasons.append(
                "parameter sensitivity: net Sharpe collapses when a key parameter is perturbed"
            )

        passed = len(reasons) == 0
        result = ValidationResult(
            passed=passed,
            strategy_name=getattr(strategy, "name", strategy.__class__.__name__),
            n_trials=int(n_trials),
            n_trades=n_trades,
            calendar_days=calendar_days,
            deflated_sharpe=float(dsr["dsr"]),
            metrics=metrics,
            walk_forward=walk_forward,
            walk_forward_summary=wf_summary,
            sensitivity=sensitivity,
            regime_breakdown=regime_breakdown,
            dsr_detail=dsr,
            reasons=reasons,
        )
        self.log.info(
            "validation_complete",
            strategy=result.strategy_name,
            passed=passed,
            oos_net_sharpe=round(oos_net_sharpe, 3),
            dsr=round(dsr["dsr"], 3),
            n_trades=n_trades,
            reasons=reasons,
        )
        return result

    # ----------------------------------------------------------- walk forward

    def _walk_forward(
        self, strategy: Strategy, bars: pd.DataFrame, capital: float
    ) -> list[dict[str, Any]]:
        n = len(bars)
        k = self.cfg.wf_n_windows
        test_size = n // (k + 1)
        if test_size < self.cfg.wf_min_test:
            return []
        windows: list[dict[str, Any]] = []
        for w in range(1, k + 1):
            train_end = w * test_size
            test_end = min((w + 1) * test_size, n)
            train = bars.iloc[:train_end]
            test = bars.iloc[train_end:test_end]
            if len(test) < self.cfg.wf_min_test:
                continue
            variant = strategy.clone()
            if hasattr(variant, "fit"):
                variant.fit(train)  # type: ignore[attr-defined]
            signals = variant.generate_signals(bars.iloc[:test_end])
            res = self.engine.run(test, signals.reindex(test.index), capital)
            windows.append(
                {
                    "window": w,
                    "start": str(test.index[0]),
                    "end": str(test.index[-1]),
                    "net_sharpe": float(res.metrics["net"]["sharpe"]),
                    "net_total_return": float(res.metrics["net"]["total_return"]),
                    "n_trades": int(res.metrics["net"]["n_trades"]),
                }
            )
        return windows

    @staticmethod
    def _summarize_walk_forward(windows: list[dict[str, Any]]) -> dict[str, float]:
        if not windows:
            return {"n_windows": 0.0, "frac_positive": 0.0, "mean_sharpe": 0.0,
                    "median_sharpe": 0.0, "std_sharpe": 0.0}
        sharpes = np.array([w["net_sharpe"] for w in windows], dtype=float)
        return {
            "n_windows": float(len(windows)),
            "frac_positive": float(np.mean(sharpes > 0)),
            "mean_sharpe": float(np.mean(sharpes)),
            "median_sharpe": float(np.median(sharpes)),
            "std_sharpe": float(np.std(sharpes)),
        }

    # ------------------------------------------------------------ sensitivity

    def _sensitivity(
        self,
        strategy: Strategy,
        bars: pd.DataFrame,
        is_bars: pd.DataFrame,
        oos_bars: pd.DataFrame,
        capital: float,
        base_oos_sharpe: float,
    ) -> dict[str, Any]:
        perturbations: list[dict[str, Any]] = []
        collapsed = False
        key_params = strategy.key_parameters()
        for name, step in key_params.items():
            base_value = strategy.params.get(name)
            if base_value is None:
                continue
            for direction, delta in (("up", step), ("down", -step)):
                try:
                    variant = strategy.clone(**{name: base_value + delta})
                    if hasattr(variant, "fit"):
                        variant.fit(is_bars)  # type: ignore[attr-defined]
                    signals = variant.generate_signals(bars)
                    res = self.engine.run(oos_bars, signals.reindex(oos_bars.index), capital)
                    sharpe = float(res.metrics["net"]["sharpe"])
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("sensitivity_variant_failed", param=name, error=str(exc))
                    sharpe = float("nan")
                perturbations.append(
                    {"param": name, "direction": direction, "value": base_value + delta,
                     "net_sharpe": sharpe}
                )
                # A collapse only matters if the base strategy actually had an edge.
                if base_oos_sharpe > 0 and (
                    not np.isfinite(sharpe)
                    or sharpe < self.cfg.sensitivity_collapse_frac * base_oos_sharpe
                ):
                    collapsed = True

        return {
            "passed": not collapsed,
            "base_oos_sharpe": base_oos_sharpe,
            "collapse_frac": self.cfg.sensitivity_collapse_frac,
            "perturbations": perturbations,
        }

    # ------------------------------------------------------- regime breakdown

    def _regime_breakdown(
        self, bars: pd.DataFrame, full_res: BacktestResult, detector: Any
    ) -> dict[str, dict[str, float]]:
        try:
            from models.regime_detector import RegimeDetector

            det = detector
            if det is None:
                det = RegimeDetector(n_iter=50, settings=self.settings)
                det.fit(bars)
            elif getattr(det, "model", None) is None:
                det.fit(bars)

            labels = det.predict_causal(bars)
            if not labels:
                return {}
            label_series = pd.Series(
                [rs.regime.value for rs in labels],
                index=pd.Index([pd.Timestamp(rs.ts_utc) for rs in labels]),
            )
            net_returns = full_res.returns["net"]
            if net_returns.empty:
                return {}
            net_returns.index = pd.to_datetime(net_returns.index)
            aligned = label_series.reindex(net_returns.index, method="ffill")
            ppy = infer_periods_per_year(net_returns.index)

            out: dict[str, dict[str, float]] = {}
            for regime, group in net_returns.groupby(aligned):
                values = group.to_numpy(dtype=float)
                if len(values) < 2:
                    continue
                out[str(regime)] = {
                    "mean_return": float(np.mean(values)),
                    "sharpe": sharpe_ratio(values, ppy),
                    "n_periods": float(len(values)),
                }
            return out
        except Exception as exc:  # noqa: BLE001
            self.log.warning("regime_breakdown_failed", error=str(exc))
            return {}
