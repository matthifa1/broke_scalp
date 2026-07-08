"""Streamlit dashboard. Launched via `python -m broke_scalp dashboard`
(which runs `streamlit run` on this file)."""

import sys
from pathlib import Path

# Streamlit runs this as a script, so make the package importable.
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import altair as alt
import pandas as pd
import streamlit as st

from broke_scalp import indicators as ind
from broke_scalp import strategies
from broke_scalp.config import DEFAULT_STRATEGY
from broke_scalp.db import init_db, instruments, load_prices

st.set_page_config(page_title="broke_scalp", layout="wide")
init_db()


@st.cache_data(ttl=15)
def _load(instrument):
    return load_prices(instrument)


st.title("broke_scalp dashboard")

insts = instruments()
if not insts:
    st.warning("No data yet. Run `python -m broke_scalp collect` first.")
    st.stop()

col1, col2, col3 = st.columns([2, 2, 1])
instrument = col1.selectbox("Instrument", insts)
strategy_name = col2.selectbox("Strategy", strategies.available(),
                               index=strategies.available().index(DEFAULT_STRATEGY))
if col3.button("Refresh"):
    st.cache_data.clear()

df = _load(instrument)
prices = pd.to_numeric(df["price"], errors="coerce").dropna().reset_index(drop=True)
st.caption(f"{len(df)} rows for {instrument}")

# --- Signal -----------------------------------------------------------------
st.subheader("Signal")
sig = strategies.generate(strategy_name, prices)
if sig is None:
    st.info("Not enough data for this strategy yet.")
else:
    colour = {"buy": "green", "sell": "red", "hold": "orange", "close": "gray"}[sig.action]
    icon = {"buy": "🟢", "sell": "🔴", "hold": "🟡", "close": "⚪"}[sig.action]
    st.markdown(f"<h2 style='color:{colour}'>{icon} {sig.action.upper()}</h2>",
                unsafe_allow_html=True)
    st.caption(sig.reason)

# --- Price chart (y-axis bounded to the data) -------------------------------
st.subheader("Price history")
# Build from df directly — pairing the reset-index `prices` with the original-
# index timestamps would misalign rows as soon as one price fails to parse.
chart_df = pd.DataFrame({
    "time": pd.to_datetime(df["fetched_at"], errors="coerce"),
    "price": pd.to_numeric(df["price"], errors="coerce"),
}).dropna()
if not chart_df.empty:
    chart = (
        alt.Chart(chart_df).mark_line().encode(
            x=alt.X("time:T", title="Time"),
            y=alt.Y("price:Q", title="Price",
                    scale=alt.Scale(domain=[chart_df["price"].min(), chart_df["price"].max()])),
        ).properties(height=320)
    )
    st.altair_chart(chart, use_container_width=True)

# --- Indicators -------------------------------------------------------------
st.subheader("Latest indicators")
c1, c2, c3 = st.columns(3)
r, m, b = ind.rsi(prices), ind.macd(prices), ind.bollinger(prices)
c1.metric("RSI(14)", f"{r.iloc[-1]:.2f}" if not r.empty else "—")
if not m.empty:
    c2.metric("MACD", f"{m.macd.iloc[-1]:.4f}")
    c2.caption(f"signal {m.signal.iloc[-1]:.4f} · hist {m.histogram.iloc[-1]:.4f}")
else:
    c2.metric("MACD", "—")
if not b.empty:
    c3.metric("Bollinger mid", f"{b.middle.iloc[-1]:.4f}")
    c3.caption(f"U {b.upper.iloc[-1]:.4f} · L {b.lower.iloc[-1]:.4f}")
else:
    c3.metric("Bollinger", "—")

# --- Raw data ---------------------------------------------------------------
st.subheader("Raw data")
st.dataframe(df.drop(columns=["id"], errors="ignore"), use_container_width=True)
