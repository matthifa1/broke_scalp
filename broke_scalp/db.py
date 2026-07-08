"""SQLite storage for collected prices.

Schema is simple and typed because we now control the data: one float price per
poll, tagged with the instrument it belongs to.

    prices(id, fetched_at TEXT, instrument TEXT, price REAL)
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from .config import DB_PATH


@contextmanager
def _connect():
    """Connection that commits on success and is always closed (sqlite3's own
    context manager only commits — it leaves the connection open)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prices (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL,
                instrument TEXT NOT NULL,
                price      REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instrument_time ON prices(instrument, fetched_at)"
        )


def save_price(price: float, instrument: str, fetched_at: str | None = None) -> str:
    ts = fetched_at or datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO prices (fetched_at, instrument, price) VALUES (?, ?, ?)",
            (ts, instrument, float(price)),
        )
    return ts


def last_row(instrument: str):
    """Return (id, fetched_at, price) of the newest row for an instrument, or None."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, fetched_at, price FROM prices WHERE instrument = ? "
            "ORDER BY id DESC LIMIT 1",
            (instrument,),
        ).fetchone()


def update_price(row_id: int, price: float) -> None:
    with _connect() as conn:
        conn.execute("UPDATE prices SET price = ? WHERE id = ?", (float(price), row_id))


def load_prices(instrument: str | None = None, limit: int | None = None) -> pd.DataFrame:
    query = "SELECT id, fetched_at, instrument, price FROM prices"
    params: list = []
    if instrument:
        query += " WHERE instrument = ?"
        params.append(instrument)
    query += " ORDER BY fetched_at ASC, id ASC"
    with _connect() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    if limit:
        df = df.tail(limit).reset_index(drop=True)
    return df


def price_series(instrument: str | None = None) -> pd.Series:
    """Just the price column as a float Series (oldest → newest)."""
    df = load_prices(instrument)
    if df.empty:
        return pd.Series(dtype=float)
    return pd.to_numeric(df["price"], errors="coerce").dropna().reset_index(drop=True)


def instruments() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT instrument FROM prices ORDER BY instrument").fetchall()
    return [r[0] for r in rows]


def delete_prices(instrument: str | None = None) -> int:
    with _connect() as conn:
        if instrument:
            conn.execute("DELETE FROM prices WHERE instrument = ?", (instrument,))
        else:
            conn.execute("DELETE FROM prices")
        return conn.execute("SELECT changes()").fetchone()[0]
