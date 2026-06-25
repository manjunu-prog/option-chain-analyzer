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
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# Suppress pandas warning for using raw psycopg2 connection
warnings.filterwarnings('ignore', category=UserWarning)

# --- CONFIGURATION & SESSION INITIALIZATION ---
st.set_page_config(page_title="SENSEX Options Quant Matrix", layout="wide")

# Run the autorefresh every 3 minutes (180,000 milliseconds)
st_autorefresh(interval=180000, key="matrix_autorefresh")

st.markdown("""
    <style>
        .block-container { padding-top: 2rem; padding-bottom: 2rem; }
        .stMetric { background-color: rgba(255, 255, 255, 0.05); padding: 15px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.1); }
        div.stButton > button:first-child { width: 100%; margin-top: 10px; }
    </style>
""", unsafe_allow_html=True)

MAX_LOTS_ALLOWED = 4
LOT_SIZE_SENSEX = 10  
LAST_ENTRY_TIME = datetime.time(14, 0)
PRODUCT_TYPE = "NRML"
ORDER_TYPE = 2       

# --- IST TIMEZONE OVERRIDE ---
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def get_ist_now():
    return datetime.datetime.now(IST).replace(tzinfo=None)

if "fyers_instance" not in st.session_state:
    st.session_state.fyers_instance = None
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# --- SENSEX 30 CONSTITUENT ARRAYS ---
TOP_5_SYMBOLS = ["BSE:HDFCBANK-EQ", "BSE:RELIANCE-EQ", "BSE:ICICIBANK-EQ", "BSE:INFY-EQ", "BSE:TCS-EQ"]
NEXT_10_SYMBOLS = ["BSE:ITC-EQ", "BSE:LT-EQ", "BSE:KOTAKBANK-EQ", "BSE:AXISBANK-EQ", "BSE:SBIN-EQ", "BSE:BHARTIARTL-EQ", "BSE:BAJFINANCE-EQ", "BSE:HINDUNILVR-EQ", "BSE:M&M-EQ", "BSE:MARUTI-EQ"]
REMAINING_15_SYMBOLS = ["BSE:SUNPHARMA-EQ", "BSE:HCLTECH-EQ", "BSE:TATAMOTORS-EQ", "BSE:TATASTEEL-EQ", "BSE:NTPC-EQ", "BSE:POWERGRID-EQ", "BSE:TITAN-EQ", "BSE:ULTRACEMCO-EQ", "BSE:ASIANPAINT-EQ", "BSE:JSWSTEEL-EQ", "BSE:INDUSINDBK-EQ", "BSE:BAJAJFINSV-EQ", "BSE:NESTLEIND-EQ", "BSE:TECHM-EQ", "BSE:WIPRO-EQ"]

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

def color_coding(val):
    color = ''
    if isinstance(val, str):
        if val.startswith('+') or "🟢" in val or "BUY" in val or "🔥" in val: color = '#4ade80' 
        elif val.startswith('-') or "🔴" in val or "SHORTS" in val or "🚨" in val: color = '#f87171' 
    return f'color: {color}' if color else ''

# --- INSTANT TELEGRAM NOTIFICATION DISPATCHER ---
def send_instant_telegram_alert(strike, opt_type, delta_vol, current_ltp, is_all_time_high=False):
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN")
        chat_id = st.secrets.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
            
        title = "🚀 SENSEX RECORD DAILY VOLUME BREAKOUT 🚀" if is_all_time_high else "⚡ SENSEX INSTITUTIONAL VOLUME SPIKE ⚡"
        
        message = (
            f"**{title}**\n\n"
            f"🎯 **Strike:** SENSEX {strike} {opt_type}\n"
            f"📈 **Volume Surge (3m):** {delta_vol:,} Contracts\n"
            f"💰 **Current LTP:** ₹{current_ltp:.2f}\n"
            f"⏰ **Time:** {get_ist_now().strftime('%H:%M:%S')} IST\n\n"
            f"🔥 *Warning: High momentum institutional blockbuster activity detected!*"
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})
    except Exception:
        pass

# --- STRICT INSTITUTIONAL MACRO BREAKOUT INTERPRETER ---
def calculate_progressive_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo, strike_context=None, ltp_context=None, live_dispatch=False):
    
    # Process circle stacks based on pure, filtered option contract bars matching your charts
    ce_circles = 0
    if d_ce_vo >= 3500000:   # 3.5M+ Contracts Critical Peak (Your yellow highlighted breakout lines)
        ce_circles = 4
    elif d_ce_vo >= 1500000: # 1.5M+ Contracts High Target
        ce_circles = 3
    elif d_ce_vo >= 500000:  # 500k+ Contracts Baseline Surge
        ce_circles = 2

    pe_circles = 0
    if d_pe_vo >= 3500000:   # 3.5M+ Contracts Critical Peak (Your yellow highlighted breakout lines)
        pe_circles = 4
    elif d_pe_vo >= 1500000: # 1.5M+ Contracts High Target
        pe_circles = 3
    elif d_pe_vo >= 500000:  # 500k+ Contracts Baseline Surge
        pe_circles = 2

    # Handle Active Telegram Alerts and UI Overrides for 3.5M+ Breakout Anomalies
    if pe_circles == 4 and d_pe_vo > d_ce_vo:
        if live_dispatch and strike_context and ltp_context:
            send_instant_telegram_alert(strike_context, "PE", d_pe_vo, ltp_context[1], is_all_time_high=True)
        return "🔴🔴🔴🔴 🚨 SENSEX PE BREAKOUT HIGH"

    if ce_circles == 4 and d_ce_vo > d_pe_vo:
        if live_dispatch and strike_context and ltp_context:
            send_instant_telegram_alert(strike_context, "CE", d_ce_vo, ltp_context[0], is_all_time_high=True)
        return "🟢🟢🟢🟢 🔥 SENSEX CE BREAKOUT HIGH"

    # Standard directional logging (No circles for normal baseline noise under 500k contracts)
    if d_ce_oi > d_pe_oi and d_pe_vo > d_ce_vo:
        if live_dispatch and pe_circles >= 3 and strike_context and ltp_context:
            send_instant_telegram_alert(strike_context, "PE", d_pe_vo, ltp_context[1], is_all_time_high=False)
        return f"{'🔴' * pe_circles} PE BUY" if pe_circles >= 2 else "🔴 PE BUY"
        
    elif d_pe_oi > d_ce_oi and d_ce_vo > d_pe_vo:
        if live_dispatch and ce_circles >= 3 and strike_context and ltp_context:
            send_instant_telegram_alert(strike_context, "CE", d_ce_vo, ltp_context[0], is_all_time_high=False)
        return f"{'🟢' * ce_circles} CE BUY" if ce_circles >= 2 else "🟢 CE BUY"
        
    elif d_ce_oi > d_pe_oi and d_ce_vo > d_pe_vo:
        return "📉 CE SHORTS"
    elif d_pe_oi > d_ce_oi and d_pe_vo > d_ce_vo:
        return "📈 PE SHORTS"
        
    if d_ce_oi == 0 and d_pe_oi == 0 and d_ce_vo == 0 and d_pe_vo == 0:
        return "⚖️ NO FLOW"
    return "⚖️ NEUTRAL"

# --- POP-UP MODAL TIMELINE ENGINE ---
@st.dialog("📋 Combined Intel Timeline Ledger", width="large")
def show_strike_popup(strike, df_flow, is_atm_anchor):
    title_decorator = f"🎯 Strike {strike} (ATM)" if is_atm_anchor else f"Strike {strike}"
    st.subheader(f"Order Flow Analysis Matrix for {title_decorator}")
    st.caption("Chronological 3-minute interval snapshots showing true cleaned institutional velocity surges.")
    
    df_st = df_flow[df_flow['strike'] == strike].copy().sort_values('timestamp', ascending=True)
    df_st['d_ce_oi'] = df_st['ce_oi'].diff().fillna(0).astype(int)
    df_st['d_pe_oi'] = df_st['pe_oi'].diff().fillna(0).astype(int)
    
    # FIX: Isolate interval volume correctly by scaling the metrics down to contract bars
    df_st['d_ce_vo'] = (df_st['ce_vol'].diff().fillna(0) / 100).astype(int)
    df_st['d_pe_vo'] = (df_st['pe_vol'].diff().fillna(0) / 100).astype(int)
    
    signals = []
    df_st_reset = df_st.reset_index(drop=True)
    for idx, row in df_st_reset.iterrows():
        signals.append(calculate_progressive_signal(row['d_ce_oi'], row['d_pe_oi'], row['d_ce_vo'], row['d_pe_vo'], strike, [row['ce_ltp'], row['pe_ltp']], live_dispatch=False))
    
    df_st_reset['🎯 ACTION SIGNAL'] = signals
    df_st_processed = df_st_reset.sort_values('timestamp', ascending=False)
    df_st_processed['Time'] = df_st_processed['timestamp'].dt.strftime('%H:%M:%S %p')
    
    df_render = pd.DataFrame()
    df_render['Timestamp'] = df_st_processed['Time']
    df_render['🎯 ACTION SIGNAL'] = df_st_processed['🎯 ACTION SIGNAL']
    df_render['Change in OI - CE'] = df_st_processed['d_ce_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in OI - PE'] = df_st_processed['d_pe_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in Vol - CE'] = df_st_processed['d_ce_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in Vol - PE'] = df_st_processed['d_pe_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    
    styled_popup = df_render.style.map(
        color_coding, 
        subset=['🎯 ACTION SIGNAL', 'Change in OI - CE', 'Change in OI - PE', 'Change in Vol - CE', 'Change in Vol - PE']
    )
    st.dataframe(styled_popup, use_container_width=True, hide_index=True)

# --- BACKUP DUMP TO TELEGRAM CHANNELS ---
def backup_and_send_telegram(supabase_conn):
    try:
        st.info("🔄 Compiling database records into local SQLite binary snapshot...")
        df_flow_export = pd.read_sql_query("SELECT * FROM sensex_strike_flow", supabase_conn)
        df_hist_export = pd.read_sql_query("SELECT * FROM sensex_flow_history", supabase_conn)
        
        filename = f"Sensex_Data_{get_ist_now().strftime('%Y-%m-%d')}.db"
        lite_conn = sqlite3.connect(filename)
        df_flow_export.to_sql("sensex_strike_flow", lite_conn, if_exists="replace", index=False)
        df_hist_export.to_sql("sensex_flow_history", lite_conn, if_exists="replace", index=False)
        lite_conn.close()
        
        token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        
        with open(filename, "rb") as db_file:
            payload = {"chat_id": chat_id, "caption": f"📂 Intraday SENSEX Quantum Matrix Backup File\n📅 Date: {get_ist_now().strftime('%Y-%m-%d')}"}
            files = {"document": db_file}
            response = requests.post(url, data=payload, files=files)
            
        if os.path.exists(filename): os.remove(filename)
        if response.json().get("ok"): st.success("🎯 Backup .db transmitted to Telegram account successfully!")
        else: st.error(f"Telegram Failure: {response.json()}")
    except Exception as ex: st.error(f"Backup Error: {ex}")

# =====================================================================
# SIDEBAR NAVIGATION INTERFACE: MODE SELECTOR SWITCH
# =====================================================================
with st.sidebar:
    st.header("🎛️ Operational Mode Matrix")
    app_mode = st.radio("Select Active Core Node Environment", ["🔴 Live Exchange Node", "📁 Offline DB File Lookback"])
    st.markdown("---")

offline_data_ready = False
df_history, df_flow = pd.DataFrame(), pd.DataFrame()

if app_mode == "🔴 Live Exchange Node":
    with st.sidebar:
        st.header("Gateway Security Credentials")
        input_fy_id = st.text_input("Fyers ID", value="FAJ88605")
        input_pin = st.text_input("Security PIN", value="4089", type="password")
        input_totp = st.text_input("TOTP Seed Key", value="ZHOQNKKVMI7IRCAPUFX7OXRMPFXRYVU6", type="password")
        input_app_id = st.text_input("App ID Parameter", value="Q3B2S22L5M")
        input_app_secret = st.text_input("Client Secret Key", value="PWZD03ONQ4", type="password")
        input_redirect = st.text_input("Redirect URI End-point", value="https://trade.fyers.in/api-login/redirect-uri/index.html")
        
        if st.button("Establish Production Gateway"):
            with st.spinner("Connecting server clusters to exchange node..."):
                token = execute_auto_login(input_fy_id, input_pin, input_totp, input_app_id, "100", input_app_secret, input_redirect)
                if token:
                    st.session_state.fyers_instance = fyersModel.FyersModel(client_id=f"{input_app_id}-100", token=token, is_async=False, log_path="")
                    st.session_state.authenticated = True
                    st.success("Synchronized successfully. Node pipelines online.")
                else: st.session_state.authenticated = False

    if not st.session_state.authenticated or st.session_state.fyers_instance is None:
        st.sidebar.info("⚡ System status: Awaiting secure initialization parameters.")
        st.stop()

    fyers = st.session_state.fyers_instance

    # --- PULL REALTIME PIPELINES (BSE SENSEX) ---
    batch_1 = ["BSE:SENSEX-INDEX", "NSE:INDIAVIX-INDEX"] + TOP_5_SYMBOLS + NEXT_10_SYMBOLS
    batch_2 = REMAINING_15_SYMBOLS
    spot_raw = {**get_live_quotes(fyers, batch_1), **get_live_quotes(fyers, batch_2)}

    if not spot_raw or "BSE:SENSEX-INDEX" not in spot_raw:
        st.error("LIVE DATA NOT AVAILABLE — NO TRADE (Sensex spot fetch failed)")
        st.stop()

    sensex_spot = float(spot_raw["BSE:SENSEX-INDEX"]["lp"])
    open_price = float(spot_raw["BSE:SENSEX-INDEX"]["open_price"])
    prev_close = float(spot_raw["BSE:SENSEX-INDEX"]["prev_close_price"])
    atm_strike = round(sensex_spot / 100) * 100  

    vix_data = spot_raw.get("NSE:INDIAVIX-INDEX", {})
    vix_lp, vix_prev = float(vix_data.get("lp", 15.0)), float(vix_data.get("prev_close_price", 15.0))
    vix_pct_change = ((vix_lp - vix_prev) / vix_prev) * 100 if vix_prev > 0 else 0.0

    top5_adv = sum(1 for s in TOP_5_SYMBOLS if s in spot_raw and float(spot_raw[s].get("lp", 0)) >= float(spot_raw[s].get("prev_close_price", 0)))
    next10_adv = sum(1 for s in NEXT_10_SYMBOLS if s in spot_raw and float(spot_raw[s].get("lp", 0)) >= float(spot_raw[s].get("prev_close_price", 0)))
    rem15_adv = sum(1 for s in REMAINING_15_SYMBOLS if s in spot_raw and float(spot_raw[s].get("lp", 0)) >= float(spot_raw[s].get("prev_close_price", 0)))
    top15_adv, sensex30_adv = top5_adv + next10_adv, top5_adv + next10_adv + rem15_adv

    chain_response = fyers.optionchain(data={"symbol": "BSE:SENSEX-INDEX", "strikecount": 15, "timestamp": "", "greeks": "1"})
    if not chain_response or chain_response.get("s") != "ok":
        st.error("LIVE DATA NOT AVAILABLE (Sensex option chain API failed)")
        st.stop()

    chain_data = chain_response.get("data", {})
    options_list = chain_data.get("optionsChain", [])

    total_ce_oi, total_pe_oi, total_ce_vol, total_pe_vol = 0, 0, 0, 0
    strike_oi_totals, current_strike_data = {}, []
    target_strikes = [atm_strike + (i * 100) for i in range(-5, 6)]

    for contract in options_list:
        opt_type, strike = contract.get("option_type"), contract.get("strike_price")
        oi_val, vol_val, ltp_val = int(contract.get("oi", 0)), int(contract.get("volume", 0)), float(contract.get("ltp", 0.0))
        strike_oi_totals[strike] = strike_oi_totals.get(strike, 0) + oi_val
        
        match = next((d for d in current_strike_data if d['strike'] == strike), None)
        if not match:
            match = {"strike": strike, "ce_oi": 0, "ce_vol": 0, "ce_ltp": 0.0, "pe_oi": 0, "pe_vol": 0, "pe_ltp": 0.0}
            current_strike_data.append(match)
        if opt_type == "CE":
            match['ce_oi'], match['ce_vol'], match['ce_ltp'] = oi_val, vol_val, ltp_val
            total_ce_oi += oi_val; total_ce_vol += vol_val
        else:
            match['pe_oi'], match['pe_vol'], match['pe_ltp'] = oi_val, vol_val, ltp_val
            total_pe_oi += oi_val; total_pe_vol += vol_val

    atm_call_contract = next((c for c in options_list if c.get("option_type") == "CE" and c.get("strike_price") == atm_strike), None)
    matched_put_contract = min([c for c in options_list if c.get("option_type") == "PE"], key=lambda x: abs(float(x.get("ltp", 0)) - float(atm_call_contract.get("ltp", 0))))
    matched_put_strike = matched_put_contract.get("strike_price")

    try:
        conn = psycopg2.connect(st.secrets["SUPABASE_URI"]); conn.autocommit = True; c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS sensex_flow_history (timestamp TIMESTAMP, total_ce_oi BIGINT, total_pe_oi BIGINT, atm_ce_oi BIGINT, atm_pe_oi BIGINT)")
        c.execute("CREATE TABLE IF NOT EXISTS sensex_strike_flow (timestamp TIMESTAMP, strike INTEGER, ce_oi BIGINT, ce_vol BIGINT, ce_ltp REAL, pe_oi BIGINT, pe_vol BIGINT, pe_ltp REAL)")
        
        c.execute("SELECT timestamp FROM sensex_flow_history ORDER BY timestamp DESC LIMIT 1")
        last_entry = c.fetchone()
        if last_entry and last_entry[0].date() != get_ist_now().date():
            c.execute("TRUNCATE TABLE sensex_flow_history; TRUNCATE TABLE sensex_strike_flow;")

        current_time = get_ist_now()
        c.execute("INSERT INTO sensex_flow_history VALUES (%s, %s, %s, %s, %s)", (current_time, total_ce_oi, total_pe_oi, int(atm_call_contract.get("oi",0)), int(matched_put_contract.get("oi",0))))
        for r in current_strike_data:
            if r['strike'] in target_strikes:
                c.execute("INSERT INTO sensex_strike_flow VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (current_time, r['strike'], r['ce_oi'], r['ce_vol'], r['ce_ltp'], r['pe_oi'], r['pe_vol'], r['pe_ltp']))
        
        df_history = pd.read_sql_query("SELECT * FROM sensex_flow_history ORDER BY timestamp ASC", conn)
        df_flow = pd.read_sql_query("SELECT * FROM sensex_strike_flow", conn)
        conn.close()
    except Exception as e:
        st.error(f"🚨 Database Engine Failure: {e}"); st.stop()

else:
    # =====================================================================
    # FILE LOOKBACK CORE ENGINE NODE
    # =====================================================================
    with st.sidebar:
        st.header("🗄️ Drop Session Database")
        uploaded_backup = st.file_uploader("Upload your saved target Sensex_Data_.db file here", type=["db"])
        
    if uploaded_backup is not None:
        try:
            with open("temp_lookback_sensex.db", "wb") as f:
                f.write(uploaded_backup.getbuffer())
                
            lite_conn = sqlite3.connect("temp_lookback_sensex.db")
            df_flow = pd.read_sql_query("SELECT * FROM sensex_strike_flow", lite_conn)
            df_history = pd.read_sql_query("SELECT * FROM sensex_flow_history ORDER BY timestamp ASC", lite_conn)
            lite_conn.close()
            os.remove("temp_lookback_sensex.db")
            
            offline_data_ready = True
            st.sidebar.success("📊 Session log data loaded into context memory successfully!")
        except Exception as lite_ex:
            st.sidebar.error(f"Failed to read file asset wrapper elements: {lite_ex}")
            st.stop()
    else:
        st.info("📁 System Status: Lookback active. Please provide a compiled session `.db` asset via sidebar loader.")
        st.stop()

# =====================================================================
# UNIFIED MATHEMATICAL MODELING LAYER (SHARED BY BOTH MODES)
# =====================================================================
df_history['timestamp'] = pd.to_datetime(df_history['timestamp'])
df_flow['timestamp'] = pd.to_datetime(df_flow['timestamp'])
unique_times = np.sort(df_flow['timestamp'].unique())

if len(unique_times) < 2:
    st.info("🕒 Gathering baseline data. Tables print upon next 3-minute iteration loop step.")
    st.stop()

t_current, t_prev = unique_times[-1], unique_times[-2]
df_curr = df_flow[df_flow['timestamp'] == t_current].set_index('strike')
df_prev = df_flow[df_flow['timestamp'] == t_prev].set_index('strike')

if app_mode == "📁 Offline DB File Lookback":
    target_strikes = sorted(df_flow['strike'].unique())
    atm_strike = target_strikes[len(target_strikes)//2] 
    sensex_spot = df_curr.loc[atm_strike, 'ce_ltp'] if atm_strike in df_curr.index else 0.0 
    open_price, prev_close, vix_pct_change, sensex30_adv, vix_lp, dte = 0.0, 0.0, 0.0, 0, 15.0, 1
    total_ce_vol = df_curr['ce_vol'].sum()
    total_pe_vol = df_curr['pe_vol'].sum()
    vol_dominance = "PE Dominance" if total_pe_vol > total_ce_vol else "CE Dominance"

# --- CALCULATE REALISTIC INTERVAL CONTRACT COUNT VALUES ---
df_delta = pd.DataFrame(index=target_strikes)
df_delta['Δ CE Vol'] = ((df_curr['ce_vol'] - df_prev['ce_vol']).fillna(0) / 100).astype(int)
df_delta['Δ CE OI'] = df_curr['ce_oi'] - df_prev['ce_oi']
df_delta['Δ CE LTP'] = (df_curr['ce_ltp'] - df_prev['ce_ltp']).round(2)
df_delta['Strike (ATM: ' + str(atm_strike) + ')'] = df_delta.index
df_delta['Δ PE LTP'] = (df_curr['pe_ltp'] - df_prev['pe_ltp']).round(2)
df_delta['Δ PE OI'] = df_curr['pe_oi'] - df_prev['pe_oi']
df_delta['Δ PE Vol'] = ((df_curr['pe_vol'] - df_prev['pe_vol']).fillna(0) / 100).astype(int)

numeric_delta = df_delta.copy()

active_row_signals = []
for strike_idx in target_strikes:
    d_ce_oi = df_delta.loc[strike_idx, 'Δ CE OI'] if strike_idx in df_delta.index else 0
    d_pe_oi = df_delta.loc[strike_idx, 'Δ PE OI'] if strike_idx in df_delta.index else 0
    d_ce_vo = df_delta.loc[strike_idx, 'Δ CE Vol'] if strike_idx in df_delta.index else 0
    d_pe_vo = df_delta.loc[strike_idx, 'Δ PE Vol'] if strike_idx in df_delta.index else 0
    
    curr_ce_ltp = df_curr.loc[strike_idx, 'ce_ltp'] if strike_idx in df_curr.index else 0.0
    curr_pe_ltp = df_curr.loc[strike_idx, 'pe_ltp'] if strike_idx in df_curr.index else 0.0
    
    is_live_loop = (app_mode == "🔴 Live Exchange Node")
    active_row_signals.append(calculate_progressive_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo, strike_idx, [curr_ce_ltp, curr_pe_ltp], live_dispatch=is_live_loop))

df_delta['🎯 ACTIVE RADAR SIGNAL'] = active_row_signals

for col in ['Δ CE Vol', 'Δ CE OI', 'Δ PE OI', 'Δ PE Vol']:
    df_delta[col] = df_delta[col].fillna(0).apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
for col in ['Δ CE LTP', 'Δ PE LTP']:
    df_delta[col] = df_delta[col].fillna(0).apply(lambda x: f"+{x:,.2f}" if x > 0 else (f"{x:,.2f}" if x < 0 else "0.00"))

final_micro_cols = ['Strike (ATM: ' + str(atm_strike) + ')', '🎯 ACTIVE RADAR SIGNAL', 'Δ CE OI', 'Δ CE Vol', 'Δ CE LTP', 'Δ PE OI', 'Δ PE Vol', 'Δ PE LTP']
df_micro_structure = df_delta[final_micro_cols].reset_index(drop=True)

# Generate Narrative Mappings
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
# DASHBOARD RENDERING INTERFACE (DYNAMIC DISPLAY BLOCKS)
# =====================================================================
if app_mode == "📁 Offline DB File Lookback":
    st.warning(f"🕒 OFFLINE SIMULATION CONTEXT INTERFACE. Displaying metrics captured directly from your loaded file backup.")

col_b, col_backup = st.columns([7, 3])
with col_b:
    st.subheader("📊 Core SENSEX Market Architecture")
    ui_col1, ui_col2, ui_col3 = st.columns(3)
    ui_col1.dataframe(pd.DataFrame({"Parameter Anchor": ["SENSEX Spot Anchor", "Calculated ATM Node"], "Values": [f"₹{sensex_spot:,.2f}", f"Strike {atm_strike}"]}), hide_index=True)

with col_backup:
    if app_mode == "🔴 Live Exchange Node":
        st.subheader("📦 Automated Safe Backup")
        if st.button("Transmit Complete Session Dump .db to Telegram", use_container_width=True):
            backup_and_send_telegram(psycopg2.connect(st.secrets["SUPABASE_URI"]))

st.markdown("---")

# --- THE INSTITUTIONAL SHIFT STORY ROW ---
st.subheader("📖 The Institutional Narrative (Shift Story)")
if not df_narrative.empty:
    st.dataframe(df_narrative.style.map(color_coding, subset=['Δ Total Call OI', 'Δ Total Put OI']), use_container_width=True, hide_index=True)

st.markdown("---")

# --- THE MASTER REALTIME OPTION FLOW GRID ---
st.subheader("🔬 SENSEX Micro-Structure Strike Tracker (ATM ± 5)")
if not df_micro_structure.empty:
    st.dataframe(df_micro_structure.style.map(color_coding, subset=['🎯 ACTIVE RADAR SIGNAL', 'Δ CE Vol', 'Δ CE OI', 'Δ CE LTP', 'Δ PE LTP', 'Δ PE OI', 'Δ PE Vol']), use_container_width=True, hide_index=True)

    st.markdown("### 🔍 Interactive Strike Cascades (Modal Windows)")
    grid_container = st.container()
    with grid_container:
        cols_per_row = 4
        for i in range(0, len(target_strikes), cols_per_row):
            row_strikes = target_strikes[i:i+cols_per_row]
            btn_cols = st.columns(cols_per_row)
            for idx, selected_strike in enumerate(row_strikes):
                is_atm_anchor = (selected_strike == atm_strike)
                label = f"🎯 Strike {selected_strike} (ATM)" if is_atm_anchor else f"🔢 Strike {selected_strike}"
                with btn_cols[idx]:
                    if st.button(label, key=f"popup_btn_{selected_strike}", use_container_width=True):
                        show_strike_popup(selected_strike, df_flow, is_atm_anchor)
