"""Standalone backtester for broke_scalp strategies.

Independent of the live bot: imports only `broke_scalp.strategies` (and its
indicators), reads price data read-only (SQLite or CSV), and never touches the
browser, the live database writes, or the trading loop.

    python -m backtest                      # list instruments / compare all strategies
    python -m backtest --strategy ema_cross --trades
"""
