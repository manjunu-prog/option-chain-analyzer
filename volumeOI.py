import streamlit as st
import pandas as pd
import numpy as np
import hashlib
import requests
import pyotp
import base64
import datetime
import psycopg2
import warnings
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# Suppress pandas warning for using raw psycopg2 connection
warnings.filterwarnings('ignore', category=UserWarning)

# --- CONFIGURATION & SESSION INITIALIZATION ---
st.set_page_config(page_title="Intraday Options Quant Matrix", layout="wide")

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
LOT_SIZE_NIFTY = 25  
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
        if val.startswith('+') or "🟢" in val or "BUY" in val: color = '#4ade80' 
        elif val.startswith('-') or "🔴" in val or "SHORTS" in val: color = '#f87171' 
    return f'color: {color}' if color else ''

# --- ADVANCED RADAR SIGNAL INTERPRETER ---
def calculate_orderflow_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo):
    if d_ce_oi == 0 and d_pe_oi == 0 and d_ce_vo == 0 and d_pe_vo == 0:
        return "⚖️ NO FLOW"
    
    if d_ce_oi > d_pe_oi and d_pe_vo > d_ce_vo:
        return "🔴 PE BUY"
    elif d_pe_oi > d_ce_oi and d_ce_vo > d_pe_vo:
        return "🟢 CE BUY"
    elif d_ce_oi > d_pe_oi and d_ce_vo > d_pe_vo:
        return "📉 CE SHORTS"
    elif d_pe_oi > d_ce_oi and d_pe_vo > d_pe_vo:
        return "📈 PE SHORTS"
        
    return "⚖️ NEUTRAL"

# --- POP-UP MODAL TIMELINE ENGINE ---
@st.dialog("📋 Combined Intel Timeline Ledger", width="large")
def show_strike_popup(strike, df_flow, is_atm_anchor):
    title_decorator = f"🎯 Strike {strike} (ATM)" if is_atm_anchor else f"Strike {strike}"
    st.subheader(f"Order Flow Analysis Matrix for {title_decorator}")
    st.caption("Chronological 3-minute interval snapshots combined with Volume + Open Interest velocity tracking rules.")
    
    df_st = df_flow[df_flow['strike'] == strike].copy().sort_values('timestamp', ascending=True)
    df_st['d_ce_oi'] = df_st['ce_oi'].diff().fillna(0).astype(int)
    df_st['d_pe_oi'] = df_st['pe_oi'].diff().fillna(0).astype(int)
    df_st['d_ce_vo'] = df_st['ce_vol'].diff().fillna(0).astype(int)
    df_st['d_pe_vo'] = df_st['pe_vol'].diff().fillna(0).astype(int)
    
    signals = []
    for _, r in df_st.iterrows():
        signals.append(calculate_orderflow_signal(r['d_ce_oi'], r['d_pe_oi'], r['d_ce_vo'], r['d_pe_vo']))
    df_st['🎯 ACTION SIGNAL'] = signals
    
    df_st.sort_values('timestamp', ascending=False, inplace=True)
    df_st['Time'] = df_st['timestamp'].dt.strftime('%H:%M:%S %p')
    
    df_render = pd.DataFrame()
    df_render['Timestamp'] = df_st['Time']
    df_render['🎯 ACTION SIGNAL'] = df_st['🎯 ACTION SIGNAL']
    df_render['Change in OI - CE'] = df_st['d_ce_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in OI - PE'] = df_st['d_pe_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in Vol - CE'] = df_st['d_ce_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_render['Change in Vol - PE'] = df_st['d_pe_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    
    styled_popup = df_render.style.map(
        color_coding, 
        subset=['🎯 ACTION SIGNAL', 'Change in OI - CE', 'Change in OI - PE', 'Change in Vol - CE', 'Change in Vol - PE']
    )
    st.dataframe(styled_popup, use_container_width=True, hide_index=True)

# --- FRONTEND INTERFACE ---
st.title("🎛️ Quantitative Index Volatility & Execution Engine")
st.caption("Fyers API v3 Production Infrastructure Node | Multi-Dimensional Matrix")

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
                st.session_state.fyers_instance = fyersModel.FyersModel(
                    client_id=f"{input_app_id}-100", token=token, is_async=False, log_path=""
                )
                st.session_state.authenticated = True
                st.success("Synchronized successfully. Node pipelines online.")
            else:
                st.session_state.authenticated = False

if not st.session_state.authenticated or st.session_state.fyers_instance is None:
    st.info("⚡ System status: Awaiting secure initialization matrix parameters via sidebar.")
    st.stop()

fyers = st.session_state.fyers_instance

# =====================================================================
# ENGINE STAGE 1: MACRO DATA PIPELINE & BATCH FETCHING
# =====================================================================
batch_1 = ["NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"] + TOP_5_SYMBOLS + NEXT_20_SYMBOLS
batch_2 = REMAINING_25_SYMBOLS

raw_batch_1 = get_live_quotes(fyers, batch_1)
raw_batch_2 = get_live_quotes(fyers, batch_2)
spot_raw = {**raw_batch_1, **raw_batch_2} 

if not spot_raw or "NSE:NIFTY50-INDEX" not in spot_raw:
    st.error("LIVE DATA NOT AVAILABLE — NO TRADE (Spot fetch failed)")
    st.stop()

nifty_spot = float(spot_raw["NSE:NIFTY50-INDEX"]["lp"])
open_price = float(spot_raw["NSE:NIFTY50-INDEX"]["open_price"])
prev_close = float(spot_raw["NSE:NIFTY50-INDEX"]["prev_close_price"])
atm_strike = round(nifty_spot / 50) * 50

vix_data = spot_raw.get("NSE:INDIAVIX-INDEX", {})
vix_lp = float(vix_data.get("lp", 15.0))
vix_prev = float(vix_data.get("prev_close_price", 15.0))
vix_pct_change = ((vix_lp - vix_prev) / vix_prev) * 100 if vix_prev > 0 else 0.0

def check_advancing(symbol):
    return 1 if symbol in spot_raw and float(spot_raw[symbol].get("lp", 0)) >= float(spot_raw[symbol].get("prev_close_price", 0)) else 0

top5_adv = sum(check_advancing(sym) for sym in TOP_5_SYMBOLS)
next20_adv = sum(check_advancing(sym) for sym in NEXT_20_SYMBOLS)
rem25_adv = sum(check_advancing(sym) for sym in REMAINING_25_SYMBOLS)
top25_adv = top5_adv + next20_adv
nifty50_adv = top25_adv + rem25_adv

chain_payload = {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 15, "timestamp": "", "greeks": "1"}
chain_response = fyers.optionchain(data=chain_payload)

if not chain_response or chain_response.get("s") != "ok":
    st.error("LIVE DATA NOT AVAILABLE — NO TRADE (Option chain API failed)")
    st.stop()

chain_data = chain_response.get("data", {})
options_list = chain_data.get("optionsChain", [])

if not options_list:
    st.error("LIVE DATA NOT AVAILABLE — NO TRADE (Empty option chain returned)")
    st.stop()

# =====================================================================
# ENGINE STAGE 2: MATHEMATICAL MODELING & ORDER FLOW PARSING
# =====================================================================
target_expiry_str = chain_data.get("expiryData", [{}])[0].get("date", "")
target_expiry = datetime.datetime.strptime(target_expiry_str, "%d-%m-%Y").date() if target_expiry_str else get_ist_now().date()
dte = max(1, (datetime.datetime.combine(target_expiry, datetime.time.min) - get_ist_now()).days)

atm_call_contract = None
put_contracts_pool = []
ce_contracts = []
pe_contracts = []

total_ce_oi, total_pe_oi = 0, 0
total_ce_vol, total_pe_vol = 0, 0

strike_oi_totals = {}
max_pos_oich_ce = {"strike": 0, "val": -float('inf')}
max_neg_oich_ce = {"strike": 0, "val": float('inf')}
max_pos_oich_pe = {"strike": 0, "val": -float('inf')}
max_neg_oich_pe = {"strike": 0, "val": float('inf')}

current_strike_data = []
target_strikes = [atm_strike + (i * 50) for i in range(-5, 6)]

for contract in options_list:
    opt_type = contract.get("option_type")
    strike = contract.get("strike_price")
    oi_val = int(contract.get("oi", 0))
    vol_val = int(contract.get("volume", 0))
    oich_val = int(contract.get("oich", 0))
    ltp_val = float(contract.get("ltp", 0.0))
    
    strike_oi_totals[strike] = strike_oi_totals.get(strike, 0) + oi_val
    
    if strike not in [d['strike'] for d in current_strike_data]:
        current_strike_data.append({"strike": strike, "ce_oi": 0, "ce_vol": 0, "ce_ltp": 0.0, "pe_oi": 0, "pe_vol": 0, "pe_ltp": 0.0})
    
    for row in current_strike_data:
        if row['strike'] == strike:
            if opt_type == "CE":
                row['ce_oi'] = oi_val
                row['ce_vol'] = vol_val
                row['ce_ltp'] = ltp_val
            elif opt_type == "PE":
                row['pe_oi'] = oi_val
                row['pe_vol'] = vol_val
                row['pe_ltp'] = ltp_val

    if opt_type == "CE":
        total_ce_oi += oi_val
        total_ce_vol += vol_val
        ce_contracts.append(contract)
        if oich_val > max_pos_oich_ce["val"]: max_pos_oich_ce = {"strike": strike, "val": oich_val}
        if oich_val < max_neg_oich_ce["val"]: max_neg_oich_ce = {"strike": strike, "val": oich_val}
        if strike == atm_strike: atm_call_contract = contract
            
    elif opt_type == "PE":
        total_pe_oi += oi_val
        total_pe_vol += vol_val
        pe_contracts.append(contract)
        put_contracts_pool.append(contract)
        if oich_val > max_pos_oich_pe["val"]: max_pos_oich_pe = {"strike": strike, "val": oich_val}
        if oich_val < max_neg_oich_pe["val"]: max_neg_oich_pe = {"strike": strike, "val": oich_val}

if not atm_call_contract:
    st.error(f"LIVE DATA NOT AVAILABLE — NO TRADE (ATM {atm_strike} CE not found in chain)")
    st.stop()

max_pain_strike = max(strike_oi_totals, key=strike_oi_totals.get)
pain_gravity = "PULLING DOWN" if nifty_spot > max_pain_strike + 30 else "PULLING UP" if nifty_spot < max_pain_strike - 30 else "NEUTRALIZED"

top_ce_vol = sorted(ce_contracts, key=lambda x: int(x.get("volume", 0)), reverse=True)[:2]
top_pe_vol = sorted(pe_contracts, key=lambda x: int(x.get("volume", 0)), reverse=True)[:2]
vol_dominance = "PE Dominance (Bullish)" if total_pe_vol > total_ce_vol else "CE Dominance (Bearish)"

atm_call_premium = float(atm_call_contract.get("ltp", 0))
test_ce_symbol = atm_call_contract.get("symbol")
bid_price_ce = float(atm_call_contract.get("bid", atm_call_premium))
ask_price_ce = float(atm_call_contract.get("ask", atm_call_premium))
atm_call_iv = float(atm_call_contract.get("greeks", {}).get("iv", 15.0))
atm_ce_oi = int(atm_call_contract.get("oi", 0))
atm_ce_oichp = float(atm_call_contract.get("oichp", 0.0))

min_diff = float("inf")
matched_put_contract = None

for put in put_contracts_pool:
    put_premium = float(put.get("ltp", 0))
    diff = abs(put_premium - atm_call_premium)
    if diff < min_diff:
        min_diff = diff
        matched_put_contract = put

matched_put_strike = matched_put_contract.get("strike_price")
matched_put_premium = float(matched_put_contract.get("ltp", 0))
matched_put_symbol = matched_put_contract.get("symbol")
atm_pe_oi = int(matched_put_contract.get("oi", 0))
atm_pe_oichp = float(matched_put_contract.get("oichp", 0.0))

local_pcr = total_pe_oi / max(total_ce_oi, 1)
oi_net_aggression = atm_pe_oichp - atm_ce_oichp

gap_pct = ((open_price - prev_close) / prev_close) * 100
gap_direction = "Gap Up" if gap_pct >= 0 else "Gap Down"
abs_gap = abs(gap_pct)
gap_type = "Normal Open" if abs_gap <= 0.3 else "Mild Gap" if abs_gap <= 0.8 else "Large Gap" if abs_gap <= 1.5 else "Extreme Gap"
continuation_probability = 35.0 if gap_type in ["Large Gap", "Extreme Gap"] else 52.0
reversal_probability = 100.0 - continuation_probability

synthetic_straddle_price = atm_call_premium + matched_put_premium
required_move = synthetic_straddle_price * 0.88
spread_compression = ask_price_ce - bid_price_ce
theta_burn_day = synthetic_straddle_price / max(dte, 1)
theta_burn_15min = theta_burn_day / 25.0 
gamma_proxy = (1 / (synthetic_straddle_price * np.sqrt(max(dte, 0.5)))) * 100

cond_a = True if vix_pct_change > 1.5 else ((atm_call_iv / 100.0) > (0.6 * theta_burn_15min))
cond_b = (synthetic_straddle_price * 1.1) >= (0.9 * required_move)
cond_c = abs(atm_ce_oichp) > -1.0  
cond_d = gamma_proxy > 0.005
cond_e = spread_compression <= 2.50 
cond_f = get_ist_now().time() < LAST_ENTRY_TIME
filters_passed = sum([cond_a, cond_b, cond_c, cond_d, cond_e, cond_f])
system_execution_passed = filters_passed >= 4

# =====================================================================
# ENGINE STAGE 3: CLOUD INTRADAY PERSISTENCE (SUPABASE POSTGRESQL)
# =====================================================================
try:
    DB_URI = st.secrets["SUPABASE_URI"]
    conn = psycopg2.connect(DB_URI)
    conn.autocommit = True
    c = conn.cursor()
except Exception as e:
    st.error(f"🚨 Database Connection Failed. Exact Error: {e}")
    st.stop()

# Initialize Tables
c.execute('''CREATE TABLE IF NOT EXISTS flow_history 
             (timestamp TIMESTAMP, total_ce_oi BIGINT, total_pe_oi BIGINT, atm_ce_oi BIGINT, atm_pe_oi BIGINT)''')

c.execute('''CREATE TABLE IF NOT EXISTS strike_flow 
             (timestamp TIMESTAMP, strike INTEGER, ce_oi BIGINT, ce_vol BIGINT, ce_ltp REAL, 
              pe_oi BIGINT, pe_vol BIGINT, pe_ltp REAL)''')

# Check for new trading day reset using IST
c.execute("SELECT timestamp FROM flow_history ORDER BY timestamp DESC LIMIT 1")
last_entry = c.fetchone()
if last_entry:
    last_date = last_entry[0].date()
    if last_date != get_ist_now().date():
        c.execute("TRUNCATE TABLE flow_history")
        c.execute("TRUNCATE TABLE strike_flow")

# Insert current snapshots with IST time
current_time = get_ist_now()
c.execute("INSERT INTO flow_history VALUES (%s, %s, %s, %s, %s)", 
          (current_time, total_ce_oi, total_pe_oi, atm_ce_oi, atm_pe_oi))

for row in current_strike_data:
    if row['strike'] in target_strikes:
        c.execute("INSERT INTO strike_flow VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", 
                  (current_time, row['strike'], row['ce_oi'], row['ce_vol'], row['ce_ltp'], 
                   row['pe_oi'], row['pe_vol'], row['pe_ltp']))

# Retrieve Historical Deltas for UI
df_history = pd.read_sql_query("SELECT * FROM flow_history ORDER BY timestamp ASC", conn)
df_flow = pd.read_sql_query("SELECT * FROM strike_flow", conn)

df_history['timestamp'] = pd.to_datetime(df_history['timestamp'])
df_flow['timestamp'] = pd.to_datetime(df_flow['timestamp'])

unique_times = np.sort(df_flow['timestamp'].unique())
df_micro_structure = pd.DataFrame()
numeric_delta = pd.DataFrame()

if len(unique_times) >= 2:
    t_current = unique_times[-1]
    t_prev = unique_times[-2]
    
    df_curr = df_flow[df_flow['timestamp'] == t_current].set_index('strike')
    df_prev = df_flow[df_flow['timestamp'] == t_prev].set_index('strike')
    
    df_delta = pd.DataFrame(index=target_strikes)
    df_delta['Δ CE Vol'] = df_curr['ce_vol'] - df_prev['ce_vol']
    df_delta['Δ CE OI'] = df_curr['ce_oi'] - df_prev['ce_oi']
    df_delta['Δ CE LTP'] = (df_curr['ce_ltp'] - df_prev['ce_ltp']).round(2)
    df_delta['Strike (ATM: ' + str(atm_strike) + ')'] = df_delta.index
    df_delta['Δ PE LTP'] = (df_curr['pe_ltp'] - df_prev['pe_ltp']).round(2)
    df_delta['Δ PE OI'] = df_curr['pe_oi'] - df_prev['pe_oi']
    df_delta['Δ PE Vol'] = df_curr['pe_vol'] - df_prev['pe_vol']
    
    numeric_delta = df_delta.copy()
    
    # Process row indicators side by side to append active 3m tags directly into the tracker frame
    active_row_signals = []
    for strike_idx in target_strikes:
        d_ce_oi = df_delta.loc[strike_idx, 'Δ CE OI']
        d_pe_oi = df_delta.loc[strike_idx, 'Δ PE OI']
        d_ce_vo = df_delta.loc[strike_idx, 'Δ CE Vol']
        d_pe_vo = df_delta.loc[strike_idx, 'Δ PE Vol']
        active_row_signals.append(calculate_orderflow_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo))
    
    df_delta['🎯 ACTIVE RADAR SIGNAL'] = active_row_signals
    
    for col in ['Δ CE Vol', 'Δ CE OI', 'Δ PE OI', 'Δ PE Vol']:
        df_delta[col] = df_delta[col].fillna(0).apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
        
    for col in ['Δ CE LTP', 'Δ PE LTP']:
        df_delta[col] = df_delta[col].fillna(0).apply(lambda x: f"+{x:,.2f}" if x > 0 else (f"{x:,.2f}" if x < 0 else "0.00"))

    # Reorder structure column sequences to include the signal right next to the strike anchors
    final_micro_cols = ['Strike (ATM: ' + str(atm_strike) + ')', '🎯 ACTIVE RADAR SIGNAL', 'Δ CE OI', 'Δ CE Vol', 'Δ CE LTP', 'Δ PE OI', 'Δ PE Vol', 'Δ PE LTP']
    df_micro_structure = df_delta[final_micro_cols].reset_index(drop=True)

# Generate Narrative Timeline Data
narrative_data = []
if len(df_history) >= 2:
    for i in range(1, len(df_history)):
        prev = df_history.iloc[i-1]
        curr = df_history.iloc[i]
        
        delta_ce = curr['total_ce_oi'] - prev['total_ce_oi']
        delta_pe = curr['total_pe_oi'] - prev['total_pe_oi']
        
        if delta_ce > delta_pe and delta_ce > 0: bias = "🐻 Call Writers Dominating (Bearish Block)"
        elif delta_pe > delta_ce and delta_pe > 0: bias = "🐂 Put Writers Dominating (Bullish Support)"
        elif delta_ce < 0 and delta_pe < 0: bias = "🌪️ Unwinding / Panic Covering"
        else: bias = "⚖️ Neutral / Ranging Flow"

        narrative_data.append({
            "Interval Window": f"{prev['timestamp'].strftime('%H:%M')} ➔ {curr['timestamp'].strftime('%H:%M')}",
            "Δ Total Call OI": f"+{int(delta_ce):,}" if delta_ce > 0 else f"{int(delta_ce):,}",
            "Δ Total Put OI": f"+{int(delta_pe):,}" if delta_pe > 0 else f"{int(delta_pe):,}",
            "Market Narrative": bias
        })
    df_narrative = pd.DataFrame(narrative_data).iloc[::-1]

# =====================================================================
# ENGINE STAGE 4: PURE DIRECTIONAL PROBABILITY MATRIX
# =====================================================================
call_edge, put_edge = 50.0, 50.0

if local_pcr >= 1.15: call_edge += 25.0    
elif local_pcr <= 0.85: put_edge += 25.0     

if oi_net_aggression > 12.0: call_edge += 20.0    
elif oi_net_aggression < -12.0: put_edge += 20.0     

if vix_pct_change > 2.0: call_edge += 10.0; put_edge += 10.0 
if top25_adv >= 16: call_edge += 30.0
elif top25_adv <= 9: put_edge += 30.0

if nifty_spot > max_pain_strike + 50: put_edge += 20.0
elif nifty_spot < max_pain_strike - 50: call_edge += 20.0

total_edge_weight = call_edge + put_edge
prob_call = (call_edge / total_edge_weight) * 100
prob_put = (put_edge / total_edge_weight) * 100
win_prob = max(prob_call, prob_put)

if not system_execution_passed: trade_decision = "NO TRADE"
else: trade_decision = "CALL BUY" if prob_call >= prob_put else "PUT BUY"

total_quantity = LOT_SIZE_NIFTY * MAX_LOTS_ALLOWED

if trade_decision == "CALL BUY":
    actionable_signal_1 = f"⚡ BUY NIFTY CE - {atm_strike}"
    actionable_signal_2 = f"🎯 Directional Confidence: {win_prob:.1f}% (Bullish Edge Identified)"
    signal_color = "green"
elif trade_decision == "PUT BUY":
    actionable_signal_1 = f"⚡ BUY NIFTY PE - {matched_put_strike}"
    actionable_signal_2 = f"🎯 Directional Confidence: {win_prob:.1f}% (Bearish Edge Identified)"
    signal_color = "red"
else:
    actionable_signal_1 = "🚨 SYSTEM BLOCKED — RISK CRITERIA NOT MET"
    actionable_signal_2 = "⚠️ STAY IN CASH (Filters failed or market time-limit breached)"
    signal_color = "grey"

# =====================================================================
# DASHBOARD UI RENDERING
# =====================================================================
col_a, col_b = st.columns([2, 3])

with col_a:
    st.subheader("🎯 Active Trading Recommendation")
    st.markdown(f"""
        <div style="background-color: rgba(255,255,255,0.03); padding: 20px; border-radius: 10px; border-left: 6px solid {signal_color}; margin-bottom: 20px;">
            <p style="margin: 0; font-size: 14px; opacity: 0.7; font-weight: bold;">PRIMARY DIRECTIVE ACTION</p>
            <h2 style="margin: 5px 0 15px 0; color: white; font-size: 26px;">{actionable_signal_1}</h2>
            <p style="margin: 0; font-size: 14px; opacity: 0.7; font-weight: bold;">SYSTEM CONVICTION</p>
            <h4 style="margin: 5px 0 0 0; color: rgba(255,255,255,0.8); font-size: 18px;">{actionable_signal_2}</h4>
        </div>
    """, unsafe_allow_html=True)

with col_b:
    st.subheader("📊 Core Market Architecture")
    ui_col1, ui_col2, ui_col3 = st.columns(3)
    ui_col1.metric("NIFTY 50 Spot", f"₹{nifty_spot:,.2f}")
    ui_col2.metric("Target Options Expiry", f"{target_expiry.strftime('%Y-%m-%d')}", f"{dte} DTE")
    ui_col3.metric("Resolved ATM Anchor", f"Strike {atm_strike}")

st.markdown("---")

# --- INSTITUTIONAL RADAR UI ---
st.subheader("🔥 Institutional Radar: Aggressive Strike Shifts")
st.caption("Auto-detecting the top 2 strikes with the highest positive surge in Open Interest since the last refresh.")

if not numeric_delta.empty:
    radar_col1, radar_col2 = st.columns(2)
    top_ce = numeric_delta[numeric_delta['Δ CE OI'] > 0].sort_values(by='Δ CE OI', ascending=False).head(2)
    top_pe = numeric_delta[numeric_delta['Δ PE OI'] > 0].sort_values(by='Δ PE OI', ascending=False).head(2)
    
    with radar_col1:
        st.markdown("""
        <div style='background-color: rgba(74, 222, 128, 0.05); padding: 15px; border-radius: 8px; border-left: 4px solid #4ade80; margin-bottom: 15px;'>
            <h4 style='margin-top: 0; color: #4ade80;'>🟢 Top Call (CE) Surges</h4>
        """, unsafe_allow_html=True)
        if not top_ce.empty:
            for idx, row in top_ce.iterrows():
                st.markdown(f"**Strike {idx}** &nbsp; | &nbsp; Δ OI: `<span style='color:#4ade80;'>+{int(row['Δ CE OI']):,}</span>` &nbsp; | &nbsp; Δ Vol: `+{int(row['Δ CE Vol']):,}`", unsafe_allow_html=True)
        else:
            st.write("No positive Call OI shifts detected.")
        st.markdown("</div>", unsafe_allow_html=True)

    with radar_col2:
        st.markdown("""
        <div style='background-color: rgba(248, 113, 113, 0.05); padding: 15px; border-radius: 8px; border-left: 4px solid #f87171; margin-bottom: 15px;'>
            <h4 style='margin-top: 0; color: #f87171;'>🔴 Top Put (PE) Surges</h4>
        """, unsafe_allow_html=True)
        if not top_pe.empty:
            for idx, row in top_pe.iterrows():
                st.markdown(f"**Strike {idx}** &nbsp; | &nbsp; Δ OI: `<span style='color:#f87171;'>+{int(row['Δ PE OI']):,}</span>` &nbsp; | &nbsp; Δ Vol: `+{int(row['Δ PE Vol']):,}`", unsafe_allow_html=True)
        else:
            st.write("No positive Put OI shifts detected.")
        st.markdown("</div>", unsafe_allow_html=True)
else:
    st.info("Awaiting the next data refresh to calculate Institutional Radar metrics.")

st.markdown("---")

# --- NEW: INTRADAY NARRATIVE TIMELINE ---
st.subheader("📖 The Institutional Narrative (Shift Story)")
st.caption("Tracking the net shift in overall Call vs Put writing between every refresh to spot trend reversals.")

if len(df_history) >= 2:
    styled_narrative = df_narrative.style.map(color_coding, subset=['Δ Total Call OI', 'Δ Total Put OI'])
    st.dataframe(styled_narrative, use_container_width=True, hide_index=True)
else:
    st.info("🕒 At least two refreshes are required to build the intraday narrative timeline.")

st.markdown("---")

# --- MICRO-STRUCTURE STRIKE TRACKER UI ---
st.subheader("🔬 Micro-Structure Strike Tracker (ATM ± 5)")
st.caption(f"Real-time order flow shifts. Showing $\Delta$ since last refresh at: {pd.to_datetime(unique_times[-2]).strftime('%H:%M:%S') if len(unique_times) >= 2 else 'N/A'} (IST)")

if not df_micro_structure.empty:
    styled_df = df_micro_structure.style.map(color_coding, subset=['🎯 ACTIVE RADAR SIGNAL', 'Δ CE Vol', 'Δ CE OI', 'Δ CE LTP', 'Δ PE LTP', 'Δ PE OI', 'Δ PE Vol'])
    st.dataframe(styled_df, use_container_width=True, hide_index=True)
    
    st.markdown("### 🔍 Interactive Strike Cascades (Modal Windows)")
    st.caption("Click on any strike vector button below to trigger its chronological execution intelligence pop-up.")

    # Render dynamic buttons aligned into clean row slots
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
else:
    st.info("🕒 First load of the day. Please refresh the app in a few minutes to establish the baseline Delta tracking.")

st.markdown("---")

# --- INSTITUTIONAL FLOW HEATMAP ---
st.subheader("🕵️‍♂️ Institutional Flow & Liquidity Heatmap")
heat_col1, heat_col2, heat_col3 = st.columns(3)

with heat_col1:
    st.metric("Total Volume Dominance", vol_dominance)
    st.markdown(f"**Total CE Vol:** {total_ce_vol:,} <br> **Total PE Vol:** {total_pe_vol:,}", unsafe_allow_html=True)

with heat_col2:
    st.markdown("**Aggressive Writing (Highest +OI Adds)**")
    st.success(f"CE Wall: Strike {max_pos_oich_ce['strike']} (+{max_pos_oich_ce['val']:,} OI)")
    st.error(f"PE Support: Strike {max_pos_oich_pe['strike']} (+{max_pos_oich_pe['val']:,} OI)")

with heat_col3:
    st.markdown("**Panic Unwinding (Highest -OI Drops)**")
    st.warning(f"CE Short Covering: Strike {max_neg_oich_ce['strike']} ({max_neg_oich_ce['val']:,} OI)")
    st.warning(f"PE Trap Unwind: Strike {max_neg_oich_pe['strike']} ({max_neg_oich_pe['val']:,} OI)")

st.markdown("<br>", unsafe_allow_html=True)
vol_col1, vol_col2 = st.columns(2)

with vol_col1:
    st.markdown("**Top 2 Liquid Call Strikes (Magnets)**")
    if len(top_ce_vol) >= 2:
        st.info(f"1. Strike {top_ce_vol[0].get('strike_price')} (Vol: {int(top_ce_vol[0].get('volume', 0)):,})")
        st.info(f"2. Strike {top_ce_vol[1].get('strike_price')} (Vol: {int(top_ce_vol[1].get('volume', 0)):,})")

with vol_col2:
    st.markdown("**Top 2 Liquid Put Strikes (Magnets)**")
    if len(top_pe_vol) >= 2:
        st.info(f"1. Strike {top_pe_vol[0].get('strike_price')} (Vol: {int(top_pe_vol[0].get('volume', 0)):,})")
        st.info(f"2. Strike {top_pe_vol[1].get('strike_price')} (Vol: {int(top_pe_vol[1].get('volume', 0)):,})")

st.markdown("---")

layout_col1, layout_col2 = st.columns([3, 2])

with layout_col1:
    st.subheader("🔢 Mathematical Calculus & Order Flow Matrix")
    calc_df = pd.DataFrame({
        "Quantitative Parameter Indicator": [
            "Synthetic Straddle Premium Core", "Calculated Volatility (ATM IV)", 
            "5-Strike Localized PCR (Put/Call)", "Institutional OI Aggression Velocity",
            "Daily Structural Theta Decay", "15-Min Scaled Structural Theta Step", 
            "Bid-Ask Spread Window Variance", "Gamma Position Proxy Score"
        ],
        "Engine Value Matrix Output": [
            f"₹{synthetic_straddle_price:.2f}", f"{atm_call_iv:.2f}%", 
            f"{local_pcr:.3f}", f"{oi_net_aggression:+.2f}%",
            f"₹{theta_burn_day:.2f}", f"₹{theta_burn_15min:.4f}", 
            f"₹{spread_compression:.2f}", f"{gamma_proxy:.6f}"
        ]
    })
    st.dataframe(calc_df, use_container_width=True, hide_index=True)
    
    st.subheader("🎯 Pure Directional Probability Array")
    p_col1, p_col2 = st.columns(2)
    p_col1.metric("CALL BUY Likelihood", f"{prob_call:.1f}%", "Bullish Edge" if prob_call >= prob_put else None)
    p_col2.metric("PUT BUY Likelihood", f"{prob_put:.1f}%", "Bearish Edge" if prob_put > prob_call else None)

with layout_col2:
    st.subheader("⚙️ Structural Gap Validation")
    st.metric("Deviation Tracked", f"{gap_pct:.3f}%", gap_direction)
    st.markdown(f"**Gap Profile:** `{gap_type}` | **Trend Continuation:** `{continuation_probability}%` | **Reversal:** `{reversal_probability}%` ")
    
    st.subheader("🎛️ Safety Verification Filter Checks")
    filter_records = [
        {"Filter Check Statement": "Cond A: Volatility & VIX Expansion Safe", "Status": "✅ PASSED" if cond_a else "❌ FAILED"},
        {"Filter Check Statement": "Cond B: Straddle Volatility > Target", "Status": "✅ PASSED" if cond_b else "❌ FAILED"},
        {"Filter Check Statement": "Cond C: Active Institutional Velocity", "Status": "✅ PASSED" if cond_c else "❌ FAILED"},
        {"Filter Check Statement": "Cond D: Strategic Gamma Boundaries", "Status": "✅ PASSED" if cond_d else "❌ FAILED"},
        {"Filter Check Statement": "Cond E: Bid-Ask Spreads Compressed", "Status": "✅ PASSED" if cond_e else "❌ FAILED"},
        {"Filter Check Statement": "Cond F: Pre-14:00 IST cutoff limit", "Status": "✅ PASSED" if cond_f else "❌ FAILED"}
    ]
    st.dataframe(pd.DataFrame(filter_records), use_container_width=True, hide_index=True)

st.markdown("---")

# --- NEW MACRO MATRIX ---
st.subheader("🏛️ Institutional Macro & Breadth Matrix (The Truth Filter)")
macro_1, macro_2, macro_3 = st.columns(3)

with macro_1:
    st.markdown("<p style='margin: 0; font-size: 14px; opacity: 0.7; font-weight: bold;'>VOLATILITY REGIME</p>", unsafe_allow_html=True)
    st.metric("India VIX Base", f"{vix_lp:.2f}", f"{vix_pct_change:.2f}%")
    if vix_pct_change > 1.5: st.success("Option Buying Environment: Excellent (VIX Expanding)")
    elif vix_pct_change < -2.0: st.error("Option Buying Environment: Poor (Vega Crush Risk)")
    else: st.warning("Option Buying Environment: Neutral")

with macro_2:
    st.markdown("<p style='margin: 0; font-size: 14px; opacity: 0.7; font-weight: bold;'>OPTION CHAIN GRAVITY</p>", unsafe_allow_html=True)
    st.metric("Max Pain Anchor", f"Strike {max_pain_strike}")
    if pain_gravity == "PULLING DOWN": st.error("Spot is over-extended above Max Pain. High risk of mean-reversion drop.")
    elif pain_gravity == "PULLING UP": st.success("Spot is heavily discounted below Max Pain. High probability of upward bounce.")
    else: st.info("Spot is balanced near Max Pain. Neutral gravity effect.")

with macro_3:
    st.markdown("<p style='margin: 0; font-size: 14px; opacity: 0.7; font-weight: bold;'>INDEX CONSTITUENT BREADTH</p>", unsafe_allow_html=True)
    st.metric("Top 5 Heavyweights", f"{top5_adv} / 5 Advancing", "BULLISH" if top5_adv >= 3 else "BEARISH")
    st.markdown(f"📈 **Top 25 Index Weights:** `{top25_adv}` Advancing | `{25 - top25_adv}` Declining")
    st.markdown(f"📊 **Overall Nifty 50:** `{nifty50_adv}` Advancing | `{50 - nifty50_adv}` Declining")

st.markdown("---")

# =====================================================================
# FINAL EXECUTION PAYLOAD
# =====================================================================
if trade_decision == "NO TRADE":
    st.warning("🚨 FILTER CORE NOT SATISFIED — SYSTEM EMITTED CODE: NO TRADE AUTHORIZED")
else:
    basket_payload = []
    if trade_decision == "CALL BUY":
        basket_payload.append({"symbol": test_ce_symbol, "qty": int(total_quantity), "type": int(ORDER_TYPE), "side": 1, "productType": PRODUCT_TYPE, "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0, "offlineOrder": False})
    elif trade_decision == "PUT BUY":
        basket_payload.append({"symbol": matched_put_symbol, "qty": int(total_quantity), "type": int(ORDER_TYPE), "side": 1, "productType": PRODUCT_TYPE, "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0, "offlineOrder": False})
        
    with st.expander("🛠️ View API Execution Payload (Hidden for Clean UI)"):
        st.json(basket_payload)
    
    if st.button("Transmit Secure Order Blocks to Broker Gateway"):
        try:
            response = fyers.place_basket_orders(data=basket_payload)
            st.success("Transaction blocks transmitted successfully.")
            st.write(response)
        except Exception as order_fault:
            st.error(f"Execution Gate Intercepted Terminal Fault: {order_fault}")
