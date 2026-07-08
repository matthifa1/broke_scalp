"""Single entry point for broke_scalp. One module, many sub-commands:

    collect     poll prices into the DB (paper-trades the strategy in the log)
    trade       collect + act on signals via the broker (dry-run unless --live)
    view        show / delete stored prices
    clear       delete the entire contents of the price DB
    indicators  print latest indicator + strategy signal
    strategies  list available strategies
    dashboard   launch the Streamlit dashboard
"""

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import strategies
from . import indicators as ind
from .config import (CDP_URL, DEFAULT_POLL_SECONDS, DEFAULT_SIZE,
                     DEFAULT_STRATEGY, get_logger)
from .db import (delete_prices, init_db, last_row, load_prices, price_series,
                 save_price, update_price)
from .tracker import SignalTracker
from .trader import Executor, Trading212

logger = get_logger()
SIZES = (25, 50, 75, 100)


# --- price storage with optional recursive-mean bucketing -------------------

def _store(instrument: str, price: float, interval: float | None) -> None:
    if not interval:
        save_price(price, instrument)
        return
    row = last_row(instrument)
    if row is not None:
        rid, ts, last_p = row
        try:
            start = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            start = datetime.now()
        if datetime.now() - start < timedelta(minutes=interval):
            update_price(rid, (last_p + price) / 2)   # recursive mean within window
            return
    save_price(price, instrument)


# --- the shared collect/trade loop ------------------------------------------

# Try to re-attach/reconnect every N consecutive failed price reads, and give
# up entirely after MAX (at the default 10s poll that is ~5 minutes of failure).
_RECOVER_EVERY = 5
_MAX_FAILURES = 30


async def _resolve_instrument(client: Trading212) -> str:
    """Determine the active instrument, retrying transient read failures.
    Refuses to run against an unreadable header — otherwise every price would
    be stored (and traded) under the bogus instrument 'UNKNOWN'."""
    for _ in range(3):
        inst = await client.current_instrument()
        if inst != "UNKNOWN":
            return inst
        await asyncio.sleep(1)
    raise RuntimeError(
        "Could not determine the active instrument — is an instrument chart open in Trading212?"
    )


async def _run_loop(*, do_trade, instrument, size, poll, interval, strategy_name, live, cdp):
    init_db()
    client = Trading212(cdp_url=cdp, dry_run=not live)
    await client.connect()
    try:
        if instrument:
            await client.select_instrument(instrument)
        inst = await _resolve_instrument(client)

        mode = ("LIVE TRADING" if live else "trading DRY-RUN") if do_trade else "collect only"
        logger.info(f"Instrument: {inst} | strategy: {strategy_name} | poll: {poll}s | "
                    + (f"bucket: {interval}min | " if interval else "") + mode)

        tracker = SignalTracker()
        executor = Executor(client, size) if do_trade else None
        if executor is not None:
            await executor.sync()   # adopt a pre-existing open position as "unknown"

        failures = 0
        while True:
            price = await client.read_price()
            if price is None:
                failures += 1
                logger.warning(f"no price extracted ({failures}/{_MAX_FAILURES} in a row)")
                if failures >= _MAX_FAILURES:
                    raise RuntimeError(
                        f"price extraction failed {failures} times in a row — "
                        "the tab is gone or the selector is broken; giving up"
                    )
                if failures % _RECOVER_EVERY == 0:
                    # The tab may have been closed/navigated, or the CDP
                    # connection dropped — try the cheap fix, then the full one.
                    if not await client.ensure_page():
                        await client.reconnect()
                await asyncio.sleep(poll)
                continue
            failures = 0

            # Guard against the user clicking another instrument in the browser:
            # storing/trading it under the old name would corrupt data and trades.
            current = await client.current_instrument()
            if current not in ("UNKNOWN", inst):
                raise RuntimeError(
                    f"active instrument changed from {inst!r} to {current!r} — stopping. "
                    "Restart the bot to trade the new instrument."
                )

            _store(inst, price, interval)

            try:
                sig = strategies.generate(strategy_name, price_series(inst))
            except Exception as exc:
                logger.error(f"strategy {strategy_name!r} failed: {exc}")
                sig = None
            if sig is not None:
                tracker.update(sig, price)
                if executor is not None:
                    try:
                        await executor.apply(sig)
                    except Exception as exc:
                        # Keep the loop alive: log, re-align position state with
                        # the UI, and let the next signal try again.
                        logger.error(f"order execution failed: {exc}")
                        await executor.sync()
            await asyncio.sleep(poll)
    finally:
        await client.disconnect()


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        logger.info("stopped by user (Ctrl+C)")


# --- sub-command implementations --------------------------------------------

def cmd_collect(args):
    _run(_run_loop(do_trade=False, instrument=args.instrument, size=DEFAULT_SIZE,
                   poll=args.poll, interval=args.interval,
                   strategy_name=args.strategy, live=False, cdp=args.cdp))


def cmd_trade(args):
    _run(_run_loop(do_trade=True, instrument=args.instrument, size=args.size,
                   poll=args.poll, interval=args.interval,
                   strategy_name=args.strategy, live=args.live, cdp=args.cdp))


def cmd_view(args):
    init_db()
    if args.delete:
        confirm = input(f"Delete prices for {args.instrument or 'ALL instruments'}? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return
        print(f"Deleted {delete_prices(args.instrument)} rows.")
        return
    df = load_prices(args.instrument, args.limit)
    if df.empty:
        print("No prices stored yet.")
        return
    print(df.to_string(index=False))
    print(f"\n{len(df)} rows shown.")


def cmd_clear(args):
    init_db()
    if not args.yes:
        confirm = input("Delete the ENTIRE price DB (all instruments)? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return
    print(f"Deleted {delete_prices(None)} rows.")


def cmd_indicators(args):
    init_db()
    prices = price_series(args.instrument)
    print(f"{len(prices)} prices for {args.instrument or 'all instruments'}")
    if len(prices):
        r, m, b = ind.rsi(prices), ind.macd(prices), ind.bollinger(prices)
        if not r.empty:
            print(f"  RSI(14)   : {r.iloc[-1]:.2f}")
        if not m.empty:
            print(f"  MACD      : {m.macd.iloc[-1]:.4f}  signal {m.signal.iloc[-1]:.4f}")
        if not b.empty:
            print(f"  Bollinger : U {b.upper.iloc[-1]:.4f}  M {b.middle.iloc[-1]:.4f}  L {b.lower.iloc[-1]:.4f}")
    sig = strategies.generate(args.strategy, prices)
    if sig is None:
        print(f"  Signal [{args.strategy}]: not enough data")
    else:
        print(f"  Signal [{args.strategy}]: {sig.action.upper()} — {sig.reason}")


def cmd_strategies(args):
    print("Available strategies:")
    for name in strategies.available():
        desc = strategies.DESCRIPTIONS.get(name, "")
        marker = " (default)" if name == DEFAULT_STRATEGY else ""
        print(f"  {name}{marker}\n      {desc}")


def cmd_dashboard(args):
    script = Path(__file__).parent / "dashboard.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(script)])


# --- argument parsing -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="broke_scalp", description="Trading212 scalping bot.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--instrument", default=None,
                        help="Instrument name/ticker to select in the watchlist (e.g. TSLA, GER40). "
                             "Omit to use whatever is open.")
        sp.add_argument("--cdp", default=CDP_URL, help="Remote debugging endpoint (default: %(default)s)")
        sp.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS,
                        help="Seconds between price reads (default: %(default)s)")
        sp.add_argument("--interval", type=float, default=None, metavar="MINUTES",
                        help="Bucket prices into N-minute recursive-mean windows.")
        sp.add_argument("--strategy", default=DEFAULT_STRATEGY, choices=strategies.available(),
                        help="Strategy to evaluate (default: %(default)s)")

    add_common(sub.add_parser("collect", help="Poll prices into the DB (paper-trades in the log)."))

    t = sub.add_parser("trade", help="Collect and act on signals via the broker.")
    add_common(t)
    t.add_argument("--size", type=int, default=DEFAULT_SIZE, choices=SIZES,
                   help="Position size quick-button %% (default: %(default)s)")
    t.add_argument("--live", action="store_true", help="Place real orders (otherwise dry-run).")

    v = sub.add_parser("view", help="Show or delete stored prices.")
    v.add_argument("--instrument", default=None)
    v.add_argument("--limit", type=int, default=100)
    v.add_argument("--delete", action="store_true")

    c = sub.add_parser("clear", help="Delete the entire contents of the price DB.")
    c.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    i = sub.add_parser("indicators", help="Print latest indicators and the strategy signal.")
    i.add_argument("--instrument", default=None)
    i.add_argument("--strategy", default=DEFAULT_STRATEGY, choices=strategies.available())

    sub.add_parser("strategies", help="List available strategies.")
    sub.add_parser("dashboard", help="Launch the Streamlit dashboard.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    {
        "collect": cmd_collect,
        "trade": cmd_trade,
        "view": cmd_view,
        "clear": cmd_clear,
        "indicators": cmd_indicators,
        "strategies": cmd_strategies,
        "dashboard": cmd_dashboard,
    }[args.command](args)
