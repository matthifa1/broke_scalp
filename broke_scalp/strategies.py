"""Pluggable trading strategies.

A strategy is a function that takes a price Series (oldest → newest) and returns
a Signal (or None when there isn't enough data). Register new ones with the
@strategy decorator — that's the only place you touch to add or edit a strategy.

    @strategy("my_idea", min_bars=30)
    def my_idea(prices):
        ...
        return Signal("buy", "because reasons")

Signals drive the executor:  buy → go/stay long,  sell → go/stay short,
close → go flat,  hold → keep the current position.

Strategies with a position *lifecycle* (enter, hold a while, exit to flat)
stay stateless: each call they re-derive their virtual position by replaying
their rules over the price history (see _replay), so the live loop, the
dashboard and the backtester always agree. Timing parameters are in bars —
with the default 10s poll, 6 bars ≈ 1 minute.
"""

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    action: str                       # "buy" | "sell" | "close" | "hold"
    reason: str = ""
    score: float = 0.0
    extras: dict = field(default_factory=dict)


Strategy = Callable[[pd.Series], "Signal | None"]
STRATEGIES: dict[str, Strategy] = {}
MIN_BARS: dict[str, int] = {}
DESCRIPTIONS: dict[str, str] = {}


def strategy(name: str, min_bars: int = 1):
    def decorator(fn: Strategy) -> Strategy:
        STRATEGIES[name] = fn
        MIN_BARS[name] = min_bars
        DESCRIPTIONS[name] = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        return fn
    return decorator


VALID_ACTIONS = frozenset({"buy", "sell", "close", "hold"})


def generate(name: str, prices: pd.Series) -> "Signal | None":
    if name not in STRATEGIES:
        raise KeyError(f"Unknown strategy {name!r}. Available: {', '.join(STRATEGIES)}")
    if len(prices) < MIN_BARS[name]:
        return None
    sig = STRATEGIES[name](prices)
    # An unchecked action would silently be executed as a SHORT downstream
    # ("long" if action == "buy" else "short") — reject it here instead.
    if sig is not None and sig.action not in VALID_ACTIONS:
        raise ValueError(
            f"Strategy {name!r} returned invalid action {sig.action!r}; "
            f"must be one of {sorted(VALID_ACTIONS)}"
        )
    return sig


def available() -> list[str]:
    return list(STRATEGIES)


# ===========================================================================
# Default — ported verbatim from the original project (MACD triple vote).
# ===========================================================================

@strategy("macd_triple", min_bars=26)
def macd_triple(prices: pd.Series) -> "Signal | None":
    """MACD triple vote: line vs signal, line vs zero, signal vs zero (score >=2 buy)."""
    m = ind.macd(prices)
    if m.empty:
        return None
    macd_v = m.macd.iloc[-1]
    sig_v = m.signal.iloc[-1]

    score = 0
    reasons: list[str] = []
    if macd_v > sig_v:
        score += 1; reasons.append("MACD above signal")
    elif macd_v < sig_v:
        score -= 1; reasons.append("MACD below signal")
    if macd_v > 0:
        score += 1; reasons.append("MACD positive")
    elif macd_v < 0:
        score -= 1; reasons.append("MACD negative")
    if sig_v > 0:
        score += 1; reasons.append("signal positive")
    elif sig_v < 0:
        score -= 1; reasons.append("signal negative")

    action = "buy" if score >= 2 else "sell" if score <= -2 else "hold"
    return Signal(action, ", ".join(reasons), score, {"macd": macd_v, "signal": sig_v})


# ===========================================================================
# Short-term strategy #1 — EMA crossover (momentum / trend following).
# Holds a position aligned with the micro-trend and flips on the crossover.
# ===========================================================================

@strategy("ema_cross", min_bars=16)
def ema_cross(prices: pd.Series, fast: int = 5, slow: int = 15) -> "Signal | None":
    """Fast/slow EMA: long while EMA5 > EMA15, short while below; flips on the cross."""
    ef = ind.ema(prices, fast)
    es = ind.ema(prices, slow)
    f, s = ef.iloc[-1], es.iloc[-1]
    gap = f - s
    if f > s:
        return Signal("buy", f"EMA{fast} above EMA{slow} (+{gap:.4f})", gap, {"fast": f, "slow": s})
    if f < s:
        return Signal("sell", f"EMA{fast} below EMA{slow} ({gap:.4f})", gap, {"fast": f, "slow": s})
    return Signal("hold", f"EMA{fast} == EMA{slow}", 0.0)


# ===========================================================================
# Short-term strategy #2 — fast-RSI reversal (mean reversion at extremes).
# Buys oversold bounces, sells overbought fades, holds in the neutral zone.
# ===========================================================================

@strategy("rsi_reversal", min_bars=10)
def rsi_reversal(prices: pd.Series, period: int = 7, low: float = 25, high: float = 75) -> "Signal | None":
    """Fast RSI(7): buy when < 25 (oversold), sell when > 75 (overbought)."""
    r = ind.rsi(prices, period)
    if r.empty or pd.isna(r.iloc[-1]):
        return None
    v = r.iloc[-1]
    if v < low:
        return Signal("buy", f"RSI({period})={v:.1f} oversold", round(low - v, 1), {"rsi": v})
    if v > high:
        return Signal("sell", f"RSI({period})={v:.1f} overbought", round(v - high, 1), {"rsi": v})
    return Signal("hold", f"RSI({period})={v:.1f} neutral", 0.0, {"rsi": v})


# ===========================================================================
# Replay helpers — the strategies below manage a position lifecycle
# (enter, hold for a while, exit to flat) yet stay stateless: every call they
# re-derive their virtual position by replaying their rules over the recent
# price history, then emit the action that establishes that state
# (buy/sell to be in it, close to be flat). Repeats are executor no-ops.
# ===========================================================================


def _bar_vol(prices: pd.Series, window: int) -> pd.Series:
    """Rolling std of one-bar moves — the yardstick for what a 'significant'
    move is on the current instrument, whatever its price scale."""
    return prices.diff().rolling(window).std()


def _replay(prices: pd.Series, first_bar: int, try_enter, should_exit):
    """Drive entry/exit rules across the series and return the virtual state
    after the last bar: (side, entry_bar, entry_price, exit_bar, exit_reason).

    try_enter(i) -> "long" | "short" | None.
    should_exit(i, side, entry_bar, entry_price) -> reason string to close, else None.
    Rules must only use data up to bar i (rolling/ewm/diff are causal, so
    precomputed indicator series indexed at i are safe).
    """
    side: "str | None" = None
    entry_bar, entry_price = -1, 0.0
    exit_bar, exit_reason = -1, ""
    for i in range(max(first_bar, 1), len(prices)):
        if side is None:
            direction = try_enter(i)
            if direction is not None:
                side, entry_bar, entry_price = direction, i, float(prices.iloc[i])
        else:
            why = should_exit(i, side, entry_bar, entry_price)
            if why:
                side, exit_bar, exit_reason = None, i, why
    return side, entry_bar, entry_price, exit_bar, exit_reason


def _hold_signal(side: str, entry_price: float, held: int, extras: dict) -> Signal:
    since = "entering now" if held == 0 else f"holding for {held} bars"
    action = "buy" if side == "long" else "sell"
    return Signal(action, f"{side} ({since}, entry {entry_price:.4f})", float(held), extras)


# ===========================================================================
# Short-term strategy #3 — momentum burst scalp (hold: a few minutes).
# ===========================================================================

@strategy("momentum_scalp", min_bars=70)
def momentum_scalp(prices: pd.Series, burst: int = 6, vol_window: int = 60,
                   enter_k: float = 2.2, target_k: float = 1.6, stop_k: float = 1.4,
                   max_hold: int = 24) -> "Signal | None":
    """Minutes-scale scalp: rides an outsized 1-minute momentum burst; exits on vol-scaled take-profit/stop or a 4-minute time stop (@10s polls)."""
    p = prices.iloc[-300:].reset_index(drop=True)   # state only depends on recent bars
    vol = _bar_vol(p, vol_window)
    burst_move = p.diff(burst)
    scale = burst ** 0.5          # a k-bar move scales like bar-vol * sqrt(k)

    def try_enter(i):
        v, b = vol.iloc[i], burst_move.iloc[i]
        if pd.isna(v) or v <= 0 or pd.isna(b):
            return None
        thr = enter_k * float(v) * scale
        if b >= thr:
            return "long"
        if b <= -thr:
            return "short"
        return None

    def should_exit(i, side, entry_bar, entry_price):
        price = float(p.iloc[i])
        pnl = price - entry_price if side == "long" else entry_price - price
        if i - entry_bar >= max_hold:
            return f"time stop after {max_hold} bars (pnl {pnl:+.4f})"
        v = vol.iloc[i]
        if pd.isna(v) or v <= 0:
            return None
        if pnl >= target_k * float(v) * scale:
            return f"profit target hit ({pnl:+.4f})"
        if pnl <= -stop_k * float(v) * scale:
            return f"stop hit ({pnl:+.4f})"
        return None

    side, entry_bar, entry_price, exit_bar, why = _replay(
        p, vol_window + burst, try_enter, should_exit)
    last = len(p) - 1
    extras = {"burst": float(burst_move.iloc[-1])}
    if side is not None:
        return _hold_signal(side, entry_price, last - entry_bar, extras)
    if exit_bar == last:
        return Signal("close", f"closing: {why}", 0.0, extras)
    return Signal("close", f"flat — no momentum burst (last {burst}-bar move "
                           f"{burst_move.iloc[-1]:+.4f})", 0.0, extras)


# ===========================================================================
# Short-term strategy #4 — intraday trend rider (hold: minutes to hours).
#   fast EMA: 30 (≈5 min @10s), slow EMA: 120 (≈20 min @10s). 9/20
# ===========================================================================

@strategy("trend_ride", min_bars=35)
def trend_ride(prices: pd.Series, fast: int = 12, slow: int = 30,
               band_k: float = 0.5) -> "Signal | None":
    """Minutes-to-hours trend rider: long/short while EMA30/EMA120 (≈5/20 min @10s) clearly diverge, flat inside the volatility dead-band."""
    ef = float(ind.ema(prices, fast).iloc[-1])
    es = float(ind.ema(prices, slow).iloc[-1])
    vol = _bar_vol(prices, slow).iloc[-1]
    if pd.isna(vol) or vol <= 0:
        return None
    gap = ef - es
    band = band_k * float(vol) * (fast ** 0.5)
    extras = {"fast": ef, "slow": es, "gap": gap, "band": band}
    if gap > band:
        return Signal("buy", f"uptrend: EMA{fast}-EMA{slow} = +{gap:.4f} (band ±{band:.4f})", gap, extras)
    if gap < -band:
        return Signal("sell", f"downtrend: EMA{fast}-EMA{slow} = {gap:.4f} (band ±{band:.4f})", gap, extras)
    return Signal("close", f"no clear trend: gap {gap:+.4f} inside ±{band:.4f}", 0.0, extras)


# ===========================================================================
# Short-term strategy #5 — local-extreme bounce, take the first profit.
# ===========================================================================

@strategy("extreme_bounce", min_bars=80)
def extreme_bounce(prices: pd.Series, k: int = 3, extent: int = 36, vol_window: int = 60,
                   confirm_k: float = 0.5, profit_k: float = 0.25, stop_k: float = 6.0,
                   max_hold: int = 180) -> "Signal | None":
    """Local-extreme bounce: buys a confirmed local low / sells a local high, then closes at the first real profit (wide stop + 30-min time stop as safety valves)."""
    p = prices.iloc[-1500:].reset_index(drop=True)
    vol = _bar_vol(p, vol_window)

    def try_enter(i):
        pv = i - k                    # extreme candidate, confirmed k bars later
        if pv < extent:
            return None
        v = vol.iloc[i]
        if pd.isna(v) or v <= 0:
            return None
        window = p.iloc[pv - extent: i + 1]
        c, cur = float(p.iloc[pv]), float(p.iloc[i])
        confirm = confirm_k * float(v)          # require a real bounce, not noise
        if c <= float(window.min()) and cur >= c + confirm:
            return "long"                       # lowest of ~6.5 min and bouncing up
        if c >= float(window.max()) and cur <= c - confirm:
            return "short"                      # highest of ~6.5 min and fading down
        return None

    def should_exit(i, side, entry_bar, entry_price):
        price = float(p.iloc[i])
        pnl = price - entry_price if side == "long" else entry_price - price
        v = vol.iloc[i]
        # "in profit" = clears a small vol-scaled epsilon, so a 0.01 tick above
        # entry (which the spread would eat) doesn't count as profit.
        eps = profit_k * float(v) if not pd.isna(v) and v > 0 else 0.0
        if pnl > eps:
            return f"in profit ({pnl:+.4f})"
        if i - entry_bar >= max_hold:
            return f"time stop after {max_hold} bars (pnl {pnl:+.4f})"
        if eps and pnl <= -stop_k * float(v):
            return f"safety stop ({pnl:+.4f})"
        return None

    side, entry_bar, entry_price, exit_bar, why = _replay(
        p, max(vol_window + 1, extent + k), try_enter, should_exit)
    last = len(p) - 1
    if side is not None:
        return _hold_signal(side, entry_price, last - entry_bar, {"entry": entry_price})
    if exit_bar == last:
        return Signal("close", f"closing: {why}")
    return Signal("close", f"flat — waiting for a fresh {extent}-bar local high/low")


# ===========================================================================
# Short-term strategy #6 — Bollinger band fade (mean reversion, ~minutes).
# ===========================================================================

@strategy("bb_fade", min_bars=70)
def bb_fade(prices: pd.Series, period: int = 60, std_dev: float = 2.0,
            max_hold: int = 120) -> "Signal | None":
    """Bollinger fade: buys the re-entry after a poke below the lower band (short at the upper), exits at the middle band; stop beyond the band, 20-min time stop."""
    p = prices.iloc[-1000:].reset_index(drop=True)
    bands = ind.bollinger(p, period, std_dev)
    if bands.empty:
        return None
    lower, mid, upper = bands.lower, bands.middle, bands.upper

    def try_enter(i):
        if pd.isna(lower.iloc[i - 1]) or pd.isna(lower.iloc[i]):
            return None
        prev, cur = float(p.iloc[i - 1]), float(p.iloc[i])
        if prev < float(lower.iloc[i - 1]) and cur >= float(lower.iloc[i]):
            return "long"                 # came back inside from below
        if prev > float(upper.iloc[i - 1]) and cur <= float(upper.iloc[i]):
            return "short"                # came back inside from above
        return None

    def should_exit(i, side, entry_bar, entry_price):
        if i - entry_bar >= max_hold:
            return f"time stop after {max_hold} bars"
        m = mid.iloc[i]
        if pd.isna(m):
            return None
        cur, m_ = float(p.iloc[i]), float(m)
        if side == "long":
            if cur >= m_:
                return f"reached middle band ({cur:.4f} >= {m_:.4f})"
            lo = float(lower.iloc[i])
            if cur < lo - 0.5 * (m_ - lo):
                return "stopped: kept falling below the band"
        else:
            if cur <= m_:
                return f"reached middle band ({cur:.4f} <= {m_:.4f})"
            up = float(upper.iloc[i])
            if cur > up + 0.5 * (up - m_):
                return "stopped: kept rising above the band"
        return None

    side, entry_bar, entry_price, exit_bar, why = _replay(p, period + 1, try_enter, should_exit)
    last = len(p) - 1
    if side is not None:
        return _hold_signal(side, entry_price, last - entry_bar, {"entry": entry_price})
    if exit_bar == last:
        return Signal("close", f"closing: {why}")
    return Signal("close", "flat — waiting for a band poke and re-entry")
