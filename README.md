# broke_scalp

A compact Trading212 scalping bot. It attaches to an **already-open, logged-in
Trading212 tab** (via Chrome's remote debugging port), polls the live price of a
chosen instrument into a local SQLite database, evaluates a pluggable trading
strategy, and can place/close CFD orders automatically.

Everything is one package driven by sub-commands — collecting, viewing, trading,
indicators and the dashboard are all `python -m broke_scalp <command>`.

> ⚠️ Automated trading on a broker's web UI very likely violates its terms of
> service, and CFDs are leveraged. Trade on a **practice account** and keep the
> trader in **dry-run** (the default) until you trust it.

## Setup

```bash
# from the broke_scalp project root
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on Unix)
pip install -r requirements.txt
playwright install chromium
```

Start Chrome with the debugging port and log into Trading212 in it:

```bash
chrome.exe --remote-debugging-port=9222
```

## Commands

```bash
python -m broke_scalp collect      [--instrument TSLA] [--poll 10] [--interval N] [--strategy NAME]
python -m broke_scalp trade        [--instrument TSLA] [--size 25] [--poll 10] [--strategy NAME] [--live]
python -m broke_scalp view         [--instrument TSLA] [--limit 100] [--delete]
python -m broke_scalp indicators   [--instrument TSLA] [--strategy NAME]
python -m broke_scalp strategies
python -m broke_scalp dashboard
```

### collect
Polls the current price and stores one row per poll. It also *paper-trades* the
chosen strategy into `trading.log` so you can judge it without placing orders.

### trade
Same loop as `collect`, but acts on the signal through the broker. **Dry-run by
default** — it logs the buttons it *would* click. Add `--live` to place real
orders. `--size` chooses the position size quick-button (25/50/75/100 %).

### view
Prints the last `--limit` rows (optionally for one `--instrument`).
`--delete` clears stored prices (asks for confirmation).

### indicators
Prints the latest RSI / MACD / Bollinger values and the chosen strategy's signal.

### dashboard
Launches a Streamlit app (http://localhost:8501) with an instrument + strategy
selector, a price chart (y-axis bounded to the data), the live indicators, and
the current signal.

## Key options

| Option | Meaning |
|---|---|
| `--instrument` | Select an instrument by name/ticker from your **watchlist** (e.g. `TSLA`, `GER40`). Omit to use whatever instrument tab is open. |
| `--poll` | Seconds between price reads. One read = one stored price, so this sets your data resolution. ~26 reads are needed before the default strategy produces a signal. |
| `--interval` | Aggregate prices into N-minute recursive-mean buckets instead of storing every poll. |
| `--size` | Position size quick-button percentage (trade only). |
| `--strategy` | Which strategy to evaluate (see below). |
| `--live` | Place real orders (trade only). Without it, dry-run. |

## Strategies

Run `python -m broke_scalp strategies` to list them. Built-in:

- **`macd_triple`** *(default)* — MACD triple vote (line vs signal, line vs zero,
  signal vs zero); score ≥ 2 → buy, ≤ −2 → sell. Ported from the original project.
- **`ema_cross`** — short-term momentum: long while EMA(5) > EMA(15), short while
  below; flips on the crossover.
- **`rsi_reversal`** — short-term mean reversion: buy when fast RSI(7) < 25
  (oversold), sell when > 75 (overbought).

Add your own in [`broke_scalp/strategies.py`](broke_scalp/strategies.py) with the
`@strategy(name, min_bars=...)` decorator — that's the only file you touch:

```python
@strategy("my_idea", min_bars=30)
def my_idea(prices):
    return Signal("buy", "because reasons")
```

It immediately becomes a valid `--strategy` choice everywhere.

## How signals become trades

`buy` → go/stay **long**, `sell` → go/stay **short**, `hold` → keep the current
position. Switching direction closes the open position first. Repeated
same-direction signals are no-ops, so a position is never opened twice.
