"""Virtual (paper) position tracker.

Mirrors the real executor's state machine but only logs hypothetical trades and
their P&L — used during collection and dry-run so you can judge a strategy
without placing real orders. Logs go to the console and trading.log.
"""

from .config import get_logger

logger = get_logger()


class SignalTracker:
    def __init__(self):
        self._last_signal: str | None = None
        self._entry: float | None = None
        self._side: str | None = None      # "long" | "short"

    def update(self, signal, price: float) -> None:
        if signal is None:
            return
        action = signal.action

        if action != self._last_signal:
            logger.info(
                f"[paper] signal {(self._last_signal or 'none').upper()} -> "
                f"{action.upper()} @ {price:.4f} : {signal.reason}"
            )
            self._last_signal = action

        if action == "hold":
            return
        if action == "close":
            if self._side is not None:
                self._close(price)
            return
        target = "long" if action == "buy" else "short"
        if target == self._side:
            return

        # close opposite side and report P&L
        if self._side is not None:
            self._close(price)

        self._side = target
        self._entry = price
        logger.info(f"[paper] OPENED {target.upper()} @ {price:.4f}")

    def _close(self, price: float) -> None:
        pnl = (price - self._entry) if self._side == "long" else (self._entry - price)
        pct = (pnl / self._entry * 100) if self._entry else 0.0
        logger.info(
            f"[paper] CLOSED {self._side.upper()} @ {price:.4f} "
            f"(entry {self._entry:.4f}) profit={pnl:+.4f} ({pct:+.2f}%)"
        )
        self._side = None
        self._entry = None
