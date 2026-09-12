"""Clean-core bot for the validated IMBALANCE strategy.

generate_setups (EMA50/200 + OB + imbalance + fib) is the SINGLE hard entry gate.
Reuses: config (testnet keys + telegram), backtest.faithful_imbalance (detection).
Deliberately bypasses the old VIP/thesis/LLM/score-blend entry path.
"""
