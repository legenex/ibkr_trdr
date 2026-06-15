"""A fake ib_async IB object for tests.

It stands in for a live TWS or IB Gateway connection so the broker can be
exercised end to end in CI without a socket. It uses real ib_async order objects
(LimitOrder, StopOrder, MarketOrder) so order construction is tested for real;
only the transport (connect, placeOrder, reqHistoricalData, ...) is faked.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable


def funded_account(net_liquidation: float = 100_000.0, account: str = "DU1") -> list[Any]:
    """Default account summary: a single paper account with net liquidation set.

    The real risk gate sizes against equity, so a connected fake broker reports a
    funded account by default. Tests override `ib._account` to change this.
    """
    return [
        SimpleNamespace(
            account=account, tag="NetLiquidation", value=str(net_liquidation), currency="USD"
        )
    ]


def liquid_daily_bars(
    n: int = 40, close: float = 100.0, volume: float = 50_000_000.0
) -> list[Any]:
    """Default daily history: liquid, gently varying bars.

    The risk gate reads average daily volume and recent returns from history, so
    a connected fake broker returns a liquid series by default (ADV well above the
    minimum). Tests override `ib._bars` to drive liquidity or volume vetoes.
    """
    bars: list[Any] = []
    for i in range(n):
        price = close + (i % 5) * 0.1  # small deterministic wiggle for nonzero returns
        bars.append(
            SimpleNamespace(
                date=f"2026-01-{(i % 28) + 1:02d}",
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=volume,
            )
        )
    return bars


class FakeEvent:
    """Minimal stand-in for an ib_async event supporting `event + handler`."""

    def __init__(self) -> None:
        self.handlers: list[Callable[..., Any]] = []

    def __add__(self, handler: Callable[..., Any]) -> "FakeEvent":
        self.handlers.append(handler)
        return self

    def __iadd__(self, handler: Callable[..., Any]) -> "FakeEvent":
        self.handlers.append(handler)
        return self

    def emit(self, *args: Any, **kwargs: Any) -> None:
        for handler in list(self.handlers):
            handler(*args, **kwargs)


class FakeIB:
    """A fake ib_async IB object for tests. No network, deterministic ids."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self._connected = False
        self._next_id = 1000
        self.connect_calls: list[dict[str, Any]] = []
        self.placed: list[tuple[Any, Any]] = []

        # Canned read data, settable by tests. Defaults make a connected broker
        # look funded and liquid so the real risk gate has equity and history to
        # work with; tests override any of these to exercise specific verdicts.
        self._account: list[Any] = funded_account()
        self._positions: list[Any] = []
        self._fills: list[Any] = []
        self._open_trades: list[Any] = []
        self._bars: list[Any] = liquid_daily_bars()

        # Events.
        self.disconnectedEvent = FakeEvent()
        self.errorEvent = FakeEvent()
        self.execDetailsEvent = FakeEvent()
        self.pendingTickersEvent = FakeEvent()

    # connection
    def connect(
        self, host: str, port: int, clientId: int = 1, timeout: float = 10.0, readonly: bool = False
    ) -> None:
        self.connect_calls.append({"host": host, "port": port, "clientId": clientId})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionRefusedError("connection refused (fake)")
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False
        self.disconnectedEvent.emit()

    # contracts and orders
    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        return list(contracts)

    def _assign_id(self, order: Any) -> Any:
        self._next_id += 1
        order.orderId = self._next_id
        return order

    def placeOrder(self, contract: Any, order: Any) -> Any:
        self._assign_id(order)
        self.placed.append((contract, order))
        return SimpleNamespace(
            order=order,
            contract=contract,
            orderStatus=SimpleNamespace(status="PreSubmitted"),
        )

    def bracketOrder(
        self,
        action: str,
        quantity: float,
        limitPrice: float,
        takeProfitPrice: float,
        stopLossPrice: float,
    ) -> Any:
        from ib_async import LimitOrder, StopOrder

        reverse = "SELL" if action == "BUY" else "BUY"
        parent = LimitOrder(action, quantity, limitPrice)
        parent.transmit = False
        take_profit = LimitOrder(reverse, quantity, takeProfitPrice)
        take_profit.transmit = False
        stop_loss = StopOrder(reverse, quantity, stopLossPrice)
        stop_loss.transmit = True
        return SimpleNamespace(parent=parent, takeProfit=take_profit, stopLoss=stop_loss)

    # reads
    def accountSummary(self, account: str = "") -> list[Any]:
        return self._account

    def positions(self, account: str = "") -> list[Any]:
        return self._positions

    def fills(self) -> list[Any]:
        return self._fills

    def openTrades(self) -> list[Any]:
        return self._open_trades

    def openOrders(self) -> list[Any]:
        return [getattr(t, "order", None) for t in self._open_trades]

    # market data
    def reqHistoricalData(self, contract: Any, **kwargs: Any) -> list[Any]:
        return self._bars

    def reqMktData(self, contract: Any, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(bid=10.0, ask=10.1, last=10.05)

    def cancelMktData(self, contract: Any) -> None:
        pass

    def sleep(self, seconds: float) -> None:
        pass
