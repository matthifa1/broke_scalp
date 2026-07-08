# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A single-package Trading212 scalping bot. It attaches over CDP to an already-open,
logged-in Trading212 tab, polls the live price into SQLite, evaluates a pluggable
strategy, and places/closes CFD orders. Everything is one package
(`broke_scalp/`) driven by sub-commands — there is intentionally **no** separate
collector/viewer/trader package (that was the previous project's structure;
this one consolidates them).

## Run / dev

```bash
# venv lives at .venv (created with python -m venv)
.venv\Scripts\python.exe -m broke_scalp <command>     # Windows
python -m broke_scalp collect|trade|view|indicators|strategies|dashboard
pip install -r requirements.txt && playwright install chromium
```

There is no test suite. Verify changes by: importing the package, exercising
`db`/`indicators`/`strategies` on a synthetic Series, and—when Chrome is running
with `--remote-debugging-port=9222` and Trading212 is open—running `indicators`
or a dry-run `trade`.

## Architecture (one package, ~10 modules)

```
config.py      paths, CDP url, defaults, the shared UTF-8-safe logger
db.py          SQLite: prices(id, fetched_at, instrument, price REAL)
indicators.py  rsi / macd / bollinger — operate on a passed-in Series (loaded once)
strategies.py  @strategy registry + Signal; macd_triple (default), ema_cross, rsi_reversal,
               momentum_scalp, trend_ride, extreme_bounce, bb_fade (replay-based)
trader.py      Trading212 client (connect/read_price/select_instrument/buy/sell/close) + Executor
tracker.py     SignalTracker — virtual/paper P&L logging
cli.py         argparse sub-commands + the shared collect/trade async loop (_run_loop)
dashboard.py   Streamlit app (run via `streamlit run`, adds ROOT to sys.path)
__main__.py    → cli.main()
```

Data flow: `trader.read_price` → `db.save_price`/bucket → `db.price_series` →
`strategies.generate` → `tracker.update` (always, paper) + `Executor.apply` (trade only).

## backtest/ (separate package, deliberately independent)

`backtest/` replays stored prices bar-by-bar through the strategies
(`python -m backtest`, see `backtest/README.md`). It imports **only**
`broke_scalp.strategies`; it opens the SQLite DB read-only (or a CSV) and has
no dependency on the browser/trader/logger. Keep it that way — it must stay
runnable without Chrome or a live session, and it mimics live semantics:
strategy sees history up to the current bar, Executor-style long/short/flip
state machine, `--spread` for CFD round-trip cost.

## Trading212 specifics — VERIFIED, do not re-probe unless the UI changed

Selectors live at the top of `trader.py` and were captured/verified against the
live CFD practice UI (full buy→close round trip passed 8/8):

- Open ticket: `trade-button-buy` / `trade-button-sell`
- Size quick-buttons: `numpad-suggestion-{25|50|75|100}%`
- **Confirm order: `button-review-order`** (text "Confirm Buy"/"Confirm Sell") —
  NOT the in-ticket trade-button, which only toggles direction. This was a real bug.
- Close position: `trading-order-item-close-button`, then
  `flyout-cfdCloseOpenPosition` → `confirmation-button` ("Yes, close")
- Price (bid): `trade-button-sell` text → `parse_number`
- Current instrument: `instrument-title-ticker` / `instrument-screen-header-title`
- Open positions count: `instrument-screen-section-header-positions` ("1 position"/"none")
- Instrument selection: click `[data-testid^='watchlist-instrument-tile']` whose text matches

Two gotchas that WILL break naive clicks:
1. A full-screen `focused-layer` overlay intercepts pointer events, and the
   buttons are `<div>`s. Native Playwright `.click()` is blocked — `_click`
   dispatches the DOM click via `loc.evaluate("el => el.click()")` instead.
2. Tickets/flyouts render asynchronously. `_click` waits for the element to be
   visible (auto-wait) rather than using fixed sleeps — fixed sleeps caused the
   "25% not found" flakiness.

Numbers can be German (`1.234,56`) or US (`1,234.56`); `trader.parse_number`
handles both by treating the last separator as the decimal point.

Market hours: orders only execute when the instrument's market is open. Price
reading works any time. You can dry-run trade and read prices with the market
closed, but you cannot verify a real fill.

## Conventions / how to extend

- **Add a strategy:** one `@strategy("name", min_bars=N)` function in
  `strategies.py` returning a `Signal`. It auto-appears as a `--strategy` choice.
- **Add a broker:** write a client with the same public method names as
  `Trading212` (connect, disconnect, read_price, select_instrument,
  current_instrument, buy, sell, close) and let `Executor` drive it. (Only
  Trading212 is implemented; the original project also had a Plus500 stub.)
- Prices are stored as floats tagged by instrument — multiple instruments coexist.
- `Signal.action` is one of `buy`/`sell`/`close`/`hold`. The executor maps
  buy→long, sell→short, close→flat, hold→keep; switching direction closes first.
  `generate()` validates the action; anything else raises.
- Position-lifecycle strategies (momentum_scalp, extreme_bounce, bb_fade) stay
  stateless by replaying their entry/exit rules over recent history each call
  (`_replay` in strategies.py) and emitting the action that establishes the
  resulting state. Timing params are in bars: default 10s poll → 6 bars/min.
- Keep the logger UTF-8 reconfigure in `config.py` — Windows cp1252 consoles
  crash on non-ASCII (German broker labels, arrows) otherwise.

## Safety

Trader defaults to dry-run; `--live` is required for real orders. Automated UI
trading likely breaches Trading212's ToS and CFDs are leveraged — practice
account only until proven.
