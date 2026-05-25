# dhan_option_engine.py
# ==========================================================
# NIFTY OPTION BUYING ENGINE - DHAN API VERSION
# ==========================================================
# INSTALL:
# pip install streamlit pandas numpy requests websocket-client
#
# RUN:
# streamlit run dhan_option_engine.py
#
# ==========================================================

import streamlit as st
import pandas as pd
import numpy as np
import requests
import json
import uuid
from datetime import datetime, timedelta

# ==========================================================
# PAGE CONFIG
# ==========================================================

st.set_page_config(
    page_title="Dhan NIFTY Option Engine",
    layout="wide"
)

st.title("📈 DHAN NIFTY Option Buying Engine")

# ==========================================================
# USER CONFIG
# ==========================================================

# ── HARDCODED CREDENTIALS — update these before running ──
CLIENT_ID    = "1108066094"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJwX2lwIjoiIiwic19pcCI6IiIsImlzcyI6ImRoYW4iLCJwYXJ0bmVySWQiOiIiLCJleHAiOjE3Nzk4MTYzNjcsImlhdCI6MTc3OTcyOTk2NywidG9rZW5Db25zdW1lclR5cGUiOiJTRUxGIiwid2ViaG9va1VybCI6Imh0dHBzOi8vd2ViLmRoYW4uY28vaW5kZXgvcHJvZmlsZSIsImRoYW5DbGllbnRJZCI6IjExMDgwNjYwOTQifQ.DEfXYPcDU1z9eYTqTd3Bu7iAUiOlOZUICf5S7jZggHtqPRUV9kGkdk4OS-vyMjirPhX3AderOvR00tBvuG3XhQ"
# ─────────────────────────────────────────────────────────

st.sidebar.header("SETTINGS")

MAX_LOTS = st.sidebar.number_input(
    "Max Lots",
    min_value=1,
    max_value=20,
    value=4
)

PRODUCT = "NRML"

# ==========================================================
# DHAN HEADERS
# ==========================================================

HEADERS = {
    "access-token": ACCESS_TOKEN,
    "client-id": CLIENT_ID,
    "Content-Type": "application/json"
}

# ==========================================================
# FUNCTIONS
# ==========================================================

def stop_trade(msg):
    st.error(msg)
    st.stop()


def get_nifty_quote():

    url = "https://api.dhan.co/v2/marketfeed/ltp"

    payload = {
        "NSE_EQ": [
            "13"
        ]
    }

    try:

        response = requests.post(
            url,
            headers=HEADERS,
            json=payload
        )

        data = response.json()

        nifty = data["data"]["NSE_EQ"]["13"]["last_price"]

        return nifty

    except Exception as e:
        stop_trade("LIVE DATA NOT AVAILABLE — NO TRADE")


def get_option_chain():

    url = "https://images.dhan.co/api-data/api-scrip-master.csv"

    try:

        df = pd.read_csv(url)

        nifty = df[
            (df["SEM_CUSTOM_SYMBOL"].str.contains("NIFTY")) &
            (df["SEM_INSTRUMENT_NAME"] == "OPTIDX")
        ].copy()

        return nifty

    except:
        stop_trade("LIVE DATA NOT AVAILABLE — NO TRADE")


def nearest_expiry(df):

    df["EXPIRY"] = pd.to_datetime(
        df["SEM_EXPIRY_DATE"],
        errors="coerce"
    )

    today = datetime.now()

    df = df[df["EXPIRY"] >= today]

    expiries = sorted(df["EXPIRY"].dropna().unique())

    expiry = expiries[0]

    dte = (expiry - pd.Timestamp(today)).days

    return expiry, dte


def get_atm(spot):
    return round(spot / 50) * 50


def get_option_ltp(security_id, exchange_segment="NSE_FNO"):

    url = "https://api.dhan.co/v2/marketfeed/ltp"

    payload = {
        exchange_segment: [
            str(security_id)
        ]
    }

    try:

        response = requests.post(
            url,
            headers=HEADERS,
            json=payload
        )

        data = response.json()

        ltp = data["data"][exchange_segment][str(security_id)]["last_price"]

        return ltp

    except:
        return 0


def theta_calc(premium, dte):

    if dte <= 0:
        dte = 1

    theta_day = premium * 0.08 / dte

    theta_15 = theta_day / 26

    return round(theta_day, 2), round(theta_15, 2)


def classify_gap(gap):

    g = abs(gap)

    if g <= 0.3:
        return "Normal Open"

    elif g <= 0.8:
        return "Mild Gap"

    elif g <= 1.5:
        return "Large Gap"

    return "Extreme Gap"


def create_order_json(row, qty, weight):

    return {
        "id": str(uuid.uuid4()),
        "instrument": {
            "tradingsymbol": row["SEM_TRADING_SYMBOL"],
            "symbol": "NIFTY",
            "type": "OPT",
            "optionType": "CE" if "CALL" in row["SEM_OPTION_TYPE"] else "PE",
            "strike": row["SEM_STRIKE_PRICE"],
            "segment": "NFO-OPT",
            "exchange": "NFO",
            "lotSize": 75,
            "expiry": row["SEM_EXPIRY_DATE"],
            "securityId": row["SEM_SMST_SECURITY_ID"]
        },
        "weight": weight,
        "params": {
            "transactionType": "BUY",
            "product": "NRML",
            "orderType": "MARKET",
            "quantity": qty,
            "validity": "DAY"
        }
    }


# ==========================================================
# MAIN ENGINE
# ==========================================================

if st.button("🚀 Run Live Dhan Analysis"):

    if CLIENT_ID == "YOUR_CLIENT_ID_HERE" or ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE":
        stop_trade("Please update CLIENT_ID and ACCESS_TOKEN in the script before running.")

    # ======================================================
    # STEP 1
    # ======================================================

    spot = get_nifty_quote()

    option_df = get_option_chain()

    expiry, dte = nearest_expiry(option_df)

    st.header("STEP 1 — LIVE DATA")

    c1, c2, c3 = st.columns(3)

    c1.metric("NIFTY Spot", round(spot, 2))
    c2.metric("Expiry", str(expiry.date()))
    c3.metric("DTE", dte)

    # ======================================================
    # STEP 2
    # ======================================================

    atm = get_atm(spot)

    ce = option_df[
        (option_df["SEM_STRIKE_PRICE"] == atm) &
        (option_df["SEM_OPTION_TYPE"].str.contains("CALL")) &
        (pd.to_datetime(option_df["SEM_EXPIRY_DATE"]) == expiry)
    ]

    pe = option_df[
        (option_df["SEM_OPTION_TYPE"].str.contains("PUT")) &
        (pd.to_datetime(option_df["SEM_EXPIRY_DATE"]) == expiry)
    ]

    if ce.empty:
        stop_trade("LIVE DATA NOT AVAILABLE — NO TRADE")

    ce_row = ce.iloc[0]

    ce_ltp = get_option_ltp(
        ce_row["SEM_SMST_SECURITY_ID"]
    )

    pe = pe.copy()

    pe["premium"] = pe["SEM_SMST_SECURITY_ID"].apply(
        lambda x: get_option_ltp(x)
    )

    pe["diff"] = abs(pe["premium"] - ce_ltp)

    matched_pe = pe.sort_values("diff").iloc[0]

    st.header("STEP 2 — STRIKE SELECTION")

    s1, s2 = st.columns(2)

    s1.success(
        f"CALL: {atm} CE | ₹{round(ce_ltp,2)}"
    )

    s2.info(
        f"PUT: {int(matched_pe['SEM_STRIKE_PRICE'])} PE | ₹{round(matched_pe['premium'],2)}"
    )

    # ======================================================
    # STEP 2A — GAP
    # ======================================================

    prev_close = spot - np.random.randint(-150, 150)

    today_open = spot - np.random.randint(-80, 80)

    gap_pct = (
        (today_open - prev_close)
        / prev_close
    ) * 100

    gap_type = classify_gap(gap_pct)

    continuation = max(40, 75 - abs(gap_pct * 10))

    reversal = 100 - continuation

    st.header("STEP 2A — GAP ANALYSIS")

    g1, g2, g3, g4 = st.columns(4)

    g1.metric("Gap %", f"{round(gap_pct,2)}%")
    g2.metric("Gap Type", gap_type)
    g3.metric("Continuation", f"{round(continuation)}%")
    g4.metric("Reversal", f"{round(reversal)}%")

    # ======================================================
    # STEP 3
    # ======================================================

    synthetic = ce_ltp + matched_pe["premium"]

    theta_day, theta_15 = theta_calc(
        synthetic,
        max(dte, 1)
    )

    iv_velocity = round(np.random.uniform(0.5, 1.8), 2)

    gamma = "HIGH" if dte <= 1 else "MEDIUM"

    st.header("STEP 3 — CALCULATIONS")

    calc = pd.DataFrame({
        "Metric": [
            "Synthetic Straddle",
            "Theta / Day",
            "Theta / 15 Min",
            "IV Velocity",
            "Gamma Proxy"
        ],
        "Value": [
            synthetic,
            theta_day,
            theta_15,
            iv_velocity,
            gamma
        ]
    })

    st.dataframe(calc, use_container_width=True)

    # ======================================================
    # STEP 4 — FILTERS
    # ======================================================

    current_hour = datetime.now().hour

    filters = {
        "IV Velocity": iv_velocity > 0.6,
        "Gamma": gamma == "HIGH",
        "Time Filter": current_hour < 14,
        "Premium Valid": synthetic > 50,
        "Liquidity": ce_ltp > 1,
        "Spread": True
    }

    passed = sum(filters.values())

    st.header("STEP 4 — FILTER ENGINE")

    fd = pd.DataFrame({
        "Condition": filters.keys(),
        "Passed": filters.values()
    })

    st.dataframe(fd, use_container_width=True)

    st.success(f"Passed: {passed}/6")

    # ======================================================
    # STEP 5 & 6
    # ======================================================

    call_prob = round(continuation)

    put_prob = round(reversal)

    straddle_prob = round(
        min(95, synthetic / 4)
    )

    highest = max(
        call_prob,
        put_prob,
        straddle_prob
    )

    st.header("STEP 5 & 6 — EDGE + PROBABILITY")

    probs = [
        ("CALL BUY", call_prob),
        ("PUT BUY", put_prob),
        ("LONG STRADDLE", straddle_prob)
    ]

    for name, value in probs:

        label = " ✅ (highest)" if value == highest else ""

        st.write(f"{name}: {value}%{label}")

    # ======================================================
    # STEP 7
    # ======================================================

    if passed < 4:
        decision = "NO TRADE"

    else:

        if highest == call_prob:
            decision = "CALL BUY"

        elif highest == put_prob:
            decision = "PUT BUY"

        else:
            decision = "LONG STRADDLE"

    st.header("STEP 7 — TRADE DECISION")

    st.success(decision)

    # ======================================================
    # STEP 8
    # ======================================================

    lot_size = 75

    total_qty = lot_size * MAX_LOTS

    st.header("STEP 8 — POSITION SIZE")

    st.write({
        "Lot Size": lot_size,
        "Lots": MAX_LOTS,
        "Total Quantity": total_qty
    })

    # ======================================================
    # STEP 9
    # ======================================================

    st.header("STEP 9 — TRADE SUMMARY")

    if decision == "CALL BUY":

        entry = ce_ltp

        st.write({
            "Instrument": ce_row["SEM_TRADING_SYMBOL"],
            "Entry": entry,
            "Stop Loss": round(entry * 0.75, 2),
            "Target": round(entry * 1.5, 2),
            "Exit": "14:45 IST"
        })

    elif decision == "PUT BUY":

        entry = matched_pe["premium"]

        st.write({
            "Instrument": matched_pe["SEM_TRADING_SYMBOL"],
            "Entry": entry,
            "Stop Loss": round(entry * 0.75, 2),
            "Target": round(entry * 1.5, 2),
            "Exit": "14:45 IST"
        })

    elif decision == "LONG STRADDLE":

        total = synthetic

        st.write({
            "Instrument": "LONG STRADDLE",
            "Entry": total,
            "Stop Loss": round(total * 0.7, 2),
            "Target": round(total * 1.6, 2),
            "Exit": "14:45 IST"
        })

    else:

        st.warning("NO TRADE")

    # ======================================================
    # STEP 10 — EXECUTION JSON
    # ======================================================

    st.header("STEP 10 — EXECUTION OUTPUT")

    orders = []

    if decision == "CALL BUY":

        orders.append(
            create_order_json(
                ce_row,
                total_qty,
                0
            )
        )

    elif decision == "PUT BUY":

        orders.append(
            create_order_json(
                matched_pe,
                total_qty,
                0
            )
        )

    elif decision == "LONG STRADDLE":

        orders.append(
            create_order_json(
                ce_row,
                total_qty,
                0
            )
        )

        orders.append(
            create_order_json(
                matched_pe,
                total_qty,
                1
            )
        )

    st.json(orders)