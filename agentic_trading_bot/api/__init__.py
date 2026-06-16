"""FastAPI layer over the existing harness modules, for the React frontend.

It reads real state from config, the audit trail, the regime detector, the
approval queue, the holdout budget, and (when reachable) the broker. It never
bypasses the risk gate; the only mutation it exposes is toggling the kill-switch
sentinel, which the orchestrator and risk gate already honor.
"""
