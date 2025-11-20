import streamlit as st
import pandas as pd
import numpy as np
import requests
import time

# ---------------------------------------------------------
# BASIC CLEAN FUNCTION
# ---------------------------------------------------------
def clean(x):
    try:
        return int(str(x).replace(",", ""))
    except:
        return 0

# ---------------------------------------------------------
# FETCH OPTION CHAIN
# ---------------------------------------------------------
def fetch_option_chain(symbol):
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.nseindia.com/option-chain"
    }

    session = requests.Session()
    r = session.get(url, headers=headers)
    data = r.json()

    rows = []
    for item in data["records"]["data"]:
        if "CE" in item and "PE" in item:
            ce = item["CE"]
            pe = item["PE"]

            rows.append({
                "Strike": ce.get("strikePrice", 0),
                "CE_Volume": clean(ce.get("totalTradedVolume", 0)),
                "CE_OI_Change": clean(ce.get("changeinOpenInterest", 0)),
                "PE_Volume": clean(pe.get("totalTradedVolume", 0)),
                "PE_OI_Change": clean(pe.get("changeinOpenInterest", 0)),
            })

    return pd.DataFrame(rows)

# ---------------------------------------------------------
# TREND INTERPRETER
# ---------------------------------------------------------
def interpret(volume, oi_change):
    if oi_change > 0 and volume > 200000:
        return "🔥🔥🔥 Strong Long Build-up"
    if oi_change > 0 and volume > 50000:
        return "🔥🔥 Long Build-up"
    if oi_change > 0:
        return "🔥 Mild Long Build-up"
    if oi_change < 0 and volume > 50000:
        return "⚪ Short Covering"
    return "Neutral"

# ---------------------------------------------------------
# STREAMLIT PAGE
# ---------------------------------------------------------
st.set_page_config(page_title="Live Option Chain Dashboard", layout="wide")

st.title("📈 LIVE OPTION CHAIN ANALYZER — PREMIUM UI")
st.caption("Real-time Market Direction • Support/Resistance • PCR • Max Pain • Trend Score")

symbol = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY"])
refresh = st.sidebar.checkbox("Auto Refresh Every 30 sec", value=True)

st.sidebar.markdown("---")
st.sidebar.write("Built for: **Black Myth Wukong Fighter** 🐒⚔️")

placeholder = st.empty()

# ---------------------------------------------------------
# MAIN DASHBOARD FUNCTION
# ---------------------------------------------------------
def run_dashboard():
    with placeholder.container():
        st.info(f"Fetching LIVE data for **{symbol}**...")

        df = fetch_option_chain(symbol)
        if df.empty:
            st.error("Unable to fetch data from NSE. Try again.")
            return

        # Strength formula
        df["CE_Strength"] = df["CE_Volume"] * 0.6 + df["CE_OI_Change"].clip(lower=0) * 0.4
        df["PE_Strength"] = df["PE_Volume"] * 0.6 + df["PE_OI_Change"].clip(lower=0) * 0.4

        # Market metrics
        total_ce_oi = df["CE_OI_Change"].clip(lower=0).sum()
        total_pe_oi = df["PE_OI_Change"].clip(lower=0).sum()
        pcr = round(total_pe_oi / max(total_ce_oi, 1), 2)

        df["Total_OI"] = df["CE_OI_Change"].clip(lower=0) + df["PE_OI_Change"].clip(lower=0)
        max_pain = df.loc[df["Total_OI"].idxmax(), "Strike"]

        support = df.loc[df["PE_OI_Change"].idxmax(), "Strike"]
        resistance = df.loc[df["CE_OI_Change"].idxmax(), "Strike"]

        if pcr > 1.3:
            trend = "📈 Bullish"
        elif pcr < 0.8:
            trend = "📉 Bearish"
        else:
            trend = "➖ Neutral"

        # Summary metrics
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Trend", trend)
        col2.metric("PCR", pcr)
        col3.metric("Max Pain", int(max_pain))
        col4.metric("Support", int(support))
        col5.metric("Resistance", int(resistance))

        st.markdown("---")

        # TOP CE / PE
        top_ce = df.sort_values("CE_Strength", ascending=False).head(2)
        top_pe = df.sort_values("PE_Strength", ascending=False).head(2)

        ce_col, pe_col = st.columns(2)

        with ce_col:
            st.subheader("🔥 TOP CE BUYERS")
            for _, row in top_ce.iterrows():
                st.write(f"### {int(row['Strike'])} CE")
                st.write(f"Volume: **{row['CE_Volume']:,}**")
                st.write(f"OI Change: **{row['CE_OI_Change']:+,}**")
                st.write(interpret(row["CE_Volume"], row["CE_OI_Change"]))
                st.markdown("---")

        with pe_col:
            st.subheader("🟢 TOP PE BUYERS")
            for _, row in top_pe.iterrows():
                st.write(f"### {int(row['Strike'])} PE")
                st.write(f"Volume: **{row['PE_Volume']:,}**")
                st.write(f"OI Change: **{row['PE_OI_Change']:+,}**")
                st.write(interpret(row["PE_Volume"], row["PE_OI_Change"]))
                st.markdown("---")



        # ---------------------------------------------------------
        # HIGH PREMIUM BUYERS SECTION
        # ---------------------------------------------------------
        st.subheader("💰 High Premium Buyers (Exact Big Money)")

        # Detect high premium buyers = high volume + high OI change
        df["CE_Buy_Power"] = df["CE_Volume"] * 0.5 + df["CE_OI_Change"].clip(lower=0) * 0.5
        df["PE_Buy_Power"] = df["PE_Volume"] * 0.5 + df["PE_OI_Change"].clip(lower=0) * 0.5

        high_ce_buy = df.sort_values("CE_Buy_Power", ascending=False).head(1)
        high_pe_buy = df.sort_values("PE_Buy_Power", ascending=False).head(1)

        ce_hp, pe_hp = st.columns(2)

        with ce_hp:
            st.write("### 🔥 Highest Premium CE Buy")
            row = high_ce_buy.iloc[0]
            st.write(f"#### {int(row['Strike'])} CE")
            st.write(f"Volume: **{row['CE_Volume']:,}**")
            st.write(f"OI Change: **{row['CE_OI_Change']:+,}**")
            st.write("Type: **Aggressive Call Buying (Bullish)**")
            st.markdown("---")

        with pe_hp:
            st.write("### 🔥 Highest Premium PE Buy")
            row = high_pe_buy.iloc[0]
            st.write(f"#### {int(row['Strike'])} PE")
            st.write(f"Volume: **{row['PE_Volume']:,}**")
            st.write(f"OI Change: **{row['PE_OI_Change']:+,}**")
            st.write("Type: **Aggressive Put Buying (Bearish)**")
            st.markdown("---")

        # ---------------------------------------------------------
        # STRONG WRITERS BASED ON HEAVY MONEY POSITION
        # ---------------------------------------------------------
        st.subheader("🧱 Strong Writers Zone (Where big money sitting)")

        df["CE_Writer_Strength"] = df["CE_OI_Change"].apply(lambda x: abs(x) if x < 0 else 0)
        df["PE_Writer_Strength"] = df["PE_OI_Change"].apply(lambda x: abs(x) if x < 0 else 0)

        top_ce_writers = df.sort_values("CE_Writer_Strength", ascending=False).head(1)
        top_pe_writers = df.sort_values("PE_Writer_Strength", ascending=False).head(1)

        w1, w2 = st.columns(2)

        with w1:
            st.write("### 🧱 Strong CE Writer (Call Writer Wall)")
            row = top_ce_writers.iloc[0]
            st.write(f"#### {int(row['Strike'])} CE")
            st.write(f"OI Drop (Writing): **{row['CE_OI_Change']:,}**")
            st.write("Meaning: **Call Writing → Bearish Wall**")
            st.markdown("---")

        with w2:
            st.write("### 🧱 Strong PE Writer (Put Writer Wall)")
            row = top_pe_writers.iloc[0]
            st.write(f"#### {int(row['Strike'])} PE")
            st.write(f"OI Drop (Writing): **{row['PE_OI_Change']:,}**")
            st.write("Meaning: **Put Writing → Bullish Wall**")
            st.markdown("---")

        # ---------------------------------------------------------
        # FINAL MARKET TREND FROM NEW LOGIC
        # ---------------------------------------------------------
        st.subheader("📊 Final Market Direction (Based on Premium + Writers)")

        ce_power = high_ce_buy["CE_Buy_Power"].iloc[0]
        pe_power = high_pe_buy["PE_Buy_Power"].iloc[0]

        ce_write = top_ce_writers["CE_Writer_Strength"].iloc[0]
        pe_write = top_pe_writers["PE_Writer_Strength"].iloc[0]

        if ce_power > pe_power and pe_write > ce_write:
            final_trend = "📈 UP (Buyers Strong + Put Writers Strong)"
        elif pe_power > ce_power and ce_write > pe_write:
            final_trend = "📉 DOWN (Put Buyers Strong + Call Writers Strong)"
        else:
            final_trend = "➖ Sideways (Fight between writers & buyers)"

        st.success(f"### **{final_trend}**")


        # ---------------------------------------------------------
        # CE / PE WRITERS SECTION
        # ---------------------------------------------------------

        st.subheader("✍️ Option Writers (CE & PE Writers)")

        writer_ce = df[df["CE_OI_Change"] < 0].sort_values("CE_OI_Change").head(3)
        writer_pe = df[df["PE_OI_Change"] < 0].sort_values("PE_OI_Change").head(3)

        w_ce_col, w_pe_col = st.columns(2)

        with w_ce_col:
            st.write("### ✍️ Top CE Writers (Call Writers)")
            if writer_ce.empty:
                st.write("No CE writing detected.")
            else:
                for _, row in writer_ce.iterrows():
                    st.write(f"#### {int(row['Strike'])} CE")
                    st.write(f"Volume: **{row['CE_Volume']:,}**")
                    st.write(f"OI Change: **{row['CE_OI_Change']:,}**")
                    st.write("Type: **Call Writing (Bearish)**")
                    st.markdown("---")

        with w_pe_col:
            st.write("### ✍️ Top PE Writers (Put Writers)")
            if writer_pe.empty:
                st.write("No PE writing detected.")
            else:
                for _, row in writer_pe.iterrows():
                    st.write(f"#### {int(row['Strike'])} PE")
                    st.write(f"Volume: **{row['PE_Volume']:,}**")
                    st.write(f"OI Change: **{row['PE_OI_Change']:,}**")
                    st.write("Type: **Put Writing (Bullish)**")
                    st.markdown("---")




        # ---------------------------------------------------------
        # FIXED HEATMAP (NO MATPLOTLIB, NO DUPLICATES)
        # ---------------------------------------------------------
        st.subheader("🔥 Option Strength Heatmap")

        heat_df = df[["Strike", "CE_Strength", "PE_Strength"]].drop_duplicates("Strike").set_index("Strike")

        # Streamlit built-in style → NO ERRORS
        st.dataframe(
            heat_df.style.format("{:.0f}").background_gradient(axis=0)
        )

        st.markdown("---")
        st.caption("⚡ Data auto-refreshes every 30 seconds if enabled.")

# ---------------------------------------------------------
# RUN LOOP
# ---------------------------------------------------------
run_dashboard()

if refresh:
    while True:
        time.sleep(30)
        run_dashboard()

# python3.11 -m streamlit run live_option_chainStreamV3.py
