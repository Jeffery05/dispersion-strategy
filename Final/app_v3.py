import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px

# ---------- LOAD & PREP DATA ----------

@st.cache_data
def load_data():
    pos = pd.read_csv("positions_log.csv")

    pos["date"] = pd.to_datetime(pos["date"])

    # Market value (unsigned notional)
    pos["mv"] = pos["price_today"] * pos["quantity"]

    return pos.sort_values(["date", "ticker"])


pos = load_data()

# unique dates (date only, no time)
all_dates = pos["date"].dt.date.unique()
min_date = all_dates[0]
max_date = all_dates[-1]

# ---------- PAGE CONFIG ----------

st.set_page_config(
    page_title="Dispersion Strategy Dashboard",
    layout="wide",
)

st.title("Dispersion Strategy Dashboard")
st.caption("Interactive view of PnL, exposures, and positions over time")

# ---------- DATE RANGE SLIDER ----------

st.markdown("### Backtest Period")

hdr_left, hdr_right = st.columns([3, 1])
with hdr_left:
    date_range = st.slider(
        "PnL date range",
        min_value=min_date,
        max_value=max_date,
        value=(min_date, max_date),
        label_visibility="visible",
    )

# Filter PnL & positions by date range (for views)
mask_pos_view = (pos["date"].dt.date >= date_range[0]) & (pos["date"].dt.date <= date_range[1])
pos_view = pos.loc[mask_pos_view].copy()


# ---------- TRUE MTM PnL FROM positions_log (TOTAL / LONG / SHORT) ----------

pos_all = pos.copy()

# Price per date/ticker
price = pos_all.groupby(["date", "ticker"])["price_today"].first()

# Long and short quantities per date/ticker
long_qty = (
    pos_all[pos_all["type"] == "long"]
    .groupby(["date", "ticker"])["quantity"]
    .sum()
)

short_qty = (
    pos_all[pos_all["type"] == "short"]
    .groupby(["date", "ticker"])["quantity"]
    .sum()
)

# Combine into one frame
daily_positions = pd.concat([price, long_qty, short_qty], axis=1).fillna(0.0)
daily_positions.columns = ["price_today", "long_qty", "short_qty"]
daily_positions = daily_positions.reset_index()

# Sort so we can shift per ticker
daily_positions = daily_positions.sort_values(["ticker", "date"])

# Previous day's quantities and price per ticker
daily_positions["prev_long_qty"] = daily_positions.groupby("ticker")["long_qty"].shift(1)
daily_positions["prev_short_qty"] = daily_positions.groupby("ticker")["short_qty"].shift(1)
daily_positions["prev_price"] = daily_positions.groupby("ticker")["price_today"].shift(1)

# Price change
daily_positions["dP"] = daily_positions["price_today"] - daily_positions["prev_price"]

# PnL formulas:
#   Long leg:  prev_long_qty * dP
#   Short leg: -prev_short_qty * dP
daily_positions["long_pnl"] = daily_positions["prev_long_qty"] * daily_positions["dP"]
daily_positions["short_pnl"] = -daily_positions["prev_short_qty"] * daily_positions["dP"]

# For first day each ticker appears → PnL = 0
mask_first = daily_positions["prev_price"].isna()
daily_positions.loc[mask_first, ["long_pnl", "short_pnl"]] = 0.0

# Total PnL per ticker/day
daily_positions["total_pnl"] = daily_positions["long_pnl"] + daily_positions["short_pnl"]

# Aggregate to portfolio level (full history)
daily_long_pnl_all = daily_positions.groupby("date")["long_pnl"].sum().sort_index()
daily_short_pnl_all = daily_positions.groupby("date")["short_pnl"].sum().sort_index()
daily_total_pnl_all = daily_positions.groupby("date")["total_pnl"].sum().sort_index()

# Initial gross notionals on the first backtest date
first_date_all = daily_total_pnl_all.index[0]
first_day_all = daily_positions[daily_positions["date"] == first_date_all]

long_initial_cap_all = (first_day_all["long_qty"] * first_day_all["price_today"]).sum()
short_initial_cap_all = (first_day_all["short_qty"] * first_day_all["price_today"]).sum()
capital_base_all = long_initial_cap_all + short_initial_cap_all

# Global equity curve (entire backtest)
equity_all = capital_base_all + daily_total_pnl_all.cumsum()

# ---------- NET DELTA / VEGA OVER TIME + HEDGE FLAGS ----------

pos_greeks = pos_all.copy()
pos_greeks["sign"] = np.where(pos_greeks["type"] == "short", -1.0, 1.0)
pos_greeks["qty_signed"] = pos_greeks["quantity"] * pos_greeks["sign"]
pos_greeks["delta_exposure"] = pos_greeks["delta"] * pos_greeks["qty_signed"]
pos_greeks["vega_exposure"] = pos_greeks["vega"] * pos_greeks["qty_signed"]

net_greeks = pos_greeks.groupby("date").agg(
    net_delta=("delta_exposure", "sum"),
    net_vega=("vega_exposure", "sum"),
).sort_index()

# Delta hedge: any change in signed SPY_HEDGE_STOCK quantity
spy_hedge = (
    pos_greeks[pos_greeks["ticker"] == "SPY_HEDGE_STOCK"]
    .groupby("date")["qty_signed"]
    .sum()
    .sort_index()
)
delta_hedge_flags = spy_hedge.ne(spy_hedge.shift()).fillna(False)

# Vega hedge: total SPY option signed quantity increases
spy_opts = pos_greeks[(pos_greeks["underlying"] == "SPY") & (pos_greeks["ticker"] != "SPY_HEDGE_STOCK")]
if not spy_opts.empty:
    total_spy_qty = spy_opts.groupby("date")["qty_signed"].sum().sort_index()
    spy_qty_diff = total_spy_qty.diff()
    vega_hedge_flags = (spy_qty_diff > 1e-8).fillna(False)
else:
    vega_hedge_flags = pd.Series(False, index=net_greeks.index)

net_greeks_df = net_greeks.copy()
net_greeks_df["delta_hedge"] = delta_hedge_flags.reindex(net_greeks_df.index, fill_value=False)
net_greeks_df["vega_hedge"] = vega_hedge_flags.reindex(net_greeks_df.index, fill_value=False)

# ----- Restrict metrics to selected window -----

mask_window = (
    (equity_all.index.date >= date_range[0]) &
    (equity_all.index.date <= date_range[1])
)

equity_window = equity_all.loc[mask_window]
daily_total_pnl = daily_total_pnl_all.loc[mask_window]
daily_long_pnl = daily_long_pnl_all.loc[mask_window]
daily_short_pnl = daily_short_pnl_all.loc[mask_window]

net_greeks_window = net_greeks_df.loc[mask_window]

if equity_window.empty:
    st.error("No equity data in selected date range (check positions_log).")
    st.stop()

# Rebase equity at start of window
equity_rebased = equity_window / equity_window.iloc[0]

# Daily returns for performance stats
daily_ret = equity_rebased.pct_change().dropna()

# 3-month normalized Sharpe (~63 trading days)
if len(daily_ret) > 1 and daily_ret.std() != 0:
    sharpe_3m = daily_ret.mean() / daily_ret.std() * np.sqrt(252)
else:
    sharpe_3m = np.nan

# Total return over selected window
total_return_pct = (equity_rebased.iloc[-1] - 1.0) * 100.0

# Max drawdown over selected window
rolling_max = equity_rebased.cummax()
dd = (equity_rebased / rolling_max) - 1.0
max_dd = dd.min() if not dd.empty else np.nan

# Starting equity in window (for % scaling)
equity0_window = equity_window.iloc[0]

# Cumulative PnL by leg ($) and as % of starting equity
cum_total_pnl = daily_total_pnl.cumsum()
cum_long_pnl = daily_long_pnl.cumsum()
cum_short_pnl = daily_short_pnl.cumsum()

cum_total_pnl_pct_leg = cum_total_pnl / equity0_window * 100.0
cum_long_pnl_pct_leg = cum_long_pnl / equity0_window * 100.0
cum_short_pnl_pct_leg = cum_short_pnl / equity0_window * 100.0

# ---------- SUMMARY METRICS (OVER CURRENT RANGE, AT TOP) ----------

st.markdown("#### Summary Metrics (over selected range)")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Return (MTM)", f"{total_return_pct:0.2f}%")
col2.metric(
    "Sharpe (3-month normalized)",
    f"{sharpe_3m:0.2f}" if not np.isnan(sharpe_3m) else "N/A",
)
col3.metric(
    "Max Drawdown (MTM)",
    f"{max_dd*100:0.2f}%" if not np.isnan(max_dd) else "N/A",
)
col4.metric(
    "Backtest Period",
    f"{pos_view['date'].min().date()} → {pos_view['date'].max().date()}"
)

# ---------- DAILY MTM PNL IN % (TOP CHART, TOTAL / LONG / SHORT) ----------

st.markdown("### Daily PnL (MTM, %, Total vs Long vs Short)")

daily_pnl_df = pd.DataFrame({
    "date": daily_total_pnl.index,
    "total_pnl_pct": daily_total_pnl.values / equity0_window * 100.0,
    "long_pnl_pct": daily_long_pnl.reindex(daily_total_pnl.index, fill_value=0).values / equity0_window * 100.0,
    "short_pnl_pct": daily_short_pnl.reindex(daily_total_pnl.index, fill_value=0).values / equity0_window * 100.0,
})

daily_pnl_long = daily_pnl_df.melt(
    id_vars="date",
    value_vars=["total_pnl_pct", "long_pnl_pct", "short_pnl_pct"],
    var_name="Leg",
    value_name="PnL_pct",
)

fig_mtm = px.line(
    daily_pnl_long,
    x="date",
    y="PnL_pct",
    color="Leg",
    labels={"date": "Date", "PnL_pct": "Daily PnL (%)", "Leg": "Leg"},
)
fig_mtm.update_traces(mode="lines+markers")
fig_mtm.update_layout(
    hovermode="x unified",
    margin=dict(l=10, r=10, t=30, b=10),
)
st.plotly_chart(fig_mtm, use_container_width=True)

# ---------- CUMULATIVE PNL (% OF STARTING EQUITY, CLICKABLE) ----------

st.markdown("### Cumulative PnL (% of starting equity, Total vs Long vs Short)")

cum_pnl_pct_df = pd.DataFrame({
    "date": cum_total_pnl_pct_leg.index,
    "total_cum_pnl_pct": cum_total_pnl_pct_leg.values,
    "long_cum_pnl_pct": cum_long_pnl_pct_leg.reindex(cum_total_pnl_pct_leg.index, fill_value=0).values,
    "short_cum_pnl_pct": cum_short_pnl_pct_leg.reindex(cum_total_pnl_pct_leg.index, fill_value=0).values,
})

cum_pnl_pct_long = cum_pnl_pct_df.melt(
    id_vars="date",
    value_vars=["total_cum_pnl_pct", "long_cum_pnl_pct", "short_cum_pnl_pct"],
    var_name="Leg",
    value_name="CumPnL_pct",
)

fig_cum = px.line(
    cum_pnl_pct_long,
    x="date",
    y="CumPnL_pct",
    color="Leg",
    labels={"date": "Date", "CumPnL_pct": "Cumulative PnL (%)", "Leg": "Leg"},
)
fig_cum.update_traces(mode="lines+markers")
fig_cum.update_layout(
    hovermode="x unified",
    margin=dict(l=10, r=10, t=30, b=10),
)

# This chart is clickable to choose inspect_date
event = st.plotly_chart(
    fig_cum,
    use_container_width=True,
    key="cum_pnl_chart",
    on_select="rerun",
)

# ---------- HANDLE CLICKED DATE FOR POSITIONS ----------

available_dates = equity_window.index.date
inspect_date = available_dates[-1]  # default: last date in current window

if event and "selection" in event and event["selection"]["points"]:
    pt = event["selection"]["points"][0]
    clicked_date = pd.to_datetime(pt["x"]).date()
    inspect_date = min(
        available_dates,
        key=lambda d: abs(pd.to_datetime(d) - pd.to_datetime(clicked_date)),
    )

st.markdown(
    f"**Selected date for positions:** {inspect_date} "
    "(click a point on the cumulative PnL chart to change)"
)

st.markdown("---")

# ---------- DAILY CHANGES (VALUE, DELTA, VEGA) FOR SELECTED DATE ----------

st.markdown(f"### Daily Changes for {inspect_date}")

pos_dates = sorted(pos["date"].dt.date.unique())
if inspect_date not in pos_dates or pos_dates.index(inspect_date) == 0:
    st.info("Cannot compute daily changes for this date (no previous day).")
else:
    idx = pos_dates.index(inspect_date)
    prev_date = pos_dates[idx - 1]

    today = pos[pos["date"].dt.date == inspect_date].copy()
    prev = pos[pos["date"].dt.date == prev_date].copy()

    # Signed greeks & quantity: long = +, short = -
    for df in (today, prev):
        df["sign"] = np.where(df["type"] == "short", -1.0, 1.0)
        df["qty_signed"] = df["quantity"] * df["sign"]
        df["delta_signed"] = df["delta"] * df["sign"]
        df["vega_signed"] = df["vega"] * df["sign"]
        df["mv_signed"] = df["price_today"] * df["qty_signed"]

    # Aggregate by ticker using SIGNED quantities (true net view)
    today_agg = today.groupby("ticker").agg(
        underlying_tdy=("underlying", "last"),
        qty_tdy=("qty_signed", "sum"),
        price_today_tdy=("price_today", "last"),
        delta_tdy=("delta_signed", "sum"),
        vega_tdy=("vega_signed", "sum"),
        value_today=("mv_signed", "sum"),
    )

    prev_agg = prev.groupby("ticker").agg(
        qty_prv=("qty_signed", "sum"),
        price_today_prv=("price_today", "last"),
        delta_prv=("delta_signed", "sum"),
        vega_prv=("vega_signed", "sum"),
        value_prev=("mv_signed", "sum"),
    )

    merged = today_agg.join(prev_agg, how="left")

    # Fill prev-day NaNs sensibly
    merged["qty_prv"] = merged["qty_prv"].fillna(0)
    merged["price_today_prv"] = merged["price_today_prv"].fillna(merged["price_today_tdy"])
    merged["delta_prv"] = merged["delta_prv"].fillna(0)
    merged["vega_prv"] = merged["vega_prv"].fillna(0)
    merged["value_prev"] = merged["value_prev"].fillna(0)

    # Compute changes
    merged["price_change"] = merged["price_today_tdy"] - merged["price_today_prv"]
    merged["quantity_change"] = merged["qty_tdy"] - merged["qty_prv"]
    merged["delta_change"] = merged["delta_tdy"] - merged["delta_prv"]
    merged["vega_change"] = merged["vega_tdy"] - merged["vega_prv"]
    merged["value_change"] = merged["qty_prv"] * merged["price_change"]

    # Derive a net 'type' label from the SIGNED quantity
    def classify_net_type(q):
        if q > 0:
            return "net_long"
        elif q < 0:
            return "net_short"
        else:
            return "flat"

    merged["net_type_tdy"] = merged["qty_tdy"].apply(classify_net_type)

    merged = merged.reset_index().rename(columns={"ticker": "Ticker"})

    # Sort tickers by biggest positive → biggest negative value_change
    merged_sorted = merged.sort_values("value_change", ascending=False)

    # ---------- "CHANGES" CHART (PER TICKER, ORDERED BY value_change) ----------

    changes_for_chart = merged_sorted[[
        "Ticker",
        "price_change",
        "quantity_change",
        "delta_change",
        "vega_change",
        "value_change",
    ]].copy()

    # Rename delta_change for chart legend only
    changes_for_chart = changes_for_chart.rename(columns={"delta_change": "Δ delta"})

    changes_long = changes_for_chart.melt(
        id_vars="Ticker",
        value_vars=["price_change", "quantity_change", "Δ delta", "vega_change", "value_change"],
        var_name="Metric",
        value_name="Change",
    )


    # ---------- TABLE: CHANGES + NET QUANTITY, WITH GREEN/RED/GREY HIGHLIGHT ----------

    show_cols = [
        "Ticker",
        "underlying_tdy",
        "net_type_tdy",
        "qty_tdy",            # net signed quantity (long +, short -)
        "price_change",
        "quantity_change",
        "delta_change",
        "vega_change",
        "value_change",
    ]

    merged_display = merged_sorted[show_cols].copy()
    merged_display = merged_display.rename(columns={"qty_tdy": "net_qty_tdy"})

    def color_change(val):
        try:
            if val > 0:
                return "background-color: rgba(0, 200, 0, 0.18); color: green;"
            elif val < 0:
                return "background-color: rgba(255, 0, 0, 0.15); color: red;"
            elif val == 0:
                return "background-color: rgba(128, 128, 128, 0.15); color: gray;"
        except Exception:
            pass
        return ""

    change_cols = [
        "price_change",
        "quantity_change",
        "delta_change",
        "vega_change",
        "value_change",
    ]

    styled = merged_display.style.applymap(color_change, subset=change_cols)

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
    )

st.markdown("---")

# ---------- POSITIONS TABLE (STATIC SNAPSHOT, NO GROUPBY) ----------

st.markdown(f"### Positions on {inspect_date}")

pos_today = pos[pos["date"].dt.date == inspect_date].copy()

if pos_today.empty:
    st.info("No positions to show for this date.")
else:
    # Signed greeks for display
    pos_today = pos_today.copy()
    pos_today["sign"] = np.where(pos_today["type"] == "short", -1.0, 1.0)
    pos_today["delta_signed"] = pos_today["delta"] * pos_today["sign"]
    pos_today["vega_signed"] = pos_today["vega"] * pos_today["sign"]

    # Overwrite delta/vega columns with signed versions for display
    pos_today["delta"] = pos_today["delta_signed"]
    pos_today["vega"] = pos_today["vega_signed"]

    display_cols = [
        "ticker",
        "underlying",
        "type",
        "quantity",
        "price_today",
        "delta",
        "vega",
        "mv",
    ]
    st.dataframe(
        pos_today[display_cols].sort_values("underlying"),
        use_container_width=True,
        hide_index=True,
    )

# ---------- NET DELTA / VEGA OVER TIME (WITH HEDGE MARKERS) ----------

st.markdown("---")
st.markdown("### Net Exposures Over Time")

if not net_greeks_window.empty:
    net_g_df = net_greeks_window.reset_index().rename(columns={"index": "date"})

    # Net Delta (no hedge markers)
    st.markdown("#### Net Delta over time")
    fig_delta = px.line(
        net_g_df,
        x="date",
        y="net_delta",
        labels={"date": "Date", "net_delta": "Net Delta"},
    )

    fig_delta.update_layout(
        hovermode="x unified",
        margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig_delta, use_container_width=True)

    # Net Vega
    st.markdown("#### Net Vega over time")
    fig_vega_ts = px.line(
        net_g_df,
        x="date",
        y="net_vega",
        labels={"date": "Date", "net_vega": "Net Vega"},
    )

    fig_vega_ts.update_layout(
        hovermode="x unified",
        margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig_vega_ts, use_container_width=True)
