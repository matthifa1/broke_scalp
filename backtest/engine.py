"""Bar-by-bar backtest engine.

Replays a price series through a strategy exactly the way the live loop trades
it: at bar i the strategy sees only prices[0..i], and the signal drives the
same state machine as the live Executor (buy → long, sell → short,
close → flat, hold → keep; flipping direction closes the old position first).

Costs: `spread` is the full bid/ask spread in price units. The collector
stores the bid, so longs enter at price + spread and exit at price, while
shorts enter at price and exit at price + spread — every round trip pays one
spread, like a real CFD.

Note the engine is O(n²): each bar re-runs the strategy on the whole history,
because the strategies are written to take a full series (same as live).
Fine for tens of thousands of bars; use --limit beyond that.
"""

from dataclasses import dataclass

import pandas as pd

from broke_scalp import strategies


@dataclass
class Trade:
    side: str                # "long" | "short"
    entry_bar: int
    exit_bar: int
    entry_fill: float
    exit_fill: float
    entry_time: str = ""
    exit_time: str = ""
    forced: bool = False     # closed at the end of the data, not by a signal

    @property
    def pnl(self) -> float:
        return (self.exit_fill - self.entry_fill) if self.side == "long" \
            else (self.entry_fill - self.exit_fill)

    @property
    def pct(self) -> float:
        return self.pnl / self.entry_fill * 100


@dataclass
class Result:
    strategy: str
    n_bars: int
    spread: float
    trades: list[Trade]
    equity: pd.Series        # mark-to-market equity multiplier per bar (starts at 1.0)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades * 100 if self.trades else 0.0

    @property
    def total_return(self) -> float:
        return (float(self.equity.iloc[-1]) - 1) * 100 if len(self.equity) else 0.0

    @property
    def avg_trade(self) -> float:
        return sum(t.pct for t in self.trades) / self.n_trades if self.trades else 0.0

    @property
    def max_drawdown(self) -> float:
        if self.equity.empty:
            return 0.0
        peak = self.equity.cummax()
        return float(((self.equity / peak) - 1).min() * 100)


def run(prices: pd.Series, strategy_name: str, *,
        times: pd.Series | None = None, spread: float = 0.0) -> Result:
    """Replay `prices` (oldest → newest) through one strategy."""
    prices = pd.to_numeric(prices, errors="coerce")
    mask = prices.notna()
    prices = prices[mask].reset_index(drop=True)
    if times is not None:
        times = times[mask].reset_index(drop=True)   # keep timestamps aligned

    position: str | None = None
    entry_fill = 0.0
    entry_bar = 0
    realized = 1.0            # compounded equity multiplier from closed trades
    trades: list[Trade] = []
    equity: list[float] = []

    def time_at(i: int) -> str:
        return str(times.iloc[i]) if times is not None else ""

    def close_at(i: int, price: float, forced: bool = False) -> None:
        nonlocal position, realized
        exit_fill = price if position == "long" else price + spread
        t = Trade(position, entry_bar, i, entry_fill, exit_fill,
                  time_at(entry_bar), time_at(i), forced)
        trades.append(t)
        realized *= 1 + t.pct / 100
        position = None

    for i in range(len(prices)):
        price = float(prices.iloc[i])
        sig = strategies.generate(strategy_name, prices.iloc[: i + 1])
        action = sig.action if sig is not None else "hold"

        if action == "close":
            if position is not None:
                close_at(i, price)
        elif action != "hold":
            target = "long" if action == "buy" else "short"
            if target != position:
                if position is not None:
                    close_at(i, price)
                entry_fill = price + spread if target == "long" else price
                entry_bar = i
                position = target

        if position == "long":
            unrealized = (price - entry_fill) / entry_fill
        elif position == "short":
            unrealized = (entry_fill - (price + spread)) / entry_fill
        else:
            unrealized = 0.0
        equity.append(realized * (1 + unrealized))

    if position is not None:                        # settle the open position
        close_at(len(prices) - 1, float(prices.iloc[-1]), forced=True)
        equity[-1] = realized

    return Result(strategy_name, len(prices), spread, trades, pd.Series(equity, dtype=float))
