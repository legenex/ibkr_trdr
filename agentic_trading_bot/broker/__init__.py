"""Broker layer: the ib_async wrapper with the paper and live toggle.

The broker is the only module that talks to TWS or IB Gateway. Every order it
submits passes the risk guardrails gate first, and no order is a naked market
order.
"""
