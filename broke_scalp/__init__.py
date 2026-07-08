"""broke_scalp — a single-package Trading212 scalping bot.

Public pieces:
    db          price storage (init_db, save_price, load_prices, price_series, ...)
    indicators  rsi / macd / bollinger on a price Series
    strategies  pluggable signal generators (registry + Signal)
    trader      Trading212 client + Executor
    tracker     virtual (paper) position logger
    cli         command-line entry point (python -m broke_scalp ...)
"""

from . import db, indicators, strategies
from .strategies import Signal, generate as generate_signal
from .trader import Executor, Trading212

__all__ = ["db", "indicators", "strategies", "Signal", "generate_signal",
           "Executor", "Trading212"]
