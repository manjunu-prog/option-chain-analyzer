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

# Suppress pandas warning for raw psycopg2 connections
warnings.filterwarnings('ignore', category=UserWarning)

# --- CONFIGURATION & 3-MIN AUTOREFRESH ---
st.set_page_config(page_title="OI & Volume Delta Tracker", layout="wide")
st_autorefresh(interval=180000, key="delta_tracker_refresh") # 3 minutes

# IST Timezone Helper
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
def get_ist_now():
    return datetime.datetime.now(IST).replace(tzinfo=None)

# --- SESSION STATES ---
if "fyers_instance" not in st.session_state:
    st.session_state.fyers_instance = None
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# --- UTILITIES ---
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
    
    # User Logic: Call Writing outpaces Put Writing, and Put Volume outpaces Call Volume
    if d_ce_oi > d_pe_oi and d_pe_vo > d_ce_vo:
        return "🔴 PE BUY"
    # Inverse Logic: Put Writing outpaces Call Writing, and Call Volume outpaces Put Volume
    elif d_pe_oi > d_ce_oi and d_ce_vo > d_pe_vo:
        return "🟢 CE BUY"
    # Call Writing outpaces Put Writing, but Call Volume dominates (Short sellers capping standard moves)
    elif d_ce_oi > d_pe_oi and d_ce_vo > d_pe_vo:
        return "📉 CE SHORTS"
    # Put Writing outpaces Call Writing, but Put Volume dominates (Short sellers setting base support)
    elif d_pe_oi > d_ce_oi and d_pe_vo > d_ce_vo:
        return "📈 PE SHORTS"
    
    return "⚖️ NEUTRAL FLOW"

# --- POP-UP MODAL ENGINE ---
@st.dialog("📋 Combined Intel Timeline Ledger", width="large")
def show_strike_popup(strike, df_flow, is_atm):
    title_decorator = f"🎯 Strike {strike} (ATM)" if is_atm else f"Strike {strike}"
    st.subheader(f"Granular Flow Ledger for {title_decorator}")
    st.caption("Chronological 3-minute combined orderflow intelligence steps (Newest logs on top)")
    
    df_hist_strike = df_flow[df_flow['strike'] == strike].copy().sort_values('timestamp', ascending=True)
    df_hist_strike['d_ce_oi'] = df_hist_strike['ce_oi'].diff().fillna(0).astype(int)
    df_hist_strike['d_pe_oi'] = df_hist_strike['pe_oi'].diff().fillna(0).astype(int)
    df_hist_strike['d_ce_vo'] = df_hist_strike['ce_vol'].diff().fillna(0).astype(int)
    df_hist_strike['d_pe_vo'] = df_hist_strike['pe_vol'].diff().fillna(0).astype(int)
    
    # Calculate orderflow tags row-by-row
    signals = []
    for _, row in df_hist_strike.iterrows():
        signals.append(calculate_orderflow_signal(row['d_ce_oi'], row['d_pe_oi'], row['d_ce_vo'], row['d_pe_vo']))
    df_hist_strike['Orderflow Signal'] = signals
    
    df_hist_strike.sort_values('timestamp', ascending=False, inplace=True)
    df_hist_strike['Time/Date'] = df_hist_strike['timestamp'].dt.strftime('%H:%M:%S %p')
    
    # Format layout dataframes
    df_popup_display = pd.DataFrame()
    df_popup_display['Time/Date'] = df_hist_strike['Time/Date']
    df_popup_display['🎯 ACTION SIGNAL'] = df_hist_strike['Orderflow Signal']
    df_popup_display['Change in OI - CE'] = df_hist_strike['d_ce_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_popup_display['Change in OI - PE'] = df_hist_strike['d_pe_oi'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_popup_display['Change in Vol - CE'] = df_hist_strike['d_ce_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    df_popup_display['Change in Vol - PE'] = df_hist_strike['d_pe_vo'].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    
    styled_popup = df_popup_display.style.map(
        color_coding, 
        subset=['🎯 ACTION SIGNAL', 'Change in OI - CE', 'Change in OI - PE', 'Change in Vol - CE', 'Change in Vol - PE']
    )
    st.dataframe(styled_popup, use_container_width=True, hide_index=True)

# --- FRONTEND INTERFACE ---
st.title("📊 NIFTY 50 Intraday OI & Volume Delta Tracker")
st.caption("Fyers API v3 Infrastructure | Automated 3-Minute Interval Logger")

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
                st.success("Gateways Online. Tracking initialized.")
            else:
                st.session_state.authenticated = False

if not st.session_state.authenticated or st.session_state.fyers_instance is None:
    st.info("⚡ System status: Awaiting secure login parameters via sidebar.")
    st.stop()

fyers = st.session_state.fyers_instance

# --- GET SPOT & OPTION CHAIN ---
quotes = fyers.quotes(data={"symbols": "NSE:NIFTY50-INDEX"})
if not quotes or quotes.get("s") != "ok":
    st.error("Failed to fetch Nifty 50 Spot price.")
    st.stop()

nifty_spot = float(quotes["d"][0]["v"]["lp"])
atm_strike = round(nifty_spot / 50) * 50

chain_payload = {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 15, "timestamp": "", "greeks": "0"}
chain_response = fyers.optionchain(data=chain_payload)

if not chain_response or chain_response.get("s") != "ok":
    st.error("Failed to pull live option chain data.")
    st.stop()

options_list = chain_response.get("data", {}).get("optionsChain", [])

# Target 11 strikes near the money (ATM ± 5 strikes)
target_strikes = [atm_strike + (i * 50) for i in range(-5, 6)]

# --- DATABASE MANAGEMENT (SUPABASE POSTGRESQL) ---
try:
    DB_URI = st.secrets["SUPABASE_URI"]
    conn = psycopg2.connect(DB_URI)
    conn.autocommit = True
    c = conn.cursor()
except Exception as e:
    st.error(f"🚨 Database Connection Failed: {e}")
    st.stop()

# Build lightweight historical storage tables
c.execute('''CREATE TABLE IF NOT EXISTS pure_delta_flow 
             (timestamp TIMESTAMP, strike INTEGER, ce_oi BIGINT, ce_vol BIGINT, pe_oi BIGINT, pe_vol BIGINT)''')

c.execute('''CREATE TABLE IF NOT EXISTS overall_delta_flow 
             (timestamp TIMESTAMP, total_ce_oi BIGINT, total_ce_vol BIGINT, total_pe_oi BIGINT, total_pe_vol BIGINT)''')

# Clear historical records if a new calendar day shifts in
c.execute("SELECT timestamp FROM pure_delta_flow ORDER BY timestamp DESC LIMIT 1")
last_db_entry = c.fetchone()
if last_db_entry and last_db_entry[0].date() != get_ist_now().date():
    c.execute("TRUNCATE TABLE pure_delta_flow")
    c.execute("TRUNCATE TABLE overall_delta_flow")

# Insert current timestamp matrix
current_time = get_ist_now()
inserted_strikes = set()

running_ce_oi, running_ce_vol = 0, 0
running_pe_oi, running_pe_vol = 0, 0

for contract in options_list:
    strike = contract.get("strike_price")
    if strike in target_strikes:
        opt_type = contract.get("option_type")
        oi = int(contract.get("oi", 0))
        vol = int(contract.get("volume", 0))
        
        if strike not in inserted_strikes:
            counterpart = next((x for x in options_list if x.get("strike_price") == strike and x.get("option_type") != opt_type), {})
            ce_oi = oi if opt_type == "CE" else int(counterpart.get("oi", 0))
            ce_vol = vol if opt_type == "CE" else int(counterpart.get("volume", 0))
            pe_oi = oi if opt_type == "PE" else int(counterpart.get("oi", 0))
            pe_vol = vol if opt_type == "PE" else int(counterpart.get("volume", 0))
            
            running_ce_oi += ce_oi
            running_ce_vol += ce_vol
            running_pe_oi += pe_oi
            running_pe_vol += pe_vol
            
            c.execute("INSERT INTO pure_delta_flow VALUES (%s, %s, %s, %s, %s, %s)", 
                      (current_time, strike, ce_oi, ce_vol, pe_oi, pe_vol))
            inserted_strikes.add(strike)

if inserted_strikes:
    c.execute("INSERT INTO overall_delta_flow VALUES (%s, %s, %s, %s, %s)", 
              (current_time, running_ce_oi, running_ce_vol, running_pe_oi, running_pe_vol))

# --- CALCULATION ENGINE ---
df_flow = pd.read_sql_query("SELECT * FROM pure_delta_flow", conn)
df_flow['timestamp'] = pd.to_datetime(df_flow['timestamp'])
unique_times = np.sort(df_flow['timestamp'].unique())

df_overall = pd.read_sql_query("SELECT * FROM overall_delta_flow ORDER BY timestamp ASC", conn)
df_overall['timestamp'] = pd.to_datetime(df_overall['timestamp'])

# Overview Metrics Row
m_col1, m_col2, m_col3 = st.columns(3)
m_col1.metric("NIFTY 50 Spot Anchor", f"₹{nifty_spot:,.2f}")
m_col2.metric("Calculated ATM Strike", f"{atm_strike}")
m_col3.metric("Last Dynamic Log Update", current_time.strftime('%H:%M:%S'))

st.markdown("---")

# =====================================================================
# OVERALL MACRO CHANGE IN OI & VOLUME EVERY 3MINS
# =====================================================================
st.subheader("🌍 Total Aggregate Market Flow Tracker (All 11 Strikes Combined)")
st.caption("Chronological time ledger showing overall combined Volume and Open Interest changes tracking every 3 minutes.")

if len(df_overall) >= 2:
    df_overall['Δ Total CE OI'] = df_overall['total_ce_oi'].diff().fillna(0).astype(int)
    df_overall['Δ Total PE OI'] = df_overall['total_pe_oi'].diff().fillna(0).astype(int)
    df_overall['Δ Total CE Vol'] = df_overall['total_ce_vol'].diff().fillna(0).astype(int)
    df_overall['Δ Total PE Vol'] = df_overall['total_pe_vol'].diff().fillna(0).astype(int)
    
    # Generate overall orderflow macro tags
    macro_signals = []
    for _, row in df_overall.iterrows():
        macro_signals.append(calculate_orderflow_signal(row['Δ Total CE OI'], row['Δ Total PE OI'], row['Δ Total CE Vol'], row['Δ Total PE Vol']))
    df_overall['Macro Directive'] = macro_signals

    df_overall_display = df_overall.sort_values('timestamp', ascending=False).copy()
    df_overall_display['Time/Date'] = df_overall_display['timestamp'].dt.strftime('%H:%M %p')
    
    for col in ['Δ Total CE OI', 'Δ Total PE OI', 'Δ Total CE Vol', 'Δ Total PE Vol']:
        df_overall_display[col] = df_overall_display[col].apply(lambda x: f"+{int(x):,}" if x > 0 else (f"{int(x):,}" if x < 0 else "0"))
    
    col_macro_oi, col_macro_vol = st.columns(2)
    with col_macro_oi:
        st.markdown("### 📊 Combined Change in Open Interest (All Strikes)")
        macro_oi_df = df_overall_display[['Time/Date', 'Macro Directive', 'Δ Total CE OI', 'Δ Total PE OI']].copy()
        macro_oi_df.columns = ['Time/Date', '⚡ MARKET SIGNAL', 'Change in OI - CE', 'Change in OI - PE']
        st.dataframe(macro_oi_df.style.map(color_coding, subset=['⚡ MARKET SIGNAL', 'Change in OI - CE', 'Change in OI - PE']), use_container_width=True, hide_index=True)
        
    with col_macro_vol:
        st.markdown("### 📈 Combined Change in Volume (All Strikes)")
        macro_vol_df = df_overall_display[['Time/Date', 'Δ Total CE Vol', 'Δ Total PE Vol']].copy()
        macro_vol_df.columns = ['Time/Date', 'Change in Volume - CE', 'Change in Volume - PE']
        st.dataframe(macro_vol_df.style.map(color_coding, subset=['Change in Volume - CE', 'Change in Volume - PE']), use_container_width=True, hide_index=True)
else:
    st.info("🕒 Gathering aggregate macro metric snapshots. Overall data will display here on the next auto-refresh step.")

st.markdown("---")

# =====================================================================
# INDIVIDUAL STRIKES MONITOR MATRIX WITH COMBINED INTELLIGENCE SIGNALS
# =====================================================================
if len(unique_times) >= 2:
    t_current = unique_times[-1]
    t_prev = unique_times[-2]
    
    df_curr = df_flow[df_flow['timestamp'] == t_current].set_index('strike')
    df_prev = df_flow[df_flow['timestamp'] == t_prev].set_index('strike')
    
    st.subheader("🔬 Live Option Flow Matrix & Interactive Ledgers")
    st.caption("Review current layout statuses below. Click the action button on any strike row to open its time-series change history pop-up.")
    
    for strike in sorted(target_strikes):
        c_ce_oi = df_curr.loc[strike, 'ce_oi'] if strike in df_curr.index else 0
        c_pe_oi = df_curr.loc[strike, 'pe_oi'] if strike in df_curr.index else 0
        c_ce_vo = df_curr.loc[strike, 'ce_vol'] if strike in df_curr.index else 0
        c_pe_vo = df_curr.loc[strike, 'pe_vol'] if strike in df_curr.index else 0
        
        p_ce_oi = df_prev.loc[strike, 'ce_oi'] if strike in df_prev.index else c_ce_oi
        p_pe_oi = df_prev.loc[strike, 'pe_oi'] if strike in df_prev.index else c_pe_oi
        p_ce_vo = df_prev.loc[strike, 'ce_vol'] if strike in df_prev.index else c_ce_vo
        p_pe_vo = df_prev.loc[strike, 'pe_vol'] if strike in df_prev.index else c_pe_vo
        
        d_ce_oi = c_ce_oi - p_ce_oi
        d_pe_oi = c_pe_oi - p_pe_oi
        d_ce_vo = c_ce_vo - p_ce_vo
        d_pe_vo = c_pe_vo - p_pe_vo
        
        def fmt(val): return f"+{int(val):,}" if val > 0 else (f"{int(val):,}" if val < 0 else "0")
        is_atm = (strike == atm_strike)
        
        # Calculate active 3-minute row snapshot signature signal
        active_row_signal = calculate_orderflow_signal(d_ce_oi, d_pe_oi, d_ce_vo, d_pe_vo)
        
        row_container = st.container(border=True)
        with row_container:
            btn_col, signal_col, data_col = st.columns([1.5, 1.5, 7])
            
            with btn_col:
                btn_label = f"🎯 {strike} (ATM)" if is_atm else f"🔢 Strike {strike}"
                st.markdown(f"<div style='padding-top: 4px; font-weight: bold;'>{btn_label}</div>", unsafe_allow_html=True)
                if st.button("🔍 View History", key=f"btn_{strike}", use_container_width=True):
                    show_strike_popup(strike, df_flow, is_atm)
            
            with signal_col:
                # Format text strings colors for live dashboard markers
                sig_color = "#4ade80" if "BUY" in active_row_signal or "🟢" in active_row_signal else ("#f87171" if "SHORTS" in active_row_signal or "🔴" in active_row_signal else "rgba(255,255,255,0.7)")
                st.markdown(f"<div style='padding-top: 4px; font-weight: bold; color: {sig_color};'>{active_row_signal}</div>", unsafe_allow_html=True)
                    
            with data_col:
                sub_col1, sub_col2, sub_col3, sub_col4 = st.columns(4)
                
                def color_html(val_str):
                    if val_str.startswith('+'): return f"<span style='color:#4ade80; font-weight:bold;'>{val_str}</span>"
                    if val_str.startswith('-'): return f"<span style='color:#f87171; font-weight:bold;'>{val_str}</span>"
                    return f"<span>{val_str}</span>"

                sub_col1.markdown(f"**CE OI:** {int(c_ce_oi):,}<br>**Δ (3m):** {color_html(fmt(d_ce_oi))}", unsafe_allow_html=True)
                sub_col2.markdown(f"**CE Vol:** {int(c_ce_vo):,}<br>**Δ (3m):** {color_html(fmt(d_ce_vo))}", unsafe_allow_html=True)
                sub_col3.markdown(f"**PE OI:** {int(c_pe_oi):,}<br>**Δ (3m):** {color_html(fmt(d_pe_oi))}", unsafe_allow_html=True)
                sub_col4.markdown(f"**PE Vol:** {int(c_pe_vo):,}<br>**Δ (3m):** {color_html(fmt(d_pe_vo))}", unsafe_allow_html=True)

else:
    st.info("🕒 Gathering initial tracking telemetry baseline. Matrix tracking structures will fully output here upon the next 3-minute iteration loop.")
