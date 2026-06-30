import streamlit as st
import pandas as pd
import numpy as np
import hashlib
import requests
import pyotp
import base64
import datetime
import psycopg2
import sqlite3
import os
import warnings
import threading
import time
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# Suppress pandas warning for using raw psycopg2 connection
warnings.filterwarnings('ignore', category=UserWarning)

# --- CONFIGURATION & SESSION INITIALIZATION ---
st.set_page_config(page_title="Intraday Options Quant Matrix", layout="wide")

# --- SYSTEM VARIABLES ---
MAX_LOTS_ALLOWED = 4
LOT_SIZE_NIFTY = 25  
LAST_ENTRY_TIME = datetime.time(14, 0)
PRODUCT_TYPE = "NRML"
ORDER_TYPE = 2       

# --- TELEGRAM ALERT CONFIG ---
DEFAULT_TELEGRAM_BOT_TOKEN = "7851529826:AAHfyHVrVZi5iQubliaNgde76gPhr8pxql4"
DEFAULT_TELEGRAM_CHAT_ID = "567677761"
DEFAULT_SUPABASE_URI = "postgresql://postgres.hcujozwjlsprkmnwlrxz:qyQ+N8+sr+fHens@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"

def get_secret_value(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default

TELEGRAM_BOT_TOKEN = get_secret_value("TELEGRAM_BOT_TOKEN", DEFAULT_TELEGRAM_BOT_TOKEN)
TELEGRAM_CHAT_ID   = get_secret_value("TELEGRAM_CHAT_ID", DEFAULT_TELEGRAM_CHAT_ID)

def send_telegram(msg: str, repeat: int = 1):
    """Send Telegram alert without blocking dashboard rendering."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    def worker():
        for i in range(repeat):
            try:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
                if repeat > 1 and i < repeat - 1:
                    time.sleep(0.5)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()

def check_volume_spike_alerts(df_flow, atm_strike, sent_alerts: set,
                              spike_multiplier=3.0, min_volume=200000,
                              dominance_ratio=1.5, min_history=5,
                              repeat_count=3):
    alerts = []
    required_cols = {"timestamp", "strike", "ce_vol", "pe_vol"}
    if df_flow.empty or not required_cols.issubset(df_flow.columns):
        return alerts

    df_flow = df_flow.copy()
    df_flow["timestamp"] = pd.to_datetime(df_flow["timestamp"], errors="coerce")
    df_flow = df_flow.dropna(subset=["timestamp", "strike"])

    def safe_num(value):
        return 0.0 if pd.isna(value) else float(value)

    latest_ts = df_flow["timestamp"].max()
    latest_time = latest_ts.strftime("%H:%M")

    for strike in sorted(df_flow["strike"].unique()):
        df_s = df_flow[df_flow["strike"] == strike].sort_values("timestamp").reset_index(drop=True)
        if len(df_s) < min_history + 1:
            continue

        deltas = []
        for i in range(1, len(df_s)):
            deltas.append({
                "ts": df_s.iloc[i]["timestamp"],
                "d_ce_vol": max(safe_num(df_s.iloc[i]["ce_vol"]) - safe_num(df_s.iloc[i - 1]["ce_vol"]), 0.0),
                "d_pe_vol": max(safe_num(df_s.iloc[i]["pe_vol"]) - safe_num(df_s.iloc[i - 1]["pe_vol"]), 0.0),
            })

        latest = deltas[-1]
        history = deltas[:-1]
        if len(history) < min_history:
            continue

        avg_ce = sum(d["d_ce_vol"] for d in history) / len(history)
        avg_pe = sum(d["d_pe_vol"] for d in history) / len(history)
        d_ce = latest["d_ce_vol"]
        d_pe = latest["d_pe_vol"]
        atm_tag = " ATM" if strike == atm_strike else ""
        tag = f"{int(strike)}{atm_tag}"

        pe_threshold = max(min_volume, avg_pe * spike_multiplier)
        ce_threshold = max(min_volume, avg_ce * spike_multiplier)

        if d_pe >= pe_threshold and d_pe >= max(d_ce * dominance_ratio, 1):
            key = f"VOL_PE_{int(strike)}_{latest['ts'].strftime('%H%M')}"
            if key not in sent_alerts:
                multiple = d_pe / avg_pe if avg_pe > 0 else 0
                alerts.append((key,
                    f"🚨 <b>PUT VOLUME SPIKE</b>\n"
                    f"Strike <b>{tag}</b> | {latest_time}\n"
                    f"PE added: <b>{format_indian_num(d_pe)}</b> volume ({d_pe / LOT_SIZE_NIFTY:,.0f} lots)\n"
                    f"Today avg/min: {format_indian_num(avg_pe)} | Spike: {multiple:.1f}x\n"
                    f"CE same minute: {format_indian_num(d_ce)}\n"
                    f"📌 Traders suddenly active in PUT side",
                    "CRITICAL", repeat_count))

        if d_ce >= ce_threshold and d_ce >= max(d_pe * dominance_ratio, 1):
            key = f"VOL_CE_{int(strike)}_{latest['ts'].strftime('%H%M')}"
            if key not in sent_alerts:
                multiple = d_ce / avg_ce if avg_ce > 0 else 0
                alerts.append((key,
                    f"🚨 <b>CALL VOLUME SPIKE</b>\n"
                    f"Strike <b>{tag}</b> | {latest_time}\n"
                    f"CE added: <b>{format_indian_num(d_ce)}</b> volume ({d_ce / LOT_SIZE_NIFTY:,.0f} lots)\n"
                    f"Today avg/min: {format_indian_num(avg_ce)} | Spike: {multiple:.1f}x\n"
                    f"PE same minute: {format_indian_num(d_pe)}\n"
                    f"📌 Traders suddenly active in CALL side",
                    "CRITICAL", repeat_count))

    return alerts

def check_reversal_alerts(df_flow, atm_strike, sent_alerts: set,
                           oi_spike_lots=500, vol_spike_lots=1000,
                           pcr_bear_thresh=1.5, pcr_bull_thresh=0.6,
                           consec_intervals=3, repeat_count=10):
    alerts = []
    required_cols = {"timestamp", "strike", "ce_oi", "pe_oi", "ce_vol", "pe_vol"}
    if df_flow.empty or not required_cols.issubset(df_flow.columns):
        return alerts

    df_flow = df_flow.copy()
    df_flow["timestamp"] = pd.to_datetime(df_flow["timestamp"], errors="coerce")
    df_flow = df_flow.dropna(subset=["timestamp", "strike"])

    def safe_num(value):
        return 0.0 if pd.isna(value) else float(value)

    now_str = get_ist_now().strftime("%H:%M")
    OI_SPIKE  = oi_spike_lots * 25
    VOL_SPIKE = vol_spike_lots * 25

    strike_deltas = {}
    for strike in df_flow["strike"].unique():
        df_s = df_flow[df_flow["strike"] == strike].sort_values("timestamp").reset_index(drop=True)
        if len(df_s) < 2:
            continue
        deltas = []
        for i in range(1, len(df_s)):
            deltas.append({
                "ts":       df_s.iloc[i]["timestamp"],
                "d_ce_oi":  safe_num(df_s.iloc[i]["ce_oi"]) - safe_num(df_s.iloc[i-1]["ce_oi"]),
                "d_pe_oi":  safe_num(df_s.iloc[i]["pe_oi"]) - safe_num(df_s.iloc[i-1]["pe_oi"]),
                "d_ce_vol": safe_num(df_s.iloc[i]["ce_vol"]) - safe_num(df_s.iloc[i-1]["ce_vol"]),
                "d_pe_vol": safe_num(df_s.iloc[i]["pe_vol"]) - safe_num(df_s.iloc[i-1]["pe_vol"]),
                "ce_oi":    safe_num(df_s.iloc[i]["ce_oi"]),
                "pe_oi":    safe_num(df_s.iloc[i]["pe_oi"]),
            })
        strike_deltas[strike] = deltas

    for strike, deltas in strike_deltas.items():
        if not deltas:
            continue
        atm_tag = " 🎯ATM" if strike == atm_strike else ""
        tag     = f"{int(strike)}{atm_tag}"
        latest  = deltas[-1]
        d_ce_oi  = latest["d_ce_oi"]
        d_pe_oi  = latest["d_pe_oi"]
        d_ce_vol = latest["d_ce_vol"]
        d_pe_vol = latest["d_pe_vol"]
        ce_oi    = latest["ce_oi"]
        pe_oi    = latest["pe_oi"]

        # SIGNAL 1: Single-interval spike
        if d_pe_oi > OI_SPIKE:
            key = f"PE_SPIKE_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"🔴 <b>BEARISH — PUT LOADING</b>\nStrike <b>{tag}</b> | {now_str}\nPE OI +{d_pe_oi/25:,.0f} lots\n⚠️ Bears building — watch DOWNSIDE",
                    "HIGH", repeat_count))

        if d_ce_oi > OI_SPIKE:
            key = f"CE_SPIKE_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"🟢 <b>BULLISH — CALL LOADING</b>\nStrike <b>{tag}</b> | {now_str}\nCE OI +{d_ce_oi/25:,.0f} lots\n⚡ Bulls building — watch UPSIDE",
                    "HIGH", repeat_count))

        # SIGNAL 2: Consecutive buildup (the smart money early-warning)
        if len(deltas) >= consec_intervals:
            last_n = deltas[-consec_intervals:]
            pe_consec    = sum(1 for d in last_n if d["d_pe_oi"] > OI_SPIKE // 2)
            ce_consec    = sum(1 for d in last_n if d["d_ce_oi"] > OI_SPIKE // 2)
            total_pe_built = sum(d["d_pe_oi"] for d in last_n)
            total_ce_built = sum(d["d_ce_oi"] for d in last_n)

            if pe_consec >= consec_intervals:
                key = f"PE_CONSEC_{strike}_{now_str}"
                if key not in sent_alerts:
                    alerts.append((key,
                        f"🚨🔴 <b>SMART MONEY — SUSTAINED PUT BUILDUP</b>\nStrike <b>{tag}</b> | {now_str}\n{consec_intervals} consecutive intervals of PUT loading\nTotal PE OI added: +{total_pe_built/25:,.0f} lots\n🎯 BIG MOVE LIKELY — WATCH DOWNSIDE BREAK",
                        "CRITICAL", repeat_count))

            if ce_consec >= consec_intervals:
                key = f"CE_CONSEC_{strike}_{now_str}"
                if key not in sent_alerts:
                    alerts.append((key,
                        f"🚨🟢 <b>SMART MONEY — SUSTAINED CALL BUILDUP</b>\nStrike <b>{tag}</b> | {now_str}\n{consec_intervals} consecutive intervals of CALL loading\nTotal CE OI added: +{total_ce_built/25:,.0f} lots\n🎯 BIG MOVE LIKELY — WATCH UPSIDE BREAK",
                        "CRITICAL", repeat_count))

        # SIGNAL 3: PCR flip
        if ce_oi > 0 and len(deltas) >= 2 and deltas[-2]["ce_oi"] > 0:
            pcr_now  = pe_oi / ce_oi
            pcr_prev = deltas[-2]["pe_oi"] / deltas[-2]["ce_oi"]
            if pcr_prev < pcr_bear_thresh and pcr_now >= pcr_bear_thresh:
                key = f"PCR_BEAR_{strike}_{now_str}"
                if key not in sent_alerts:
                    alerts.append((key,
                        f"🔴 <b>PCR FLIP — BEARISH WALL</b>\nStrike <b>{tag}</b> | {now_str}\nPCR {pcr_prev:.2f} → {pcr_now:.2f} (crossed {pcr_bear_thresh})\nPut writers dominating — RESISTANCE forming",
                        "MEDIUM", repeat_count))
            if pcr_prev > pcr_bull_thresh and pcr_now <= pcr_bull_thresh:
                key = f"PCR_BULL_{strike}_{now_str}"
                if key not in sent_alerts:
                    alerts.append((key,
                        f"🟢 <b>PCR FLIP — BULLISH WALL</b>\nStrike <b>{tag}</b> | {now_str}\nPCR {pcr_prev:.2f} → {pcr_now:.2f} (below {pcr_bull_thresh})\nCall writers dominating — SUPPORT forming",
                        "MEDIUM", repeat_count))

        # SIGNAL 4: Unwinding (reversal signal)
        if d_ce_oi < -OI_SPIKE and d_ce_vol > VOL_SPIKE:
            key = f"CE_UNWIND_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"⚡ <b>CE UNWINDING — CALL SELLERS COVERING</b>\nStrike <b>{tag}</b> | {now_str}\nCE OI {d_ce_oi/25:,.0f} lots | Vol spiking\n🔄 Bears exiting — BULLISH REVERSAL possible",
                    "HIGH", repeat_count))

        if d_pe_oi < -OI_SPIKE and d_pe_vol > VOL_SPIKE:
            key = f"PE_UNWIND_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"⚡ <b>PE UNWINDING — PUT SELLERS COVERING</b>\nStrike <b>{tag}</b> | {now_str}\nPE OI {d_pe_oi/25:,.0f} lots | Vol spiking\n🔄 Bulls exiting — BEARISH REVERSAL possible",
                    "HIGH", repeat_count))

        # SIGNAL 5: Massive block (waterfall / institutional)
        MASSIVE = OI_SPIKE * 5
        if d_ce_oi > MASSIVE:
            key = f"CE_MASSIVE_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"🚨🚨 <b>MASSIVE CALL BLOCK — WATERFALL RISK</b>\nStrike <b>{tag}</b> | {now_str}\nCE OI +{d_ce_oi/25:,.0f} lots IN ONE INTERVAL\n📌 Institutional sell block — HIGH PROBABILITY DOWNSIDE",
                    "CRITICAL", repeat_count))
        if d_pe_oi > MASSIVE:
            key = f"PE_MASSIVE_{strike}_{now_str}"
            if key not in sent_alerts:
                alerts.append((key,
                    f"🚨🚨 <b>MASSIVE PUT BLOCK — RALLY RISK</b>\nStrike <b>{tag}</b> | {now_str}\nPE OI +{d_pe_oi/25:,.0f} lots IN ONE INTERVAL\n📌 Institutional support block — HIGH PROBABILITY UPSIDE",
                    "CRITICAL", repeat_count))

    return alerts

# --- IST TIMEZONE OVERRIDE ---
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def get_ist_now():
    return datetime.datetime.now(IST).replace(tzinfo=None)

if "sent_alerts" not in st.session_state:
    st.session_state.sent_alerts = set()
if "alert_log" not in st.session_state:
    st.session_state.alert_log = []  # List of dicts: {time, severity, message}
if "fyers_instance" not in st.session_state:
    st.session_state.fyers_instance = None
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "auto_login_attempted" not in st.session_state:
    st.session_state.auto_login_attempted = False

# --- NIFTY 50 CONSTITUENT ARRAYS ---
TOP_5_SYMBOLS = ["NSE:HDFCBANK-EQ", "NSE:RELIANCE-EQ", "NSE:ICICIBANK-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"]
NEXT_20_SYMBOLS = ["NSE:ITC-EQ", "NSE:LT-EQ", "NSE:KOTAKBANK-EQ", "NSE:AXISBANK-EQ", "NSE:SBIN-EQ", "NSE:BHARTIARTL-EQ", "NSE:BAJFINANCE-EQ", "NSE:HINDUNILVR-EQ", "NSE:M&M-EQ", "NSE:MARUTI-EQ", "NSE:SUNPHARMA-EQ", "NSE:HCLTECH-EQ", "NSE:TATAMOTORS-EQ", "NSE:TATASTEEL-EQ", "NSE:NTPC-EQ", "NSE:POWERGRID-EQ", "NSE:TITAN-EQ", "NSE:ULTRACEMCO-EQ", "NSE:ASIANPAINT-EQ", "NSE:COALINDIA-EQ"]
REMAINING_25_SYMBOLS = ["NSE:BAJAJFINSV-EQ", "NSE:ADANIENT-EQ", "NSE:ADANIPORTS-EQ", "NSE:NESTLEIND-EQ", "NSE:GRASIM-EQ", "NSE:ONGC-EQ", "NSE:JSWSTEEL-EQ", "NSE:HINDALCO-EQ", "NSE:CIPLA-EQ", "NSE:DRREDDY-EQ", "NSE:TATACONSUM-EQ", "NSE:WIPRO-EQ", "NSE:APOLLOHOSP-EQ", "NSE:BRITANNIA-EQ", "NSE:EICHERMOT-EQ", "NSE:HEROMOTOCO-EQ", "NSE:DIVISLAB-EQ", "NSE:TECHM-EQ", "NSE:BAJAJ-AUTO-EQ", "NSE:INDUSINDBK-EQ", "NSE:SBILIFE-EQ", "NSE:HDFCLIFE-EQ", "NSE:BPCL-EQ", "NSE:LTIM-EQ", "NSE:TRENT-EQ"]

# --- SYSTEM UTILITIES ---
def b64(s): return base64.b64encode(str(s).encode()).decode()

def generate_app_id_hash(app_id, app_type, app_secret):
    return hashlib.sha256(f"{app_id}-{app_type}:{app_secret}".encode()).hexdigest()

def execute_auto_login(fy_id, pin, totp_key, app_id, app_type, app_secret, redirect_uri):
    session = requests.Session()
    try:
        r1 = session.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", json={"fy_id": b64(fy_id), "app_id": "2"})
        request_key = r1.json().get("request_key")
        totp_code = pyotp.TOTP(totp_key).now()
        r2 = session.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": request_key, "otp": totp_code})
        request_key = r2.json().get("request_key")
        r3 = session.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json={"request_key": request_key, "identity_type": "pin", "identifier": b64(pin)})
        login_token = r3.json().get("data", {}).get("access_token")
        r4 = session.post("https://api-t1.fyers.in/api/v3/token", json={
            "fyers_id": fy_id, "app_id": app_id, "redirect_uri": redirect_uri, "appType": app_type,
            "code_challenge": "", "state": "quant_engine", "scope": "", "nonce": "", "response_type": "code", "create_cookie": True
        }, headers={"Authorization": f"Bearer {login_token}"})
        auth_url = r4.json().get("Url")
        auth_code = parse_qs(urlparse(auth_url).query).get("auth_code", [None])[0]
        app_id_hash = generate_app_id_hash(app_id, app_type, app_secret)
        r5 = session.post("https://api-t1.fyers.in/api/v3/validate-authcode", json={
            "grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code
        })
        return r5.json().get("access_token")
    except Exception as e:
        st.error(f"Authentication Failure: {str(e)}")
        return None

def establish_fyers_gateway(fy_id, pin, totp_key, app_id, app_secret, redirect_uri):
    token = execute_auto_login(fy_id, pin, totp_key, app_id, "100", app_secret, redirect_uri)
    if token:
        st.session_state.fyers_instance = fyersModel.FyersModel(client_id=f"{app_id}-100", token=token, is_async=False, log_path="")
        st.session_state.authenticated = True
        return True
    st.session_state.fyers_instance = None
    st.session_state.authenticated = False
    return False

def get_live_quotes(fyers, symbols_list):
    data = {"symbols": ",".join(symbols_list)}
    try:
        response = fyers.quotes(data=data)
        valid_quotes = {}
        if response and response.get("s") == "ok":
            for d in response.get("d", []):
                if d.get("s") == "ok" and "v" in d and "lp" in d["v"]:
                    valid_quotes[d["n"]] = d["v"]
        return valid_quotes
    except Exception:
        return {}

def get_last_closing_spot(fyers, symbol="NSE:NIFTY50-INDEX"):
    try:
        end_date = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=5)
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": start_date.strftime('%Y-%m-%d'),
            "range_to": end_date.strftime('%Y-%m-%d'),
            "cont_flag": "1"
        }
        res = fyers.history(data=data)
        if res and res.get('s') == 'ok' and len(res.get('candles', [])) > 0:
            last_candle = res['candles'][-1]
            return float(last_candle[4])
    except Exception as e:
        st.error(f"Historical fallback error: {e}")
    return None

def color_coding(val):
    color = ''
    if isinstance(val, str):
        if val.startswith('+') or "🟢" in val or "BUY" in val: color = '#4ade80' 
        elif val.startswith('-') or "🔴" in val or "SHORTS" in val: color = '#f87171' 
    return f'color: {color}' if color else ''

def format_indian_num(number):
    if pd.isna(number): return "0"
    s = str(int(number))
    if len(s) <= 3: return s
    last_three = s[-3:]
    remaining = s[:-3]
    remaining_fmt = ""
    while len(remaining) > 2:
        remaining_fmt = "," + remaining[-2:] + remaining_fmt
        remaining = remaining[:-2]
    if remaining: remaining_fmt = remaining + remaining_fmt
    return remaining_fmt + "," + last_three

def format_percentage(val):
    if pd.isna(val) or val == np.inf or val == -np.inf: return "0.00%"
    return f"+{val:,.2f}%" if val >= 0 else f"{val:,.2f}%"

# --- ADVANCED RADAR SIGNAL INTERPRETER ---
def calculate_orderflow_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo):
    if d_ce_oi == 0 and d_pe_oi == 0 and d_ce_vo == 0 and d_pe_vo == 0: return "⚖️ NO FLOW"
    if d_ce_oi > d_pe_oi and d_pe_vo > d_ce_vo: return "🔴 PE BUY"
    elif d_pe_oi > d_ce_oi and d_ce_vo > d_pe_vo: return "🟢 CE BUY"
    elif d_ce_oi > d_pe_oi and d_ce_vo > d_pe_vo: return "📉 CE SHORTS"
    elif d_pe_oi > d_ce_oi and d_pe_vo > d_ce_vo: return "📈 PE SHORTS"
    return "⚖️ NEUTRAL"

# --- POP-UP MODAL TIMELINE ENGINE ---
@st.dialog("📋 Combined Intel Timeline Ledger", width="large")
def show_strike_popup(strike, df_flow, is_atm_anchor):
    title_decorator = f"🎯 Strike {strike} (ATM)" if is_atm_anchor else f"Strike {strike}"
    st.subheader(f"Order Flow Analysis Matrix for {title_decorator}")
    st.caption("Chronological 1-minute interval snapshots combined with Volume + Open Interest velocity tracking rules.")
    
    df_st = df_flow[df_flow['strike'] == strike].copy().sort_values('timestamp', ascending=True)
    df_st['d_ce_oi'] = df_st['ce_oi'].diff().fillna(0).astype(int)
    df_st['d_pe_oi'] = df_st['pe_oi'].diff().fillna(0).astype(int)
    df_st['d_ce_vo'] = df_st['ce_vol'].diff().fillna(0).astype(int)
    df_st['d_pe_vo'] = df_st['pe_vol'].diff().fillna(0).astype(int)

    # Pull daily OI change columns if available (stored from API)
    for col in ['ce_oich', 'ce_oichp', 'pe_oich', 'pe_oichp']:
        if col not in df_st.columns:
            df_st[col] = 0.0

    signals = []
    for _, r in df_st.iterrows():
        signals.append(calculate_orderflow_signal(r['d_ce_oi'], r['d_pe_oi'], r['d_ce_vo'], r['d_pe_vo']))
    df_st['🎯 ACTION SIGNAL'] = signals

    df_st.sort_values('timestamp', ascending=False, inplace=True)
    df_st['Time'] = df_st['timestamp'].dt.strftime('%H:%M:%S %p')

    def fmt_chg(x):
        x = int(x)
        return f"+{x:,}" if x > 0 else (f"{x:,}" if x < 0 else "0")

    def fmt_pct(x):
        return f"+{x:.2f}%" if x > 0 else (f"{x:.2f}%" if x < 0 else "+0.00%")

    df_render = pd.DataFrame()
    df_render['Timestamp']           = df_st['Time'].values
    df_render['🎯 ACTION SIGNAL']    = df_st['🎯 ACTION SIGNAL'].values
    df_render['CE OI Change (Day)']  = df_st['ce_oich'].apply(fmt_chg).values
    df_render['CE OI % Chg (Day)']   = df_st['ce_oichp'].apply(fmt_pct).values
    df_render['PE OI Change (Day)']  = df_st['pe_oich'].apply(fmt_chg).values
    df_render['PE OI % Chg (Day)']   = df_st['pe_oichp'].apply(fmt_pct).values
    df_render['Δ OI CE (1-Min)']     = df_st['d_ce_oi'].apply(fmt_chg).values
    df_render['Δ OI PE (1-Min)']     = df_st['d_pe_oi'].apply(fmt_chg).values
    df_render['Δ Vol CE (1-Min)']    = df_st['d_ce_vo'].apply(fmt_chg).values
    df_render['Δ Vol PE (1-Min)']    = df_st['d_pe_vo'].apply(fmt_chg).values

    color_cols = ['🎯 ACTION SIGNAL',
                  'CE OI Change (Day)', 'CE OI % Chg (Day)',
                  'PE OI Change (Day)', 'PE OI % Chg (Day)',
                  'Δ OI CE (1-Min)', 'Δ OI PE (1-Min)',
                  'Δ Vol CE (1-Min)', 'Δ Vol PE (1-Min)']

    styled_popup = df_render.style.map(color_coding, subset=color_cols)
    st.dataframe(styled_popup, use_container_width=True, hide_index=True)

def backup_and_send_telegram(supabase_conn):
    try:
        st.info("🔄 Compiling database records into local SQLite binary snapshot...")
        df_flow_export = pd.read_sql_query("SELECT * FROM strike_flow", supabase_conn)
        df_hist_export = pd.read_sql_query("SELECT * FROM flow_history", supabase_conn)
        
        filename = f"Nifty_Data_{get_ist_now().strftime('%Y-%m-%d')}.db"
        lite_conn = sqlite3.connect(filename)
        df_flow_export.to_sql("strike_flow", lite_conn, if_exists="replace", index=False)
        df_hist_export.to_sql("flow_history", lite_conn, if_exists="replace", index=False)
        lite_conn.close()
        
        token = get_secret_value("TELEGRAM_BOT_TOKEN")
        chat_id = get_secret_value("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            st.error("Telegram backup is not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to Streamlit secrets.")
            return

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        
        with open(filename, "rb") as db_file:
            payload = {"chat_id": chat_id, "caption": f"📂 Intraday Quantum Matrix Backup File\n📅 Date: {get_ist_now().strftime('%Y-%m-%d')}"}
            files = {"document": db_file}
            response = requests.post(url, data=payload, files=files)
            
        if os.path.exists(filename): os.remove(filename)
        if response.json().get("ok"): st.success("🎯 Backup .db transmitted to Telegram account successfully!")
        else: st.error(f"Telegram Failure: {response.json()}")
    except Exception as ex: st.error(f"Backup Error: {ex}")

# =====================================================================
# SIDEBAR NAVIGATION INTERFACE
# =====================================================================
with st.sidebar:
    st.header("🎛️ Operational Mode Matrix")
    app_mode = st.radio("Select Active Core Node Environment", ["🔴 Live Exchange Node", "📁 Offline DB File Lookback"])
    st.markdown("---")
    st.header("📲 Telegram Alerts")
    if not TELEGRAM_BOT_TOKEN:
        st.warning("Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to Streamlit secrets.")
    else:
        st.success("🟢 Telegram Active")
        if st.button("🔔 Send Test Alert Now"):
            send_telegram(
                "✅ <b>Nifty Volume Spike Alert System — LIVE</b>\n"
                "Connected and monitoring sudden CE/PE volume spikes against today's average pace.",
                repeat=3
            )
            st.sidebar.success("Test sent (3x)!")
    st.markdown("**📡 Alert Settings**")
    st.session_state["tg_repeat"]  = st.slider("🔁 Repeat each alert (times)", 1, 10, 3, help="Telegram repeats happen in background, so the dashboard will not freeze")
    with st.expander("⚙️ Volume Spike Thresholds"):
        st.session_state["vol_spike_mult"] = st.number_input("Spike vs today's average (x)", value=3.0, min_value=1.0, step=0.5)
        st.session_state["min_vol_lakh"] = st.number_input("Minimum fresh volume (lakh)", value=2.0, min_value=0.1, step=0.5)
        st.session_state["side_dominance"] = st.number_input("Side dominance vs opposite side (x)", value=1.5, min_value=1.0, step=0.1)
        st.session_state["min_history"] = st.number_input("Minimum past minutes for average", value=5, min_value=2, max_value=60, step=1)
    if st.button("🗑️ Clear Alert History"):
        st.session_state.sent_alerts = set()
        st.session_state.alert_log = []
        st.sidebar.success("Alert history cleared")
    st.markdown("---")

offline_data_ready = False
df_history, df_flow = pd.DataFrame(), pd.DataFrame()
manual_spot = 0.0

if app_mode == "🔴 Live Exchange Node":
    st_autorefresh(interval=60000, key="matrix_autorefresh")
    
    with st.sidebar:
        st.header("Gateway Security Credentials")
        
        input_fy_id = st.text_input("Fyers ID", value="FAJ88605")
        input_pin = st.text_input("Security PIN", value="4089", type="password")
        input_totp = st.text_input("TOTP Seed Key", value="ZHOQNKKVMI7IRCAPUFX7OXRMPFXRYVU6", type="password")
        input_app_id = st.text_input("App ID Parameter", value="Q3B2S22L5M")
        input_app_secret = st.text_input("Client Secret Key", value="PWZD03ONQ4", type="password")
        input_redirect = st.text_input("Redirect URI End-point", value="https://trade.fyers.in/api-login/redirect-uri/index.html")
             
        st.markdown("---")
        st.subheader("Manual Data Overrides")
        manual_spot = st.number_input("Override NIFTY Spot Price (Leave 0 to use API)", value=0.0, step=1.0)
        
        if st.button("Establish Production Gateway"):
            with st.spinner("Connecting server clusters to exchange node..."):
                if establish_fyers_gateway(input_fy_id, input_pin, input_totp, input_app_id, input_app_secret, input_redirect):
                    st.success("Synchronized successfully. Node pipelines online.")
                else:
                    st.error("Gateway login failed. Check Fyers credentials/TOTP and retry.")

    if not st.session_state.authenticated and not st.session_state.auto_login_attempted:
        st.session_state.auto_login_attempted = True
        with st.spinner("Auto-connecting to Fyers gateway..."):
            establish_fyers_gateway(input_fy_id, input_pin, input_totp, input_app_id, input_app_secret, input_redirect)

    if not st.session_state.authenticated or st.session_state.fyers_instance is None:
        st.warning("⚡ Auto-login failed or is waiting on Fyers. Use the sidebar gateway button to retry.")
        st.stop()

    fyers = st.session_state.fyers_instance

    # --- PULL REALTIME PIPELINES ---
    batch_1 = ["NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"] + TOP_5_SYMBOLS + NEXT_20_SYMBOLS
    batch_2 = REMAINING_25_SYMBOLS
    spot_raw = {**get_live_quotes(fyers, batch_1), **get_live_quotes(fyers, batch_2)}

    if manual_spot > 0:
        nifty_spot = manual_spot
        open_price, prev_close = nifty_spot, nifty_spot 
    else:
        if not spot_raw or "NSE:NIFTY50-INDEX" not in spot_raw:
            st.warning("⚠️ Live quotes unavailable (Market Closed). Attempting to fetch historical EOD data for analysis...")
            historical_close = get_last_closing_spot(fyers)
            if historical_close:
                nifty_spot = historical_close
                open_price, prev_close = historical_close, historical_close
                st.success(f"✅ Loaded last market close: ₹{nifty_spot:,.2f}")
            else:
                st.error("LIVE DATA AND HISTORICAL FALLBACK FAILED. Please enter a Manual Spot Price in the sidebar.")
                st.stop()
        else:
            nifty_spot = float(spot_raw["NSE:NIFTY50-INDEX"]["lp"])
            open_price = float(spot_raw["NSE:NIFTY50-INDEX"]["open_price"])
            prev_close = float(spot_raw["NSE:NIFTY50-INDEX"]["prev_close_price"])

    atm_strike = round(nifty_spot / 50) * 50

    chain_response = fyers.optionchain(data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 30, "timestamp": "", "greeks": "1"})
    if not chain_response or chain_response.get("s") != "ok":
        st.error("LIVE DATA NOT AVAILABLE (Option chain API failed)")
        st.stop()

    chain_data = chain_response.get("data", {})
    options_list = chain_data.get("optionsChain", [])

    # --- PROCESS REALTIME RECORDS TO SUPABASE ---
    total_ce_oi, total_pe_oi, total_ce_vol, total_pe_vol = 0, 0, 0, 0
    strike_oi_totals, current_strike_data = {}, []
    target_strikes = [atm_strike + (i * 50) for i in range(-10, 11)]

    for contract in options_list:
        opt_type, strike = contract.get("option_type"), contract.get("strike_price")
        
        # EXTRACTING CRITICAL API FIELDS (DAILY CHANGES)
        oi_val = int(contract.get("oi", 0))
        vol_val = int(contract.get("volume", 0))
        ltp_val = float(contract.get("ltp", 0.0))
        # Use oich/oichp if Fyers populates them; else calculate from oi - prev_oi
        raw_oich = float(contract.get("oich") or 0.0)
        raw_oichp = float(contract.get("oichp") or 0.0)
        prev_oi_val = float(contract.get("prev_oi") or 0.0)
        if raw_oich == 0 and prev_oi_val > 0:
            raw_oich = float(oi_val) - prev_oi_val
            raw_oichp = (raw_oich / prev_oi_val * 100) if prev_oi_val != 0 else 0.0
        oich_val = raw_oich
        oichp_val = raw_oichp
        
        strike_oi_totals[strike] = strike_oi_totals.get(strike, 0) + oi_val
        
        match = next((d for d in current_strike_data if d['strike'] == strike), None)
        if not match:
            match = {
                "strike": strike, 
                "ce_oi": 0, "ce_vol": 0, "ce_ltp": 0.0, "ce_oich": 0.0, "ce_oichp": 0.0,
                "pe_oi": 0, "pe_vol": 0, "pe_ltp": 0.0, "pe_oich": 0.0, "pe_oichp": 0.0
            }
            current_strike_data.append(match)
            
        if opt_type == "CE":
            match['ce_oi'], match['ce_vol'], match['ce_ltp'] = oi_val, vol_val, ltp_val
            match['ce_oich'], match['ce_oichp'] = oich_val, oichp_val
            total_ce_oi += oi_val; total_ce_vol += vol_val
        else:
            match['pe_oi'], match['pe_vol'], match['pe_ltp'] = oi_val, vol_val, ltp_val
            match['pe_oich'], match['pe_oichp'] = oich_val, oichp_val
            total_pe_oi += oi_val; total_pe_vol += vol_val

    atm_call_contract = next((c for c in options_list if c.get("option_type") == "CE" and c.get("strike_price") == atm_strike), {"oi": 0, "ltp": 0})
    matched_put_contract = min([c for c in options_list if c.get("option_type") == "PE"], key=lambda x: abs(float(x.get("ltp", 0)) - float(atm_call_contract.get("ltp", 0))), default={"oi":0, "strike_price": atm_strike})

    try:
        supabase_uri = get_secret_value("SUPABASE_URI", DEFAULT_SUPABASE_URI)
        if not supabase_uri:
            st.error("Database is not configured. Add SUPABASE_URI to Streamlit secrets, or use Offline DB File Lookback mode.")
            st.stop()

        conn = psycopg2.connect(supabase_uri); conn.autocommit = True; c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS flow_history (timestamp TIMESTAMP, total_ce_oi BIGINT, total_pe_oi BIGINT, atm_ce_oi BIGINT, atm_pe_oi BIGINT)")
        
        # UPGRADED SCHEMA: Now stores daily OI changes persistently
        c.execute("""CREATE TABLE IF NOT EXISTS strike_flow (
            timestamp TIMESTAMP, strike INTEGER, 
            ce_oi BIGINT, ce_vol BIGINT, ce_ltp REAL, 
            pe_oi BIGINT, pe_vol BIGINT, pe_ltp REAL,
            ce_oich REAL DEFAULT 0, ce_oichp REAL DEFAULT 0,
            pe_oich REAL DEFAULT 0, pe_oichp REAL DEFAULT 0
        )""")
        
        # SAFE EVOLUTION: Add columns if table existed prior to this update
        for col in ['ce_oich', 'ce_oichp', 'pe_oich', 'pe_oichp']:
            c.execute(f"ALTER TABLE strike_flow ADD COLUMN IF NOT EXISTS {col} REAL DEFAULT 0")
        
        c.execute("SELECT timestamp FROM flow_history ORDER BY timestamp DESC LIMIT 1")
        last_entry = c.fetchone()
        if last_entry and last_entry[0].date() != get_ist_now().date():
            c.execute("TRUNCATE TABLE flow_history; TRUNCATE TABLE strike_flow;")

        current_time = get_ist_now()
        c.execute("INSERT INTO flow_history VALUES (%s, %s, %s, %s, %s)", (current_time, total_ce_oi, total_pe_oi, int(atm_call_contract.get("oi",0)), int(matched_put_contract.get("oi",0))))
        for r in current_strike_data:
            if r['strike'] in target_strikes:
                c.execute(
                    """INSERT INTO strike_flow (
                        timestamp, strike,
                        ce_oi, ce_vol, ce_ltp,
                        pe_oi, pe_vol, pe_ltp,
                        ce_oich, ce_oichp, pe_oich, pe_oichp
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (current_time, r['strike'], r['ce_oi'], r['ce_vol'], r['ce_ltp'], r['pe_oi'], r['pe_vol'], r['pe_ltp'], r['ce_oich'], r['ce_oichp'], r['pe_oich'], r['pe_oichp'])
                )
        
        # --- TELEGRAM VOLUME SPIKE ALERTS ---
        df_flow_live = pd.read_sql_query("SELECT * FROM strike_flow", conn)
        new_alerts = check_volume_spike_alerts(
            df_flow_live, atm_strike, st.session_state.sent_alerts,
            spike_multiplier=st.session_state.get("vol_spike_mult", 3.0),
            min_volume=st.session_state.get("min_vol_lakh", 2.0) * 100000,
            dominance_ratio=st.session_state.get("side_dominance", 1.5),
            min_history=int(st.session_state.get("min_history", 5)),
            repeat_count=st.session_state.get("tg_repeat", 10),
        )
        for key, msg, severity, repeat in new_alerts:
            send_telegram(msg, repeat=repeat)
            st.session_state.sent_alerts.add(key)
            clean_msg = msg.replace("<b>","").replace("</b>","")
            st.session_state.alert_log.insert(0, {
                "time": get_ist_now().strftime("%H:%M:%S"),
                "severity": severity,
                "message": clean_msg
            })
            st.session_state.alert_log = st.session_state.alert_log[:50]  # keep last 50
            st.toast(clean_msg[:120], icon="🚨")

        df_history = pd.read_sql_query("SELECT * FROM flow_history ORDER BY timestamp ASC", conn)
        df_flow = pd.read_sql_query("SELECT * FROM strike_flow", conn)
        conn.close()
    except Exception as e:
        st.error(f"🚨 Database Engine Failure: {e}"); st.stop()

else:
    # =====================================================================
    # FILE LOOKBACK CORE ENGINE NODE
    # =====================================================================
    with st.sidebar:
        st.header("🗄️ Drop Session Database")
        uploaded_backup = st.file_uploader("Upload your saved target Nifty_Data_.db file here", type=["db"])
        
    if uploaded_backup is not None:
        try:
            with open("temp_lookback.db", "wb") as f:
                f.write(uploaded_backup.getbuffer())
                
            lite_conn = sqlite3.connect("temp_lookback.db")
            df_flow = pd.read_sql_query("SELECT * FROM strike_flow", lite_conn)
            
            # Patcher: Ensure old DB files don't crash the new metric engine
            for col in ['ce_oich', 'ce_oichp', 'pe_oich', 'pe_oichp']:
                if col not in df_flow.columns: df_flow[col] = 0.0
                
            df_history = pd.read_sql_query("SELECT * FROM flow_history ORDER BY timestamp ASC", lite_conn)
            lite_conn.close()
            os.remove("temp_lookback.db")
            
            offline_data_ready = True
            st.sidebar.success("📊 Session log data loaded into context memory successfully!")
        except Exception as lite_ex:
            st.sidebar.error(f"Failed to read file asset wrapper elements: {lite_ex}")
            st.stop()
    else:
        st.info("📁 System Status: Lookback active. Please provide a compiled session `.db` asset via sidebar loader.")
        st.stop()

# =====================================================================
# UNIFIED MATHEMATICAL MODELING LAYER
# =====================================================================
df_history['timestamp'] = pd.to_datetime(df_history['timestamp'])
df_flow['timestamp'] = pd.to_datetime(df_flow['timestamp'])
unique_times = np.sort(df_flow['timestamp'].unique())

if len(unique_times) < 2:
    st.info("🕒 Gathering baseline data. ATM ±10 matrices will print upon the next 1-minute iteration step.")
    st.stop()

t_current, t_prev = unique_times[-1], unique_times[-2]
df_curr = df_flow[df_flow['timestamp'] == t_current].set_index('strike')
df_prev = df_flow[df_flow['timestamp'] == t_prev].set_index('strike')
df_day_first = df_flow.sort_values('timestamp').groupby('strike', as_index=True).first()

def oi_change_from_day_start(strike, side, current_oi, api_change, api_pct):
    if pd.notna(api_change) and float(api_change) != 0:
        return float(api_change), float(api_pct) if pd.notna(api_pct) else 0.0

    first_col = f"{side}_oi"
    if strike not in df_day_first.index or first_col not in df_day_first.columns:
        return 0.0, 0.0

    first_oi = df_day_first.loc[strike, first_col]
    if pd.isna(first_oi) or float(first_oi) == 0:
        return 0.0, 0.0

    change = float(current_oi) - float(first_oi)
    pct = (change / float(first_oi)) * 100
    return change, pct

if app_mode == "📁 Offline DB File Lookback":
    target_strikes = sorted(df_flow['strike'].unique())
    atm_strike = target_strikes[len(target_strikes)//2] 
    nifty_spot = df_curr.loc[atm_strike, 'ce_ltp'] if atm_strike in df_curr.index else 0.0 

target_strikes = [atm_strike + (i * 50) for i in range(-10, 11)]
target_strikes.sort(reverse=True)

# Generate Exact Metric Grid Mappings
display_rows = []
for strike in target_strikes:
    is_atm = (strike == atm_strike)
    
    # 1. PULL CALL METRICS DIRECTLY FROM DAILY API FIELDS
    ce_vol = df_curr.loc[strike, 'ce_vol'] if strike in df_curr.index else 0
    ce_oi = df_curr.loc[strike, 'ce_oi'] if strike in df_curr.index else 0
    ce_oi_chg = df_curr.loc[strike, 'ce_oich'] if ('ce_oich' in df_curr.columns and strike in df_curr.index) else 0
    ce_oi_pct = df_curr.loc[strike, 'ce_oichp'] if ('ce_oichp' in df_curr.columns and strike in df_curr.index) else 0
    ce_oi_chg, ce_oi_pct = oi_change_from_day_start(strike, 'ce', ce_oi, ce_oi_chg, ce_oi_pct)
    ce_traded = ce_vol / LOT_SIZE_NIFTY # Fyers Returns Shares. Convert to Lots (Contracts).
    
    # 2. PULL PUT METRICS DIRECTLY FROM DAILY API FIELDS
    pe_vol = df_curr.loc[strike, 'pe_vol'] if strike in df_curr.index else 0
    pe_oi = df_curr.loc[strike, 'pe_oi'] if strike in df_curr.index else 0
    pe_oi_chg = df_curr.loc[strike, 'pe_oich'] if ('pe_oich' in df_curr.columns and strike in df_curr.index) else 0
    pe_oi_pct = df_curr.loc[strike, 'pe_oichp'] if ('pe_oichp' in df_curr.columns and strike in df_curr.index) else 0
    pe_oi_chg, pe_oi_pct = oi_change_from_day_start(strike, 'pe', pe_oi, pe_oi_chg, pe_oi_pct)
    pe_traded = pe_vol / LOT_SIZE_NIFTY
    
    display_rows.append({
        "CE Traded Contracts": format_indian_num(ce_traded),
        "CE OI % Chg": format_percentage(ce_oi_pct),
        "CE OI Change": format_indian_num(ce_oi_chg),
        "CE Open Interest": format_indian_num(ce_oi),
        "CE Volumes": format_indian_num(ce_vol),
        
        "⚡ STRIKE ⚡": f"🎯 {int(strike)} (ATM)" if is_atm else f"{int(strike)}",
        
        "PE Volumes": format_indian_num(pe_vol),
        "PE Open Interest": format_indian_num(pe_oi),
        "PE OI Change": format_indian_num(pe_oi_chg),
        "PE OI % Chg": format_percentage(pe_oi_pct),
        "PE Traded Contracts": format_indian_num(pe_traded),
    })

df_display_matrix = pd.DataFrame(display_rows)

narrative_data = []
for i in range(1, len(df_history)):
    prev, curr = df_history.iloc[i-1], df_history.iloc[i]
    delta_ce = curr['total_ce_oi'] - prev['total_ce_oi']
    delta_pe = curr['total_pe_oi'] - prev['total_pe_oi']
    if delta_ce > delta_pe and delta_ce > 0: bias = "🐻 Call Writers Dominating (Bearish Block)"
    elif delta_pe > delta_ce and delta_pe > 0: bias = "🐂 Put Writers Dominating (Bullish Support)"
    elif delta_ce < 0 and delta_pe < 0: bias = "🌪️ Unwinding / Panic Covering"
    else: bias = "⚖️ Neutral Flow"

    narrative_data.append({
        "Interval Window": f"{prev['timestamp'].strftime('%H:%M')} ➔ {curr['timestamp'].strftime('%H:%M')}",
        "Δ Total Call OI": f"+{int(delta_ce):,}" if delta_ce > 0 else f"{int(delta_ce):,}",
        "Δ Total Put OI": f"+{int(delta_pe):,}" if delta_pe > 0 else f"{int(delta_pe):,}",
        "Market Narrative": bias
    })
df_narrative = pd.DataFrame(narrative_data).iloc[::-1]

# =====================================================================
# DASHBOARD RENDERING INTERFACE
# =====================================================================
if app_mode == "📁 Offline DB File Lookback":
    st.warning(f"🕒 OFFLINE SIMULATION CONTEXT INTERFACE. Displaying metrics captured directly from your loaded file backup.")

col_b, col_backup = st.columns([7, 3])
with col_b:
    st.subheader("📊 Core Market Architecture")
    ui_col1, ui_col2, ui_col3 = st.columns(3)
    ui_col1.dataframe(pd.DataFrame({"Parameter Anchor": ["Spot Anchor Reference", "Calculated ATM Node"], "Values": [f"₹{nifty_spot:,.2f}", f"Strike {atm_strike}"]}), hide_index=True)

with col_backup:
    if app_mode == "🔴 Live Exchange Node":
        st.subheader("📦 Automated Safe Backup")
        if st.button("Transmit Complete Session Dump .db to Telegram", use_container_width=True):
            supabase_uri = get_secret_value("SUPABASE_URI", DEFAULT_SUPABASE_URI)
            if not supabase_uri:
                st.error("Database is not configured. Add SUPABASE_URI to Streamlit secrets before sending a backup.")
            else:
                backup_and_send_telegram(psycopg2.connect(supabase_uri))

st.markdown("---")

# ── LIVE ALERT FEED PANEL ─────────────────────────────────────────────────
if st.session_state.get("alert_log"):
    st.markdown("### 🚨 Live Alert Feed")
    severity_colors = {"CRITICAL": "#ff2222", "HIGH": "#ff8c00", "MEDIUM": "#ffd700"}
    severity_bg     = {"CRITICAL": "#2a0000", "HIGH": "#1a0a00", "MEDIUM": "#1a1500"}
    for a in st.session_state.alert_log[:10]:  # show last 10
        sev   = a.get("severity", "MEDIUM")
        color = severity_colors.get(sev, "#ffd700")
        bg    = severity_bg.get(sev, "#111")
        lines = a["message"].split("\n")
        title = lines[0] if lines else a["message"]
        body  = " | ".join(lines[1:]) if len(lines) > 1 else ""
        st.markdown(
            f"""<div style="background:{bg};border-left:4px solid {color};
            padding:8px 14px;border-radius:4px;margin-bottom:6px;">
            <span style="color:{color};font-weight:bold;font-size:13px;">[{a['time']}] {title}</span><br>
            <span style="color:#ccc;font-size:12px;">{body}</span>
            </div>""", unsafe_allow_html=True)
    st.markdown("---")

st.subheader("🔬 NIFTY ATM ±10 Matrix (1-Min Auto-Refresh)")
if not df_display_matrix.empty:
    def style_option_chain(st_df):
        return st_df.style.map(
            lambda x: 'font-weight: bold; font-size: 15px;' if '🎯' in str(x) else '',
            subset=['⚡ STRIKE ⚡']
        ).map(
            lambda x: 'color: #4ade80' if '+' in str(x) else ('color: #f87171' if '-' in str(x) and str(x) != '0' else ''),
            subset=['CE OI % Chg', 'PE OI % Chg']
        )
    
    st.dataframe(
        style_option_chain(df_display_matrix), 
        use_container_width=True, 
        hide_index=True
    )

st.markdown("---")

st.subheader("📖 The Institutional Narrative (Shift Story)")
if not df_narrative.empty:
    st.dataframe(df_narrative.style.map(color_coding, subset=['Δ Total Call OI', 'Δ Total Put OI']), use_container_width=True, hide_index=True)

st.markdown("---")

st.markdown("### 🔍 Interactive Strike Cascades (Modal Windows)")
grid_container = st.container()
with grid_container:
    cols_per_row = 4
    button_strikes = sorted(target_strikes)
    for i in range(0, len(button_strikes), cols_per_row):
        row_strikes = button_strikes[i:i+cols_per_row]
        btn_cols = st.columns(cols_per_row)
        for idx, selected_strike in enumerate(row_strikes):
            is_atm_anchor = (selected_strike == atm_strike)
            label = f"🎯 Strike {int(selected_strike)} (ATM)" if is_atm_anchor else f"🔢 Strike {int(selected_strike)}"
            with btn_cols[idx]:
                if st.button(label, key=f"popup_btn_{selected_strike}", use_container_width=True):
                    show_strike_popup(selected_strike, df_flow, is_atm_anchor)
