"""ib_async wrapper for IBKR with a hard paper-default and the final risk gate.

Responsibilities and guarantees:
  - Paper is the default. The LIVE port is selected only when LIVE_TRADING is
    true AND the caller passes a confirmation string that matches the configured
    phrase. A live request missing either condition is refused, logged, and
    falls back to the paper port.
  - Every order method calls the risk guardrails gate first via the stable
    `evaluate` interface. If the gate module is unavailable, the broker raises
    and submits nothing.
  - There is no naked market order path. `place_order` requires an attached
    protective stop; `place_bracket_order` submits a native IBKR bracket.
  - Connect, order, fill, and error events are written to the append-only audit
    trail.

The IB transport is injected through `ib_factory` so tests can run against a
mock without a live connection.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from config import Settings, settings as default_settings
from core.contracts import (
    AccountState,
    AccountSummary,
    Fill,
    Order,
    OrderKind,
    OrderPlacementResult,
    OrderSide,
    OrderType,
    Position,
    ReconciliationReport,
    RiskDecision,
)
from data.cache import DiskCache
from data.ibkr_source import IBKRDataSource
from utils.audit import AuditTrail, get_audit_trail
from utils.logging import RUN_ID, get_logger

# Import the stable risk gate. If this fails, the broker still imports (so reads
# and connection work) but no order can be submitted: each order method checks
# the gate and raises GuardrailsUnavailableError.
try:
    from risk.guardrails import evaluate as _module_risk_evaluate

    _GUARDRAILS_IMPORT_ERROR: Optional[BaseException] = None
except BaseException as exc:  # pragma: no cover - exercised only on a broken gate
    _module_risk_evaluate = None
    _GUARDRAILS_IMPORT_ERROR = exc

# Calendar lookback used to estimate average daily volume and recent returns for
# the risk gate. Wide enough to clear the gate's correlation_min_periods.
_RISK_HISTORY_DAYS: int = 90

_API_PORT_REMINDER = (
    "Could not reach TWS or IB Gateway. Confirm it is running and that the API "
    "port is enabled: in TWS, Configure > API > Settings > Enable ActiveX and "
    "Socket Clients, and add 127.0.0.1 to Trusted IPs. Ports: paper TWS 7497, "
    "live TWS 7496, IB Gateway paper 4002, IB Gateway live 4001."
)


class BrokerError(Exception):
    """Base class for broker errors."""


class NotConnectedError(BrokerError):
    """Raised when an operation needs a connection that is not established."""


class GuardrailsUnavailableError(BrokerError):
    """Raised when the risk gate cannot be used. No order is sent."""


class NakedOrderRejected(BrokerError):
    """Raised when an order would be submitted without a protective stop."""


def _default_ib_factory() -> Any:
    """Construct a real ib_async IB object."""
    from ib_async import IB

    return IB()


class IBKRClient:
    """Thin, auditable wrapper around an ib_async IB session."""

    def __init__(
        self,
        settings: Settings = default_settings,
        audit: Optional[AuditTrail] = None,
        ib_factory: Optional[Callable[[], Any]] = None,
        data_source: Optional[IBKRDataSource] = None,
        auto_reconnect: bool = True,
    ) -> None:
        """Create the client.

        Args:
            settings: Configuration object (defaults to the global settings).
            audit: Audit trail to write to (defaults to the process-wide trail).
            ib_factory: Callable returning an IB-like object. Injected in tests.
            data_source: Optional pre-built IBKRDataSource. Built on connect if
                not supplied.
            auto_reconnect: Reconnect with backoff after an unexpected drop.
        """
        self.settings = settings
        self.log = get_logger(__name__)
        self.audit = audit if audit is not None else get_audit_trail()
        self._ib_factory = ib_factory or _default_ib_factory
        self.ib: Optional[Any] = None
        self._connected = False
        self._auto_reconnect = auto_reconnect
        self._deliberate_disconnect = False

        # The risk gate, stored per-instance so tests can swap or disable it.
        self._risk_evaluate = _module_risk_evaluate

        # Equity baselines for the drawdown circuit breakers. Tracked per UTC day
        # and ISO week as a best-effort in-process snapshot; the orchestrator may
        # override them with persisted session values via set_equity_baselines.
        self._equity_anchors: dict[str, tuple[str, float]] = {}
        self._forced_day_start: Optional[float] = None
        self._forced_week_start: Optional[float] = None

        # Local intended state, for reconciliation against the broker.
        self._intended_positions: dict[str, float] = {}
        self._working_order_ids: list[int] = []

        # Data plumbing.
        self._cache = DiskCache(self.settings.cache_path)
        self._data_source = data_source

        # Remembered connection target so reconnect can reuse it.
        self._last_host: Optional[str] = None
        self._last_port: Optional[int] = None
        self._last_mode: str = "PAPER"
        self._last_readonly: bool = False

        if _module_risk_evaluate is None:
            self.log.error(
                "risk_guardrails_import_failed",
                detail="Risk gate failed to import; no orders can be submitted. "
                f"Import error: {_GUARDRAILS_IMPORT_ERROR}",
            )

    # ----------------------------------------------------------------- connect

    def _resolve_port_and_mode(self, confirmation: Optional[str]) -> tuple[int, str]:
        """Decide which port to connect to, enforcing the live-trading guard.

        Returns a (port, mode) pair. Live is selected only when LIVE_TRADING is
        true AND the confirmation matches. Otherwise paper, and a refused live
        request is logged and audited.
        """
        s = self.settings
        live_requested = s.live_trading
        confirmed = confirmation is not None and confirmation == s.live_confirmation_phrase
        use_live = live_requested and confirmed

        if live_requested and not confirmed:
            reason = (
                "Live trading requested but refused: missing or incorrect typed "
                "confirmation. Falling back to the paper port."
            )
            self.log.error("live_trading_refused", reason=reason)
            self.audit.record(
                "LIVE_TRADING_REFUSED",
                {"live_trading_flag": True, "confirmation_provided": confirmation is not None},
                reason,
            )

        if use_live:
            port = s.ibkr_gateway_live_port if s.use_ib_gateway else s.ibkr_live_port
            return port, "LIVE"
        port = s.ibkr_gateway_paper_port if s.use_ib_gateway else s.ibkr_paper_port
        return port, "PAPER"

    def connect(
        self,
        confirmation: Optional[str] = None,
        *,
        max_retries: int = 5,
        base_backoff: float = 1.0,
        timeout: float = 10.0,
        readonly: bool = False,
    ) -> bool:
        """Connect to TWS or IB Gateway, defaulting to paper.

        Args:
            confirmation: Typed live-trading confirmation. Required (and must
                match settings.live_confirmation_phrase) for a live connection.
            max_retries: Connection attempts before raising.
            base_backoff: Base seconds for exponential backoff between attempts.
            timeout: Per-attempt connect timeout in seconds.
            readonly: Connect in read-only mode (no order submission).

        Returns:
            True on success.

        Raises:
            NotConnectedError: If all attempts fail (TWS or Gateway not running,
                or the API port is not enabled).
        """
        port, mode = self._resolve_port_and_mode(confirmation)
        host = self.settings.ibkr_host
        client_id = self.settings.ibkr_client_id

        self._last_host = host
        self._last_port = port
        self._last_mode = mode
        self._last_readonly = readonly

        self.audit.record(
            "CONNECT_ATTEMPT",
            {"host": host, "port": port, "mode": mode, "client_id": client_id},
            f"Connecting to IBKR in {mode} mode",
        )
        return self._do_connect(host, port, client_id, mode, max_retries, base_backoff, timeout, readonly)

    def _do_connect(
        self,
        host: str,
        port: int,
        client_id: int,
        mode: str,
        max_retries: int,
        base_backoff: float,
        timeout: float,
        readonly: bool,
    ) -> bool:
        last_error: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            ib = self._ib_factory()
            try:
                ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=readonly)
                self.ib = ib
                self._connected = True
                self._deliberate_disconnect = False
                self._wire_session()
                self.log.info("ibkr_connected", host=host, port=port, mode=mode, attempt=attempt)
                self.audit.record(
                    "CONNECTED",
                    {"host": host, "port": port, "mode": mode, "attempt": attempt},
                    f"Connected to IBKR ({mode})",
                )
                return True
            except (ConnectionRefusedError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
                last_error = exc
                self._log_connect_failure(exc, host, port, mode, attempt, max_retries)
            except Exception as exc:  # noqa: BLE001 - last-resort, still audited
                last_error = exc
                self._log_connect_failure(exc, host, port, mode, attempt, max_retries)

            if attempt < max_retries:
                self._sleep(base_backoff * (2 ** (attempt - 1)))

        message = f"{_API_PORT_REMINDER} (mode={mode}, port={port}, last error: {last_error})"
        self.audit.record(
            "CONNECT_FAILED",
            {"host": host, "port": port, "mode": mode, "error": str(last_error)},
            message,
        )
        raise NotConnectedError(message) from last_error

    def _log_connect_failure(
        self, exc: BaseException, host: str, port: int, mode: str, attempt: int, max_retries: int
    ) -> None:
        self.log.error(
            "ibkr_connect_failed",
            host=host,
            port=port,
            mode=mode,
            attempt=attempt,
            max_retries=max_retries,
            error=str(exc),
        )
        self.audit.record(
            "CONNECT_ERROR",
            {"host": host, "port": port, "mode": mode, "attempt": attempt, "error": str(exc)},
            f"{_API_PORT_REMINDER} (attempt {attempt}/{max_retries})",
        )

    def _sleep(self, seconds: float) -> None:
        """Sleep for backoff. Separated so tests can stub it out."""
        if seconds > 0:
            time.sleep(seconds)

    def _wire_session(self) -> None:
        """Attach the data source and event handlers to the live session."""
        if self._data_source is None:
            self._data_source = IBKRDataSource(ib=self.ib, cache=self._cache)
        else:
            self._data_source.ib = self.ib

        for event_name, handler in (
            ("disconnectedEvent", self._on_disconnect),
            ("errorEvent", self._on_error),
            ("execDetailsEvent", self._on_exec_details),
        ):
            event = getattr(self.ib, event_name, None)
            if event is not None:
                try:
                    setattr(self.ib, event_name, event + handler)
                except TypeError:
                    # Some event objects mutate in place via += semantics.
                    try:
                        event += handler  # type: ignore[misc]
                    except Exception:
                        pass

    def disconnect(self) -> None:
        """Deliberately disconnect. Suppresses auto-reconnect."""
        self._deliberate_disconnect = True
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("ibkr_disconnect_error", error=str(exc))
        self._connected = False
        self.audit.record("DISCONNECTED", {}, "Deliberate disconnect")

    def reconnect(self, *, max_retries: int = 5, base_backoff: float = 1.0, timeout: float = 10.0) -> bool:
        """Reconnect to the last target with backoff."""
        if self._last_port is None or self._last_host is None:
            raise NotConnectedError("reconnect called before an initial connect")
        self.audit.record(
            "RECONNECT_ATTEMPT",
            {"host": self._last_host, "port": self._last_port, "mode": self._last_mode},
            "Reconnecting to IBKR",
        )
        return self._do_connect(
            self._last_host,
            self._last_port,
            self.settings.ibkr_client_id,
            self._last_mode,
            max_retries,
            base_backoff,
            timeout,
            self._last_readonly,
        )

    def is_connected(self) -> bool:
        """True if the session is currently connected."""
        if self.ib is None:
            return False
        try:
            return bool(self.ib.isConnected())
        except Exception:
            return self._connected

    def _require_connected(self) -> Any:
        if not self.is_connected():
            raise NotConnectedError("Not connected to IBKR. Call connect() first.")
        return self.ib

    # ------------------------------------------------------------ event hooks

    def _on_disconnect(self, *args: Any) -> None:
        self._connected = False
        self.log.warning("ibkr_disconnected", deliberate=self._deliberate_disconnect)
        self.audit.record(
            "CONNECTION_LOST",
            {"deliberate": self._deliberate_disconnect},
            "IBKR connection lost",
        )
        if self._auto_reconnect and not self._deliberate_disconnect:
            try:
                self.reconnect()
            except Exception as exc:  # noqa: BLE001
                self.log.error("ibkr_auto_reconnect_failed", error=str(exc))

    def _on_error(self, *args: Any) -> None:
        # ib_async errorEvent signature: (reqId, errorCode, errorString, contract)
        req_id = args[0] if len(args) > 0 else None
        code = args[1] if len(args) > 1 else None
        message = args[2] if len(args) > 2 else None
        self.log.error("ibkr_error", req_id=req_id, code=code, message=message)
        self.audit.record(
            "IB_ERROR",
            {"req_id": req_id, "code": code, "message": message},
            str(message),
        )

    def _on_exec_details(self, *args: Any) -> None:
        # ib_async execDetailsEvent signature: (trade, fill)
        fill = args[-1] if args else None
        payload = self._fill_payload(fill)
        self.log.info("ibkr_fill", **payload)
        self.audit.record("FILL", payload, "Execution reported by IBKR")

    @staticmethod
    def _fill_payload(fill: Any) -> dict[str, Any]:
        if fill is None:
            return {}
        execution = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        return {
            "symbol": getattr(contract, "symbol", None),
            "side": getattr(execution, "side", None),
            "shares": getattr(execution, "shares", None),
            "price": getattr(execution, "price", None),
            "exec_id": getattr(execution, "execId", None),
        }

    # ------------------------------------------------------------- risk gate

    def set_equity_baselines(
        self, day_start: Optional[float] = None, week_start: Optional[float] = None
    ) -> None:
        """Override the day/week start equity used by the drawdown breakers.

        The orchestrator calls this at session start with persisted values so the
        drawdown circuit breakers measure from the true period open rather than
        the broker's first in-process observation. Either may be None to fall back
        to the in-process snapshot for that period.
        """
        self._forced_day_start = day_start
        self._forced_week_start = week_start

    def _evaluate_risk(self, order: Order) -> RiskDecision:
        """Run the order through the real risk gate. Raises if unavailable.

        The broker assembles an AccountState from broker-reported equity and
        positions plus data-source liquidity and returns, then hands it to the
        gate. The gate is the only thing that can veto or shrink; the broker only
        builds the inputs and obeys the verdict.
        """
        if self._risk_evaluate is None:
            reason = (
                "Risk guardrails gate is unavailable; refusing to submit any "
                f"order. Import error: {_GUARDRAILS_IMPORT_ERROR}"
            )
            self.log.error("guardrails_unavailable", reason=reason)
            self.audit.record(
                "GUARDRAILS_UNAVAILABLE",
                {"symbol": order.symbol},
                reason,
            )
            raise GuardrailsUnavailableError(reason)

        account_state = self._build_account_state(order)
        decision = self._risk_evaluate(order, account_state)
        self.audit.record(
            "RISK_DECISION",
            {
                "symbol": order.symbol,
                "approved": decision.approved,
                "vetoes": decision.vetoes,
                "evaluator": decision.evaluator,
                "equity": account_state.equity,
                "requested_quantity": order.quantity,
                "adjusted_quantity": decision.adjusted_quantity,
            },
            decision.reason,
        )
        return decision

    # ------------------------------------------------------- account state

    def _build_account_state(self, order: Order) -> AccountState:
        """Assemble the AccountState the risk gate evaluates against.

        Equity and positions come from the broker; reference price, average daily
        volume, and recent returns come from the wired data source. This never
        reaches into the gate's logic; it only supplies inputs.
        """
        equity = self._net_liquidation()
        positions = self.positions()
        day_start, week_start = self._equity_baselines(equity)

        symbols = {order.symbol} | {p.symbol for p in positions if p.symbol}
        prices: dict[str, float] = {}
        adv: dict[str, float] = {}
        returns: dict[str, list[float]] = {}

        # Reference price for the order symbol: its own limit, else a live quote.
        entry_ref = order.limit_price
        if entry_ref is None:
            entry_ref = self._reference_price(order.symbol)
        if entry_ref is not None:
            prices[order.symbol] = entry_ref

        for position in positions:
            if position.market_price is not None:
                prices.setdefault(position.symbol, position.market_price)

        for symbol in symbols:
            volume, sym_returns = self._liquidity_and_returns(symbol)
            if volume is not None:
                adv[symbol] = volume
            if sym_returns:
                returns[symbol] = sym_returns

        return AccountState(
            equity=equity,
            day_start_equity=day_start,
            week_start_equity=week_start,
            positions=positions,
            prices=prices,
            average_daily_volume=adv,
            recent_returns=returns,
        )

    def _net_liquidation(self) -> float:
        """Total net liquidation value across reported accounts (0.0 if absent)."""
        total = 0.0
        found = False
        for summary in self.account_summary():
            value = summary.get_float("NetLiquidation")
            if value is not None:
                total += value
                found = True
        return total if found else 0.0

    def _equity_baselines(self, equity: float) -> tuple[Optional[float], Optional[float]]:
        """Day and week start equity for the drawdown breakers.

        Operator-supplied baselines win. Otherwise a best-effort in-process
        snapshot is used: the first equity seen in a given UTC day (or ISO week)
        becomes that period's start and is reused until the period rolls over.
        """
        if self._forced_day_start is not None or self._forced_week_start is not None:
            return self._forced_day_start, self._forced_week_start

        now = datetime.now(timezone.utc)
        iso = now.isocalendar()
        day = self._roll_anchor("day", now.strftime("%Y-%m-%d"), equity)
        week = self._roll_anchor("week", f"{iso[0]}-W{iso[1]:02d}", equity)
        return day, week

    def _roll_anchor(self, period: str, key: str, equity: float) -> float:
        anchor = self._equity_anchors.get(period)
        if anchor is None or anchor[0] != key:
            self._equity_anchors[period] = (key, equity)
            return equity
        return anchor[1]

    def _reference_price(self, symbol: str) -> Optional[float]:
        """Best available live reference price for an order with no limit price."""
        if self._data_source is None:
            return None
        try:
            quote = self._data_source.get_quote(symbol)
        except Exception as exc:  # noqa: BLE001 - missing price must not crash sizing
            self.log.warning("reference_price_unavailable", symbol=symbol, error=str(exc))
            return None
        for candidate in (quote.mid, quote.last, quote.bid, quote.ask):
            if candidate is not None:
                return candidate
        return None

    def _liquidity_and_returns(
        self, symbol: str
    ) -> tuple[Optional[float], list[float]]:
        """Average daily volume and recent close-to-close returns for a symbol.

        Returns (None, []) when no history is available; the gate then vetoes for
        unknown liquidity rather than guessing, which is the safe default.
        """
        if self._data_source is None:
            return None, []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_RISK_HISTORY_DAYS)
        try:
            bars = self._data_source.get_historical_bars(
                symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "1 day"
            )
        except Exception as exc:  # noqa: BLE001 - missing history must not crash sizing
            self.log.warning("risk_history_unavailable", symbol=symbol, error=str(exc))
            return None, []
        if bars is None or len(bars) == 0:
            return None, []

        adv: Optional[float] = None
        if "volume" in bars.columns:
            volume = bars["volume"].dropna()
            if len(volume) > 0:
                adv = float(volume.mean())

        returns: list[float] = []
        if "close" in bars.columns:
            change = bars["close"].pct_change().dropna()
            returns = [float(x) for x in change.tolist()]

        return adv, returns

    # -------------------------------------------------------- order placement

    def _stock(self, order: Order) -> Any:
        if order.sec_type != "STK":
            raise NotImplementedError(
                f"Only STK contracts are supported in stage 2, got {order.sec_type}"
            )
        from ib_async import Stock

        contract = Stock(order.symbol, order.exchange, order.currency)
        self.ib.qualifyContracts(contract)
        return contract

    @staticmethod
    def _entry_order(order: Order, quantity: float) -> Any:
        """Build the entry order object (never a naked market order on its own)."""
        from ib_async import LimitOrder, MarketOrder

        action = order.side.value
        if order.order_type is OrderType.LMT:
            return LimitOrder(action, quantity, order.limit_price)
        return MarketOrder(action, quantity)

    def place_order(self, order: Order) -> OrderPlacementResult:
        """Submit a single entry order WITH an attached protective stop.

        The order is rejected outright if it has no stop. There is no path that
        submits a bare entry. The risk gate runs first; a veto submits nothing.
        """
        self._require_connected()

        if not order.has_stop:
            reason = "Rejected: place_order requires an attached protective stop (no naked order path)."
            self.log.error("naked_order_rejected", symbol=order.symbol)
            self.audit.record("ORDER_REJECTED", {"symbol": order.symbol}, reason)
            raise NakedOrderRejected(reason)

        decision = self._evaluate_risk(order)
        if not decision.approved:
            return self._vetoed_result(order, decision, OrderKind.SINGLE_WITH_STOP)

        quantity = decision.adjusted_quantity or order.quantity
        contract = self._stock(order)
        from ib_async import StopOrder

        parent = self._entry_order(order, quantity)
        parent.transmit = False
        parent_trade = self.ib.placeOrder(contract, parent)
        parent_id = parent_trade.order.orderId

        stop = StopOrder(order.side.opposite.value, quantity, order.stop_price)
        stop.parentId = parent_id
        stop.transmit = True
        stop_trade = self.ib.placeOrder(contract, stop)

        ids = [parent_id, stop_trade.order.orderId]
        self._record_submission(order, quantity, ids, OrderKind.SINGLE_WITH_STOP)
        return OrderPlacementResult(
            accepted=True,
            reason="submitted single entry with attached stop",
            kind=OrderKind.SINGLE_WITH_STOP,
            risk_decision=decision,
            ib_order_ids=ids,
            symbol=order.symbol,
        )

    def place_bracket_order(
        self,
        order: Order,
        *,
        entry_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        target_price: Optional[float] = None,
    ) -> OrderPlacementResult:
        """Submit a native IBKR bracket (entry, protective stop, target).

        Prices default to the order's fields when not passed explicitly. All
        three are required; a bracket without a stop or target is rejected. The
        risk gate runs first; a veto submits nothing.
        """
        self._require_connected()

        entry = entry_price if entry_price is not None else order.limit_price
        stop = stop_price if stop_price is not None else order.stop_price
        target = target_price if target_price is not None else order.target_price

        if entry is None or stop is None or target is None:
            reason = (
                "Rejected: a bracket requires entry, stop, and target prices "
                f"(entry={entry}, stop={stop}, target={target})."
            )
            self.log.error("incomplete_bracket_rejected", symbol=order.symbol)
            self.audit.record("ORDER_REJECTED", {"symbol": order.symbol}, reason)
            raise NakedOrderRejected(reason)

        decision = self._evaluate_risk(order)
        if not decision.approved:
            return self._vetoed_result(order, decision, OrderKind.BRACKET)

        quantity = decision.adjusted_quantity or order.quantity
        contract = self._stock(order)
        bracket = self.ib.bracketOrder(
            order.side.value,
            quantity,
            limitPrice=entry,
            takeProfitPrice=target,
            stopLossPrice=stop,
        )

        ids: list[int] = []
        for child in (bracket.parent, bracket.takeProfit, bracket.stopLoss):
            trade = self.ib.placeOrder(contract, child)
            ids.append(trade.order.orderId)

        self._record_submission(order, quantity, ids, OrderKind.BRACKET)
        return OrderPlacementResult(
            accepted=True,
            reason="submitted native bracket (entry, stop, target)",
            kind=OrderKind.BRACKET,
            risk_decision=decision,
            ib_order_ids=ids,
            symbol=order.symbol,
        )

    def _vetoed_result(
        self, order: Order, decision: RiskDecision, kind: OrderKind
    ) -> OrderPlacementResult:
        self.log.warning("order_vetoed", symbol=order.symbol, reason=decision.reason)
        self.audit.record(
            "RISK_VETO",
            {"symbol": order.symbol, "vetoes": decision.vetoes},
            decision.reason,
        )
        return OrderPlacementResult(
            accepted=False,
            reason=decision.reason,
            kind=kind,
            risk_decision=decision,
            ib_order_ids=[],
            symbol=order.symbol,
        )

    def _record_submission(
        self, order: Order, quantity: float, ids: list[int], kind: OrderKind
    ) -> None:
        signed = quantity if order.side is OrderSide.BUY else -quantity
        self._intended_positions[order.symbol] = self._intended_positions.get(order.symbol, 0.0) + signed
        self._working_order_ids.extend(ids)
        self.log.info(
            "order_submitted",
            symbol=order.symbol,
            side=order.side.value,
            quantity=quantity,
            kind=kind.value,
            ids=ids,
        )
        self.audit.record(
            "ORDER_SUBMITTED",
            {
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": quantity,
                "kind": kind.value,
                "order_type": order.order_type.value,
                "limit_price": order.limit_price,
                "stop_price": order.stop_price,
                "target_price": order.target_price,
                "ib_order_ids": ids,
                "source": order.source,
            },
            f"Submitted {kind.value} for {order.symbol}",
        )

    # --------------------------------------------------------------- reads

    def account_summary(self) -> list[AccountSummary]:
        """Return account values grouped by account."""
        ib = self._require_connected()
        grouped: dict[str, dict[str, str]] = {}
        for value in ib.accountSummary():
            account = getattr(value, "account", "") or ""
            tag = getattr(value, "tag", None)
            raw = getattr(value, "value", None)
            if tag is None:
                continue
            grouped.setdefault(account, {})[str(tag)] = str(raw)
        return [AccountSummary(account=acct, values=vals) for acct, vals in grouped.items()]

    def positions(self) -> list[Position]:
        """Return current broker-reported positions."""
        ib = self._require_connected()
        out: list[Position] = []
        for p in ib.positions():
            contract = getattr(p, "contract", None)
            out.append(
                Position(
                    symbol=getattr(contract, "symbol", ""),
                    quantity=float(getattr(p, "position", 0.0)),
                    avg_cost=float(getattr(p, "avgCost", 0.0)),
                    account=getattr(p, "account", None),
                )
            )
        return out

    def open_orders(self) -> list[dict[str, Any]]:
        """Return open orders as plain dicts (broker-reported)."""
        ib = self._require_connected()
        out: list[dict[str, Any]] = []
        for trade in ib.openTrades():
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status = getattr(trade, "orderStatus", None)
            out.append(
                {
                    "order_id": getattr(order, "orderId", None),
                    "symbol": getattr(contract, "symbol", None),
                    "action": getattr(order, "action", None),
                    "order_type": getattr(order, "orderType", None),
                    "quantity": getattr(order, "totalQuantity", None),
                    "status": getattr(status, "status", None),
                }
            )
        return out

    def recent_fills(self) -> list[Fill]:
        """Return recent fills from this session."""
        ib = self._require_connected()
        out: list[Fill] = []
        for f in ib.fills():
            execution = getattr(f, "execution", None)
            contract = getattr(f, "contract", None)
            commission_report = getattr(f, "commissionReport", None)
            raw_side = getattr(execution, "side", "")
            side = OrderSide.BUY if str(raw_side).upper() in {"BOT", "BUY"} else OrderSide.SELL
            out.append(
                Fill(
                    symbol=getattr(contract, "symbol", ""),
                    side=side,
                    quantity=float(getattr(execution, "shares", 0.0)),
                    price=float(getattr(execution, "price", 0.0)),
                    ts_utc=str(getattr(execution, "time", "")),
                    exec_id=getattr(execution, "execId", None),
                    order_id=getattr(execution, "orderId", None),
                    commission=(
                        float(getattr(commission_report, "commission", 0.0))
                        if commission_report is not None
                        else None
                    ),
                )
            )
        return out

    # ------------------------------------------------------------ market data

    def get_historical_bars(self, symbol: str, start: Any, end: Any, bar_size: str = "1 day") -> Any:
        """Return historical bars via the wired IBKRDataSource."""
        self._require_connected()
        return self._data_source.get_historical_bars(symbol, start, end, bar_size)

    def get_quote(self, symbol: str) -> Any:
        """Return the latest quote via the wired IBKRDataSource."""
        self._require_connected()
        return self._data_source.get_quote(symbol)

    def subscribe_realtime(self, symbol: str, callback: Callable[[Any], None]) -> Any:
        """Subscribe to streaming ticks for symbol; invoke callback on updates.

        Returns the ib_async Ticker. The callback fires whenever this symbol's
        ticker updates.
        """
        ib = self._require_connected()
        contract = self._data_source._stock(symbol)  # qualified contract
        ticker = ib.reqMktData(contract, "", False, False)

        def _on_pending(tickers: Any) -> None:
            try:
                if ticker in tickers:
                    callback(ticker)
            except TypeError:
                callback(ticker)

        event = getattr(ib, "pendingTickersEvent", None)
        if event is not None:
            try:
                setattr(ib, "pendingTickersEvent", event + _on_pending)
            except TypeError:
                try:
                    event += _on_pending  # type: ignore[misc]
                except Exception:
                    pass
        self.audit.record("REALTIME_SUBSCRIBE", {"symbol": symbol}, "Subscribed to realtime data")
        return ticker

    # ---------------------------------------------------------- reconciliation

    def reconcile(self) -> ReconciliationReport:
        """Compare local intended state against broker-reported state.

        Logs and audits any drift between what we believe we hold or have working
        and what IBKR reports. This never places or cancels anything; it only
        observes and reports.
        """
        ib = self._require_connected()

        broker_positions = {p.symbol: p.quantity for p in self.positions()}
        position_drift: list[dict[str, Any]] = []
        symbols = set(self._intended_positions) | set(broker_positions)
        for symbol in sorted(symbols):
            intended = self._intended_positions.get(symbol, 0.0)
            reported = broker_positions.get(symbol, 0.0)
            if abs(intended - reported) > 1e-9:
                position_drift.append(
                    {"symbol": symbol, "intended": intended, "reported": reported}
                )

        reported_ids = {
            getattr(getattr(t, "order", None), "orderId", None) for t in ib.openTrades()
        }
        reported_ids.discard(None)
        order_drift: list[dict[str, Any]] = []
        for oid in self._working_order_ids:
            if oid not in reported_ids:
                order_drift.append({"order_id": oid, "state": "missing_at_broker"})

        in_sync = not position_drift and not order_drift
        report = ReconciliationReport(
            in_sync=in_sync,
            position_drift=position_drift,
            order_drift=order_drift,
            details="positions and working orders reconciled against IBKR",
        )

        if in_sync:
            self.log.info("reconcile_in_sync")
        else:
            self.log.warning(
                "reconcile_drift",
                position_drift=position_drift,
                order_drift=order_drift,
            )
        self.audit.record(
            "RECONCILE",
            {
                "in_sync": in_sync,
                "position_drift": position_drift,
                "order_drift": order_drift,
            },
            "Reconciliation in sync" if in_sync else "Reconciliation drift detected",
        )
        return report
