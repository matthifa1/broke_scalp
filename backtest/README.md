# backtest

Standalone backtester for the `broke_scalp` strategies. It imports only
`broke_scalp.strategies` (and its indicators); it never touches the browser,
never writes to the database (SQLite is opened read-only), and shares no
runtime with the bot.

## How it works

Prices are replayed bar by bar exactly like the live loop: at bar *i* the
strategy sees only the history up to *i*, and its signal drives the same
state machine as the live `Executor` — `buy` → long, `sell` → short,
`close` → flat, `hold` → keep, flipping direction closes first. A position still open at the
end of the data is settled on the last bar (marked in the trade list).

`--spread` models CFD costs: the stored price is the bid, so longs enter at
`price + spread` / exit at `price`, shorts enter at `price` / exit at
`price + spread` — one full spread per round trip. Run with a realistic
spread; scalping results without it are meaningless.

## Usage

```bash
# compare all strategies on the stored data (picks the instrument if only one)
python -m backtest

# choose instrument / DB, apply a spread
python -m backtest --instrument GER40 --spread 1.2

# one strategy in detail, with every trade listed
python -m backtest --strategy ema_cross --trades

# only the newest 500 prices
python -m backtest --limit 500

# any CSV with a price/close column (optional time/date column)
python -m backtest --csv prices.csv
```

Metrics: number of trades, win rate, compounded total return, average return
per trade, max drawdown (mark-to-market), and buy & hold over the same bars
for comparison.

Caveat: the engine is O(n²) because the strategies take a full series each bar
(same as live). Tens of thousands of bars are fine; use `--limit` beyond that.
