"""Self-learning primitives (Stage 7.5).

These exist to make the loop harder to fool: a cumulative trial ledger so
overfitting corrections see every hypothesis ever tried against a family, and a
consumable holdout budget so truly-unseen data cannot be mined indefinitely.
Neither touches risk or execution; the learning loop proposes only.
"""
