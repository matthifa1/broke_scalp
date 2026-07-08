"""Central configuration and shared logger for broke_scalp."""

import logging
import sys
from pathlib import Path

# Project root is the folder that *contains* the broke_scalp package.
ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "broke_scalp.db"
LOG_PATH = ROOT / "trading.log"

# --- Trading212 / browser ---------------------------------------------------
# Attach to a Chrome started with:  chrome.exe --remote-debugging-port=9222
CDP_URL = "http://localhost:9222"
TRADING212_URL_MATCH = "trading212.com"

# --- defaults (overridable on the CLI) --------------------------------------
DEFAULT_INSTRUMENT = None        # None = trade whatever instrument is open
DEFAULT_SIZE = 25                # quick-size button: 25 | 50 | 75 | 100 (%)
DEFAULT_POLL_SECONDS = 10        # one price stored per poll → data resolution
DEFAULT_STRATEGY = "macd_triple"


def get_logger() -> logging.Logger:
    """Logger writing to the console (UTF-8 safe) and trading.log."""
    logger = logging.getLogger("broke_scalp")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Windows consoles default to cp1252 and choke on non-ASCII (e.g. German
    # broker labels). Make stdout tolerant so logging never crashes.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
