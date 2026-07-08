"""Backtest CLI: load prices (SQLite read-only, or any CSV) and replay one or
all strategies from broke_scalp.strategies over them.

    python -m backtest                                # single instrument: compare all
    python -m backtest --instrument GER40             # pick when several are stored
    python -m backtest --strategy ema_cross --trades  # detail + trade list
    python -m backtest --csv prices.csv --spread 1.2
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

# Make the repo root importable so this also works when run as a plain script
# (`python backtest/cli.py`) from any working directory.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import engine                      # noqa: E402
from broke_scalp import strategies               # noqa: E402

DEFAULT_DB = ROOT / "broke_scalp.db"


# --- data loading ------------------------------------------------------------

def load_db(db_path: Path, instrument: str | None) -> tuple[pd.DataFrame, str]:
    """Read prices for one instrument from the bot's SQLite DB (read-only)."""
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        insts = [r[0] for r in con.execute(
            "SELECT DISTINCT instrument FROM prices ORDER BY instrument")]
        if not insts:
            sys.exit("No prices stored yet — run `python -m broke_scalp collect` first.")
        chosen = _pick_instrument(insts, instrument)
        df = pd.read_sql_query(
            "SELECT fetched_at, price FROM prices WHERE instrument = ? "
            "ORDER BY fetched_at, id",
            con, params=[chosen],
        )
    finally:
        con.close()
    return df, chosen


def _pick_instrument(stored: list[str], query: str | None) -> str:
    if query is None:
        if len(stored) == 1:
            return stored[0]
        sys.exit("Multiple instruments stored — pick one with --instrument:\n  "
                 + "\n  ".join(stored))
    exact = [i for i in stored if i.lower() == query.lower()]
    if exact:
        return exact[0]
    partial = [i for i in stored if query.lower() in i.lower()]
    if len(partial) == 1:
        return partial[0]
    sys.exit(f"No unique instrument matching {query!r}. Stored: {', '.join(stored)}")


def load_csv(path: Path) -> tuple[pd.DataFrame, str]:
    """Load a CSV with a price/close column and an optional time column."""
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    df = pd.read_csv(path)
    price_col = next((c for c in df.columns if c.lower() in ("price", "close")), None)
    if price_col is None:
        if df.shape[1] == 1:
            price_col = df.columns[0]
        else:
            sys.exit(f"CSV needs a 'price' or 'close' column; found: {', '.join(df.columns)}")
    time_col = next((c for c in df.columns
                     if c.lower() in ("fetched_at", "time", "timestamp", "date", "datetime")), None)
    out = pd.DataFrame({
        "fetched_at": df[time_col].astype(str) if time_col else [""] * len(df),
        "price": df[price_col],
    })
    return out, path.name


# --- reporting ----------------------------------------------------------------

def _print_header(label: str, df: pd.DataFrame, spread: float) -> None:
    first, last = df["fetched_at"].iloc[0], df["fetched_at"].iloc[-1]
    span = f" ({first} → {last})" if str(first).strip() else ""
    print(f"Backtest: {label} — {len(df)} bars{span}, spread {spread:g}\n")


def _print_comparison(results: list[engine.Result], buy_hold: float) -> None:
    print(f"{'strategy':<14} {'trades':>6} {'win%':>7} {'return%':>9} "
          f"{'avg/trade%':>11} {'maxDD%':>8}")
    for r in sorted(results, key=lambda r: r.total_return, reverse=True):
        print(f"{r.strategy:<14} {r.n_trades:>6} {r.win_rate:>7.1f} {r.total_return:>9.2f} "
              f"{r.avg_trade:>11.3f} {r.max_drawdown:>8.2f}")
    print(f"\nbuy & hold over the same bars: {buy_hold:+.2f}%")


def _print_detail(r: engine.Result, buy_hold: float, show_trades: bool) -> None:
    print(f"strategy      : {r.strategy}")
    print(f"trades        : {r.n_trades} ({r.wins} wins, {r.win_rate:.1f}% win rate)")
    print(f"total return  : {r.total_return:+.2f}%   (buy & hold {buy_hold:+.2f}%)")
    print(f"avg per trade : {r.avg_trade:+.3f}%")
    print(f"max drawdown  : {r.max_drawdown:.2f}%")
    forced = sum(1 for t in r.trades if t.forced)
    if forced:
        print(f"note          : {forced} position was still open and got closed on the last bar")
    if show_trades and r.trades:
        print(f"\n{'#':>3}  {'side':<5} {'entry':>12} {'exit':>12} {'pnl':>10} {'pct':>8}  when")
        for n, t in enumerate(r.trades, 1):
            when = f"{t.entry_time} → {t.exit_time}".strip(" →")
            mark = " (end)" if t.forced else ""
            print(f"{n:>3}  {t.side:<5} {t.entry_fill:>12.4f} {t.exit_fill:>12.4f} "
                  f"{t.pnl:>+10.4f} {t.pct:>+7.3f}%  {when}{mark}")


# --- entry point ----------------------------------------------------------------

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="backtest",
                                description="Backtest broke_scalp strategies on stored prices.")
    p.add_argument("--instrument", default=None,
                   help="Instrument in the DB (exact or unique partial match).")
    p.add_argument("--strategy", default=None, choices=strategies.available(),
                   help="Single strategy to test in detail (default: compare all).")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help="SQLite DB to read prices from (default: %(default)s)")
    p.add_argument("--csv", type=Path, default=None,
                   help="Load prices from a CSV (price/close column) instead of the DB.")
    p.add_argument("--spread", type=float, default=0.0,
                   help="Full bid/ask spread in price units, paid per round trip (default: 0).")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="Use only the newest N prices.")
    p.add_argument("--trades", action="store_true",
                   help="List individual trades (with --strategy).")
    args = p.parse_args(argv)

    if args.csv is not None:
        df, label = load_csv(args.csv)
    else:
        df, label = load_db(args.db, args.instrument)
    df = df.assign(price=pd.to_numeric(df["price"], errors="coerce")) \
           .dropna(subset=["price"]).reset_index(drop=True)
    if args.limit:
        df = df.tail(args.limit).reset_index(drop=True)
    if len(df) < 2:
        sys.exit(f"Not enough usable prices ({len(df)}) to backtest.")

    prices, times = df["price"], df["fetched_at"]
    buy_hold = (float(prices.iloc[-1]) / float(prices.iloc[0]) - 1) * 100
    _print_header(label, df, args.spread)

    names = [args.strategy] if args.strategy else strategies.available()
    results = [engine.run(prices, name, times=times, spread=args.spread) for name in names]

    if args.strategy:
        _print_detail(results[0], buy_hold, args.trades)
    else:
        _print_comparison(results, buy_hold)


if __name__ == "__main__":
    main()
