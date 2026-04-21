import streamlit as st
import requests
import pandas as pd
import time

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJwX2lwIjoiIiwic19pcCI6IiIsImlzcyI6ImRoYW4iLCJwYXJ0bmVySWQiOiIiLCJleHAiOjE3NzY4Nzk0MzAsImlhdCI6MTc3Njc5MzAzMCwidG9rZW5Db25zdW1lclR5cGUiOiJTRUxGIiwid2ViaG9va1VybCI6Imh0dHBzOi8vd2ViLmRoYW4uY28vaW5kZXgvcHJvZmlsZSIsImRoYW5DbGllbnRJZCI6IjExMDgwNjYwOTQifQ.j-Kxz_mchy_QGtyNz0_pu-WOfw36mgD0ORY_ngS_GnDTMLFKrQNMO_ViM_a6fl9rb4kfNz5OEN_YA8hep4OM4g"
DHAN_CLIENT_ID    = "1108066094"

TELEGRAM_TOKEN   = "8243416633:AAFjISDBXvhqGsM8xvOkWOeQ4eEmhMPlkNU"
TELEGRAM_CHAT_ID = "567677761"

API_BASE        = "https://api.dhan.co/v2"
OPTIONCHAIN_URL = f"{API_BASE}/optionchain"
EXPIRY_LIST_URL = f"{API_BASE}/optionchain/expirylist"

UNDERLYING_MAP = {
    "NIFTY":  {"Scrip": 13, "Segments": ["IDX_I", "NSE_FNO"], "step": 50},
    "SENSEX": {"Scrip": 1,  "Segments": ["BSE_FNO", "IDX_I"], "step": 100},
}

# ──────────────────────────────────────────────
# TELEGRAM ALERT
# ──────────────────────────────────────────────
def send_telegram_alert(index_name, ltp, atm, expiry, pcr, df):
    try:
        # ── CALL side ──
        max_c_oi_chg_row = df.loc[df["_cd"].idxmax()]
        max_c_vol_row    = df.loc[df["_cv"].idxmax()]

        # ── PUT side ──
        max_p_oi_chg_row = df.loc[df["_pd"].idxmax()]
        max_p_vol_row    = df.loc[df["_pv"].idxmax()]

        c_arrow = "▲" if max_c_oi_chg_row["_cd"] >= 0 else "▼"
        p_arrow = "▲" if max_p_oi_chg_row["_pd"] >= 0 else "▼"

        msg = (
            f"📊 *{index_name} Option Chain Alert*\n"
            f"🕐 {time.strftime('%d-%b %H:%M')} | Expiry: {expiry}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 LTP: `{ltp:,.0f}` | ATM: `{int(atm)}` | PCR: `{pcr:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 *CALL (CE)*\n"
            f"  🔹 Highest OI Chg : `{int(max_c_oi_chg_row['STRIKE'])}` — ΔOI: `{max_c_oi_chg_row['_cd']/1e5:.2f}L {c_arrow}` | LTP: `{max_c_oi_chg_row['C LTP']}`\n"
            f"  🔹 Highest Vol    : `{int(max_c_vol_row['STRIKE'])}` — Vol: `{max_c_vol_row['_cv']/1e5:.2f}L` | LTP: `{max_c_vol_row['C LTP']}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📉 *PUT (PE)*\n"
            f"  🔸 Highest OI Chg : `{int(max_p_oi_chg_row['STRIKE'])}` — ΔOI: `{max_p_oi_chg_row['_pd']/1e5:.2f}L {p_arrow}` | LTP: `{max_p_oi_chg_row['P LTP']}`\n"
            f"  🔸 Highest Vol    : `{int(max_p_vol_row['STRIKE'])}` — Vol: `{max_p_vol_row['_pv']/1e5:.2f}L` | LTP: `{max_p_vol_row['P LTP']}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"_Auto-alert on every page refresh_"
        )

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        pass  # Silent fail — never break the dashboard

# ──────────────────────────────────────────────
# CSS - CLEAN DARK TERMINAL
# ──────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="Pro Option Terminal", page_icon="📊")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700;800&family=JetBrains+Mono:wght@500;700&display=swap');

/* Force Deep Dark Background */
[data-testid="stAppViewContainer"], [data-testid="stHeader"], .main {
    background-color: #0a0c12 !important;
}

/* Clean White Text Global */
html, body, [class*="css"] {
    color: #ffffff !important;
    font-family: 'Inter', sans-serif !important;
}

/* Table Headers */
.section-headers { display: grid; grid-template-columns: 1fr 110px 1fr; gap: 10px; margin-bottom: 5px; }
.sh { 
    text-align: center; padding: 10px; font-weight: 700; border-radius: 4px; 
    border: 1px solid #2d3446; background: #141824; font-size: 0.8rem;
}

/* Dataframe Container */
[data-testid="stDataFrameResizable"] {
    background-color: #0a0c12 !important;
    border: 1px solid #2d3446 !important;
}
.stDataFrame th {
    background-color: #1c2230 !important;
    color: #94a3b8 !important;
    font-size: 0.7rem !important;
}

/* Style for Buttons */
div.stButton > button {
    width: 100%;
    background-color: #141824;
    color: white;
    border: 1px solid #2d3446;
}
div.stButton > button:hover {
    border-color: #00bcd4;
    color: #00bcd4;
}
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def _headers():
    return {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}

def fmt_lakh(val): return f"{val/1e5:.1f}"

# ──────────────────────────────────────────────
# REFRESH & INDEX SELECTION
# ──────────────────────────────────────────────
if "index_choice" not in st.session_state:
    st.session_state.index_choice = "NIFTY"

# 3 Minute Refresh (180 Seconds)
refresh_interval = 600
if "last_refresh" not in st.session_state: 
    st.session_state.last_refresh = time.time()

elapsed = time.time() - st.session_state.last_refresh
if elapsed >= refresh_interval:
    st.session_state.last_refresh = time.time()
    st.rerun()

# Index Selection Buttons at the very top
col_btn1, col_btn2, col_spacer = st.columns([1, 1, 5])
with col_btn1:
    if st.button("NIFTY"):
        st.session_state.index_choice = "NIFTY"
        st.rerun()
with col_btn2:
    if st.button("SENSEX"):
        st.session_state.index_choice = "SENSEX"
        st.rerun()

cfg = UNDERLYING_MAP[st.session_state.index_choice]

# ──────────────────────────────────────────────
# DATA FETCHING
# ──────────────────────────────────────────────
found_expiry, used_seg = None, None
for seg in cfg["Segments"]:
    r_exp = requests.post(EXPIRY_LIST_URL, json={"UnderlyingScrip": cfg["Scrip"], "UnderlyingSeg": seg}, headers=_headers())
    exp_list = r_exp.json().get("data", [])
    if exp_list:
        found_expiry, used_seg = exp_list[0], seg
        break

if found_expiry:
    r_oc = requests.post(OPTIONCHAIN_URL, json={"UnderlyingScrip": cfg["Scrip"], "UnderlyingSeg": used_seg, "Expiry": found_expiry}, headers=_headers())
    data_sec = r_oc.json().get("data", {})
    oc_map   = data_sec.get("oc", {})
    ltp      = float(data_sec.get("last_price") or 0)

    if oc_map:
        atm = round(ltp / cfg["step"]) * cfg["step"]
        rows = []
        for strike_s, legs in oc_map.items():
            strike_f = float(strike_s)
            if abs(strike_f - atm) <= (cfg["step"] * 10):
                ce, pe = legs.get("ce", {}), legs.get("pe", {})
                c_delta = int(ce.get("oi", 0)) - int(ce.get("previous_oi") or 0)
                p_delta = int(pe.get("oi", 0)) - int(pe.get("previous_oi") or 0)
                rows.append({
                    "C OI CH%": f"{ce.get('oi_change_pct', 0):.1f}%",
                    "C VOL (L)": f"{int(ce.get('volume',0))/1e5:.2f}",
                    "CALL OI (L)": f"{int(ce.get('oi',0))/1e5:.2f}",
                    "C Δ OI": f"{c_delta:,} {'▲' if c_delta >= 0 else '▼'}",
                    "C LTP": f"{float(ce.get('last_price', 0)):.1f}",
                    "STRIKE": strike_f,
                    "IV": f"{float(ce.get('iv', 0)):.1f}" if ce.get('iv') else "0.0",
                    "P LTP": f"{float(pe.get('last_price', 0)):.1f}",
                    "P Δ OI": f"{p_delta:,} {'▲' if p_delta >= 0 else '▼'}",
                    "PUT OI (L)": f"{int(pe.get('oi',0))/1e5:.2f}",
                    "P VOL (L)": f"{int(pe.get('volume',0))/1e5:.2f}",
                    "P OI CH%": f"{pe.get('oi_change_pct', 0):.1f}%",
                    "_cv": int(ce.get("volume", 0)), "_pv": int(pe.get("volume", 0)),
                    "_cd": c_delta, "_pd": p_delta,
                    "_coi": int(ce.get("oi", 0)), "_poi": int(pe.get("oi", 0))
                })

        df = pd.DataFrame(rows).sort_values("STRIKE").reset_index(drop=True)
        
        # PCR Calculation
        total_c_oi = df["_coi"].sum()
        total_p_oi = df["_poi"].sum()
        pcr = total_p_oi / total_c_oi if total_c_oi else 0

        # ──────────────────────────────────────────────
        # TELEGRAM ALERT — fires on every page load/refresh
        # ──────────────────────────────────────────────
        send_telegram_alert(
            st.session_state.index_choice, ltp, atm,
            found_expiry, pcr, df
        )

        # ──────────────────────────────────────────────
        # TOP PANEL (METRICS)
        # ──────────────────────────────────────────────
        st.markdown(f"""
        <div style="background-color: #0a0c12; padding: 10px 0px; border-bottom: 1px solid #2d3446;">
            <h1 style="color: white; font-size: 2.2rem; font-weight: 800; margin-bottom: 20px; letter-spacing: -1px;">
                NSE {st.session_state.index_choice} | ATM {int(atm)} | LTP {ltp:,.0f} | {time.strftime('%H:%M')}
            </h1>
            <div style="display: grid; grid-template-columns: repeat(6, 1fr); gap: 15px;">
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">LTP</div><div style="font-size: 1.6rem; font-weight: 700;">{ltp:,.0f}</div></div>
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">ATM</div><div style="font-size: 1.6rem; font-weight: 700;">{int(atm)}</div></div>
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">PCR</div><div style="font-size: 1.6rem; font-weight: 700;">{pcr:.2f}</div></div>
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">CE OI</div><div style="font-size: 1.6rem; font-weight: 700;">{fmt_lakh(total_c_oi)}L</div></div>
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">PE OI</div><div style="font-size: 1.6rem; font-weight: 700;">{fmt_lakh(total_p_oi)}L</div></div>
                <div><div style="color: #94a3b8; font-size: 0.75rem; font-weight: 600;">CE OI Chg</div><div style="font-size: 1.6rem; font-weight: 700;">{fmt_lakh(df['_cd'].sum())}L</div></div>
            </div>
            <div style="color: #64748b; font-size: 0.7rem; margin-top: 20px;">
                Expiry: {found_expiry} | Update in: {int(refresh_interval - elapsed)}s
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)
        st.markdown("<div class='section-headers'><div class='sh'>CALLS</div><div class='sh'>STRIKE</div><div class='sh'>PUTS</div></div>", unsafe_allow_html=True)

        # ──────────────────────────────────────────────
        # ORIGINAL TABLE HIGHLIGHT LOGIC
        # ──────────────────────────────────────────────
        max_c_vol_idx = df['_cv'].idxmax()
        max_p_vol_idx = df['_pv'].idxmax()
        max_c_oi_idx  = df['_cd'].idxmax()
        max_p_oi_idx  = df['_pd'].idxmax()

        def style_terminal(data):
            styles = pd.DataFrame('', index=data.index, columns=data.columns)
            styles.update(pd.DataFrame('background-color: #0a0c12; color: #ffffff;', index=data.index, columns=data.columns))
            styles['STRIKE'] = 'background-color: #141824; color: #ffffff; font-weight: 700;'
            styles['IV'] = 'background-color: #141824; color: #94a3b8;'

            # ORIGINAL Highlights (Dark Cyan / Deep Rose)
            styles.loc[max_c_vol_idx, 'C VOL (L)'] = 'background-color: #00bcd4; color: #000000; font-weight: 700;'
            styles.loc[max_c_oi_idx, 'C Δ OI'] = 'background-color: #00bcd4; color: #000000; font-weight: 700;'
            styles.loc[max_p_vol_idx, 'P VOL (L)'] = 'background-color: #e91e63; color: #000000; font-weight: 700;'
            styles.loc[max_p_oi_idx, 'P Δ OI'] = 'background-color: #e91e63; color: #000000; font-weight: 700;'

            atm_idx = data[data['STRIKE'] == atm].index
            if not atm_idx.empty:
                styles.loc[atm_idx[0], 'STRIKE'] = 'background-color: #ffffff; color: #000000; font-weight: 900;'
            return styles

        display_cols = ["C OI CH%", "C VOL (L)", "CALL OI (L)", "C Δ OI", "C LTP", "STRIKE", "IV", "P LTP", "P Δ OI", "PUT OI (L)", "P VOL (L)", "P OI CH%"]
        raw_cols = ["_cv", "_pv", "_cd", "_pd", "_coi", "_poi"]

        st.dataframe(
            df[display_cols + raw_cols].style.apply(style_terminal, axis=None)
            .format(precision=0).hide(axis="columns", subset=raw_cols),
            use_container_width=True, height=780
        )

# Forced reload via component to sync with refresh_interval
st.components.v1.html(f"<script>setTimeout(function(){{ window.parent.location.reload(); }}, {refresh_interval * 1000});</script>", height=0)
