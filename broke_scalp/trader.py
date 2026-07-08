"""Trading212 client: attach to the live tab, read prices, select an instrument,
and place / close CFD orders. Also the Executor that turns a Signal into broker
actions.

Selectors below were captured and verified against the live app.trading212.com
DOM (CFD, practice mode). To support another broker, write a sibling client with
the same public methods (connect, read_price, select_instrument, current_instrument,
buy, sell, close, disconnect) — see CLAUDE.md.
"""

import asyncio
import re

from playwright.async_api import Page, async_playwright

from .config import CDP_URL, TRADING212_URL_MATCH, get_logger

logger = get_logger()

# --- verified selectors -----------------------------------------------------
OPEN_BUY = "[data-testid='trade-button-buy']"
OPEN_SELL = "[data-testid='trade-button-sell']"
SIZE_TMPL = "[data-testid='numpad-suggestion-{size}%']"
CONFIRM_BUY = "[data-testid='button-review-order']:has-text('Buy')"
CONFIRM_SELL = "[data-testid='button-review-order']:has-text('Sell')"
CLOSE_X = "[data-testid='trading-order-item-close-button']"
CONFIRM_CLOSE = "[data-testid='flyout-cfdCloseOpenPosition'] [data-testid='confirmation-button']"
PRICE_BID = "[data-testid='trade-button-sell']"          # SELL price = bid
TICKER = "[data-testid='instrument-title-ticker']"
HEADER_TITLE = "[data-testid='instrument-screen-header-title']"
POSITIONS_HEADER = "[data-testid='instrument-screen-section-header-positions']"

_SETTLE_MS = 600
# After closing a position the broker needs time to settle before a new ticket
# can be opened; opening immediately times out. Wait at least this long.
_POST_CLOSE_SECONDS = 5


def parse_number(text: str) -> float | None:
    """Parse a price string into a float, German ("1.234,56") or US ("1,234.56")."""
    s = re.sub(r"[^0-9.,-]", "", text or "")
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


class Trading212:
    def __init__(self, cdp_url: str = CDP_URL, dry_run: bool = True):
        self.cdp_url = cdp_url
        self.dry_run = dry_run
        self._pw = None
        self._browser = None
        self.page: Page | None = None

    # --- connection ---------------------------------------------------------

    async def connect(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        self.page = self._find_page()
        logger.info(
            f"Attached to Trading212 ({'DRY-RUN' if self.dry_run else 'LIVE'}): {self.page.url}"
        )

    def _find_page(self) -> Page:
        urls = []
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                urls.append(p.url)
                if TRADING212_URL_MATCH in p.url:
                    return p
        raise RuntimeError(
            f"No open Trading212 tab at {self.cdp_url}. Open tabs: {urls or '(none)'}"
        )

    async def disconnect(self) -> None:
        # Best-effort: either call can raise if the CDP connection already died,
        # and disconnect() must never mask the error that got us here.
        try:
            if self._browser is not None:
                await self._browser.close()   # CDP: detaches only; your Chrome stays open
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self.page = None

    async def ensure_page(self) -> bool:
        """True if the attached page is still a live Trading212 tab; otherwise
        try to re-find one in the same browser (handles closed/navigated tabs)."""
        try:
            if (self.page is not None and not self.page.is_closed()
                    and TRADING212_URL_MATCH in self.page.url):
                return True
        except Exception:
            pass
        logger.warning("Trading212 tab lost — trying to re-attach")
        try:
            self.page = self._find_page()
            logger.warning(f"re-attached to Trading212 tab: {self.page.url}")
            return True
        except Exception as exc:
            logger.error(f"re-attach failed: {exc}")
            return False

    async def reconnect(self) -> bool:
        """Full CDP reconnect, for when the browser connection itself dropped."""
        await self.disconnect()
        try:
            await self.connect()
            return True
        except Exception as exc:
            logger.error(f"reconnect failed: {exc}")
            return False

    # --- reading ------------------------------------------------------------

    async def read_price(self) -> float | None:
        try:
            text = await self.page.locator(PRICE_BID).first.inner_text(timeout=5000)
        except Exception as exc:
            logger.warning(f"price element not found ({PRICE_BID}): {exc}")
            return None
        price = parse_number(text)
        if price is None:
            logger.warning(f"price element text not parseable as a number: {text!r}")
        return price

    async def current_instrument(self) -> str:
        for sel in (TICKER, HEADER_TITLE):
            try:
                t = (await self.page.locator(sel).first.inner_text(timeout=2000)).strip()
                if t:
                    return t
            except Exception:
                continue
        return "UNKNOWN"

    async def positions_text(self) -> str:
        try:
            return (await self.page.locator(POSITIONS_HEADER).first.inner_text(timeout=2000)).strip()
        except Exception:
            return "none"

    async def has_position(self) -> bool:
        """True if the positions section header reports at least one open
        position ("1 position"). The header is absent when there are none."""
        m = re.search(r"\d+", await self.positions_text())
        return bool(m and int(m.group()) > 0)

    async def select_instrument(self, query: str) -> str:
        """Open the watchlist instrument whose tile text matches `query` (name or ticker)."""
        label = await self.page.evaluate(
            """(q) => {
                const tiles = [...document.querySelectorAll("[data-testid^='watchlist-instrument-tile']")];
                const t = tiles.find(el => (el.innerText||'').toLowerCase().includes(q.toLowerCase()));
                if (t) { t.click(); return (t.innerText||'').replace(/\\s+/g,' ').trim(); }
                return null;
            }""",
            query,
        )
        if not label:
            raise RuntimeError(f"No watchlist tile matching {query!r} — open/add it in Trading212 first.")
        await self.page.wait_for_timeout(1500)
        logger.info(f"Selected instrument matching {query!r}: {label[:50]}")
        return label

    # --- clicking (overlay-safe, auto-waiting) ------------------------------

    async def _click(self, selector: str, label: str, timeout: float = 8000) -> None:
        loc = self.page.locator(selector).first
        try:
            await loc.wait_for(state="visible", timeout=1000 if self.dry_run else timeout)
            matched = True
        except Exception:
            matched = False

        if self.dry_run:
            logger.info(
                f"[dry-run] would click {label} — {'MATCHED' if matched else 'NOT FOUND'}: {selector}"
            )
            return
        if not matched:
            raise RuntimeError(f"{label}: selector not found after {timeout:.0f}ms: {selector}")
        # Trading212 buttons are <div>s under a full-screen overlay that intercepts
        # pointer events; dispatching the DOM click bypasses it.
        await loc.evaluate("el => el.click()")
        logger.info(f"clicked {label}")

    # --- trading ------------------------------------------------------------

    async def buy(self, size: int = 25) -> None:
        await self._click(OPEN_BUY, "open buy ticket")
        await self._click(SIZE_TMPL.format(size=size), f"set size {size}%")
        await self.page.wait_for_timeout(_SETTLE_MS)
        await self._click(CONFIRM_BUY, "confirm buy")

    async def sell(self, size: int = 25) -> None:
        await self._click(OPEN_SELL, "open sell ticket")
        await self._click(SIZE_TMPL.format(size=size), f"set size {size}%")
        await self.page.wait_for_timeout(_SETTLE_MS)
        await self._click(CONFIRM_SELL, "confirm sell")

    async def close(self) -> None:
        await self._click(CLOSE_X, "close position (x)")
        await self._click(CONFIRM_CLOSE, "confirm close (Yes, close)")


# After confirming an order, wait this long before checking the positions
# header to verify the fill actually happened.
_VERIFY_SECONDS = 2


class Executor:
    """Maps a Signal to broker actions, holding the current position state.

    buy → target long, sell → target short, close → go flat, hold → keep.
    Opening the opposite direction closes the existing position first. Repeated
    same-direction (or close-while-flat) signals are no-ops, so a position is
    never opened or closed twice.

    `position` may also be "unknown": a position exists in the UI (adopted at
    startup or after a failed action) but its direction wasn't opened by us.
    The next buy/sell signal closes it before opening.
    """

    def __init__(self, client: Trading212, size: int = 25):
        self.client = client
        self.size = size
        self.position: str | None = None

    async def sync(self) -> None:
        """Align tracked state with what the broker UI actually shows.
        Call at startup and after a failed order action."""
        if self.client.dry_run:
            return
        if await self.client.has_position():
            if self.position not in ("long", "short"):
                self.position = "unknown"
                logger.warning(
                    "open position detected in the UI (direction unknown) — "
                    "the next buy/sell signal will close it first"
                )
        else:
            if self.position is not None:
                logger.warning(f"tracked position {self.position!r} is not in the UI — now flat")
            self.position = None

    async def apply(self, signal) -> None:
        if signal is None or signal.action == "hold":
            return
        if signal.action == "close":
            if self.position is not None:
                logger.info(f"Signal CLOSE (currently {self.position}): {signal.reason}")
                await self._close_current()
            return
        if signal.action not in ("buy", "sell"):
            # Never guess: an unexpected action must not silently become a trade.
            logger.error(f"ignoring signal with unexpected action {signal.action!r}")
            return
        target = "long" if signal.action == "buy" else "short"
        if target == self.position:
            return

        logger.info(
            f"Signal {signal.action.upper()} -> target {target} "
            f"(currently {self.position or 'flat'}): {signal.reason}"
        )
        if self.position is not None:
            await self._close_current()
        if target == "long":
            await self.client.buy(self.size)
        else:
            await self.client.sell(self.size)
        if not self.client.dry_run:
            await asyncio.sleep(_VERIFY_SECONDS)
            if not await self.client.has_position():
                logger.error(
                    "order was confirmed but no open position is visible — it was "
                    "likely rejected (market closed? insufficient funds?); staying flat"
                )
                self.position = None
                return
        self.position = target

    async def _close_current(self) -> None:
        # The tracked position may be stale (e.g. stopped out by the broker, or
        # "unknown" adopted at startup) — only click close if the UI shows one.
        if not self.client.dry_run and not await self.client.has_position():
            logger.warning(f"tracked position {self.position!r} not present in the UI — treating as flat")
            self.position = None
            return
        await self.client.close()
        self.position = None
        # Give the broker time to settle the close before opening a new
        # position — opening immediately after a close times out.
        if not self.client.dry_run:
            logger.info(f"closed; waiting {_POST_CLOSE_SECONDS}s for the close to settle")
            await asyncio.sleep(_POST_CLOSE_SECONDS)
            if await self.client.has_position():
                self.position = "unknown"
                raise RuntimeError("close was confirmed but a position is still open — aborting signal")
