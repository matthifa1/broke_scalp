"""Technical indicators.

Unlike the previous project, these operate on a price Series passed in by the
caller (loaded once) rather than each re-reading the database. Each returns an
empty Series / dataclass when there is not enough data.
"""

from dataclasses import dataclass

import pandas as pd


def ema(prices: pd.Series, span: int) -> pd.Series:
    return prices.ewm(span=span, adjust=False).mean()


def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    if len(prices) < period + 1:
        return pd.Series(dtype=float)
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return (100 - (100 / (1 + rs))).rename("rsi")


@dataclass
class MACDResult:
    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series

    @property
    def empty(self) -> bool:
        return self.macd.empty


def macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> MACDResult:
    if len(prices) < slow:
        empty = pd.Series(dtype=float)
        return MACDResult(empty, empty, empty)
    macd_line = (ema(prices, fast) - ema(prices, slow)).rename("macd")
    signal_line = ema(macd_line, signal).rename("signal")
    histogram = (macd_line - signal_line).rename("histogram")
    return MACDResult(macd_line, signal_line, histogram)


@dataclass
class BollingerResult:
    upper: pd.Series
    middle: pd.Series
    lower: pd.Series

    @property
    def empty(self) -> bool:
        return self.middle.empty


def bollinger(prices: pd.Series, period: int = 20, std_dev: float = 2.0) -> BollingerResult:
    if len(prices) < period:
        empty = pd.Series(dtype=float)
        return BollingerResult(empty, empty, empty)
    sma = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()
    return BollingerResult(
        upper=(sma + std_dev * std).rename("upper"),
        middle=sma.rename("middle"),
        lower=(sma - std_dev * std).rename("lower"),
    )
