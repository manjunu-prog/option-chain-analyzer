import base64
import datetime
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import warnings
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pyotp
import psycopg2
import requests
import streamlit as st
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

warnings.filterwarnings("ignore", category=UserWarning)

st.set_page_config(page_title="Candle Volume Spike Radar", layout="wide")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DB_PATH = os.path.join(tempfile.gettempdir(), "candle_volume_spike_radar.db")

def get_secret_value(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default


FYERS_ID = get_secret_value("FYERS_ID", "FAJ88605")
FYERS_PIN = get_secret_value("FYERS_PIN", "4089")
FYERS_TOTP = get_secret_value("FYERS_TOTP", "ZHOQNKKVMI7IRCAPUFX7OXRMPFXRYVU6")
FYERS_APP_ID = get_secret_value("FYERS_APP_ID", "Q3B2S22L5M")
FYERS_APP_SECRET = get_secret_value("FYERS_APP_SECRET", "PWZD03ONQ4")
FYERS_REDIRECT_URI = get_secret_value("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")

TELEGRAM_BOT_TOKEN = get_secret_value("TELEGRAM_BOT_TOKEN", "7851529826:AAHfyHVrVZi5iQubljaNgde76gPhr8pxql4")
TELEGRAM_CHAT_ID = get_secret_value("TELEGRAM_CHAT_ID", "567677761")
DEFAULT_SUPABASE_URI = "postgresql://postgres.hcujozwjlsprkmnwlrxz:qyQ+N8+sr+fHens@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"
SUPABASE_URI = get_secret_value("SUPABASE_URI", DEFAULT_SUPABASE_URI)

INSTRUMENTS = {
    "NIFTY": {
        "symbol": "NSE:NIFTY50-INDEX",
        "strike_step": 50,
        "lot_size": 25,
        "strike_count": 30,
    },
    "BANKNIFTY": {
        "symbol": "NSE:NIFTYBANK-INDEX",
        "strike_step": 100,
        "lot_size": 15,
        "strike_count": 30,
    },
    "SENSEX": {
        "symbol": "BSE:SENSEX-INDEX",
        "strike_step": 100,
        "lot_size": 10,
        "strike_count": 30,
    },
}


def get_ist_now():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def minute_floor(dt):
    return dt.replace(second=0, microsecond=0)


def b64(value):
    return base64.b64encode(str(value).encode()).decode()


def generate_app_id_hash(app_id, app_type, app_secret):
    return hashlib.sha256(f"{app_id}-{app_type}:{app_secret}".encode()).hexdigest()


def format_indian_num(number):
    if pd.isna(number):
        return "0"
    number = int(round(float(number)))
    sign = "-" if number < 0 else ""
    s = str(abs(number))
    if len(s) <= 3:
        return sign + s
    last_three = s[-3:]
    remaining = s[:-3]
    chunks = []
    while len(remaining) > 2:
        chunks.insert(0, remaining[-2:])
        remaining = remaining[:-2]
    if remaining:
        chunks.insert(0, remaining)
    return sign + ",".join(chunks + [last_three])


def send_telegram(message, repeat=1):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    def worker():
        for i in range(max(1, int(repeat))):
            try:
                requests.post(
                    url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
                    timeout=5,
                )
                if i < repeat - 1:
                    time.sleep(0.5)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


def execute_auto_login():
    session = requests.Session()
    try:
        r1 = session.post(
            "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
            json={"fy_id": b64(FYERS_ID), "app_id": "2"},
            timeout=10,
        )
        request_key = r1.json().get("request_key")
        totp_code = pyotp.TOTP(FYERS_TOTP).now()
        r2 = session.post(
            "https://api-t2.fyers.in/vagator/v2/verify_otp",
            json={"request_key": request_key, "otp": totp_code},
            timeout=10,
        )
        request_key = r2.json().get("request_key")
        r3 = session.post(
            "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
            json={"request_key": request_key, "identity_type": "pin", "identifier": b64(FYERS_PIN)},
            timeout=10,
        )
        login_token = r3.json().get("data", {}).get("access_token")
        r4 = session.post(
            "https://api-t1.fyers.in/api/v3/token",
            json={
                "fyers_id": FYERS_ID,
                "app_id": FYERS_APP_ID,
                "redirect_uri": FYERS_REDIRECT_URI,
                "appType": "100",
                "code_challenge": "",
                "state": "volume_radar",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            },
            headers={"Authorization": f"Bearer {login_token}"},
            timeout=10,
        )
        auth_url = r4.json().get("Url")
        auth_code = parse_qs(urlparse(auth_url).query).get("auth_code", [None])[0]
        r5 = session.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": generate_app_id_hash(FYERS_APP_ID, "100", FYERS_APP_SECRET),
                "code": auth_code,
            },
            timeout=10,
        )
        return r5.json().get("access_token")
    except Exception as exc:
        st.error(f"Fyers auto-login failed: {exc}")
        return None


def establish_gateway():
    token = execute_auto_login()
    if not token:
        st.session_state.authenticated = False
        st.session_state.fyers = None
        return False
    st.session_state.fyers = fyersModel.FyersModel(
        client_id=f"{FYERS_APP_ID}-100",
        token=token,
        is_async=False,
        log_path="",
    )
    st.session_state.authenticated = True
    return True


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS option_minute_snapshot (
            ts TEXT NOT NULL,
            instrument TEXT NOT NULL,
            atm INTEGER NOT NULL,
            strike INTEGER NOT NULL,
            ce_vol INTEGER DEFAULT 0,
            pe_vol INTEGER DEFAULT 0,
            ce_oi INTEGER DEFAULT 0,
            pe_oi INTEGER DEFAULT 0,
            ce_ltp REAL DEFAULT 0,
            pe_ltp REAL DEFAULT 0,
            PRIMARY KEY (ts, instrument, strike)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_log (
            ts TEXT NOT NULL,
            key TEXT PRIMARY KEY,
            instrument TEXT NOT NULL,
            strike INTEGER NOT NULL,
            side TEXT NOT NULL,
            message TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def get_supabase_conn():
    if not SUPABASE_URI:
        return None
    return psycopg2.connect(SUPABASE_URI)


def init_supabase():
    conn = get_supabase_conn()
    if conn is None:
        return False
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_minute_snapshot (
                ts TIMESTAMP NOT NULL,
                instrument TEXT NOT NULL,
                atm INTEGER NOT NULL,
                strike INTEGER NOT NULL,
                ce_vol BIGINT DEFAULT 0,
                pe_vol BIGINT DEFAULT 0,
                ce_oi BIGINT DEFAULT 0,
                pe_oi BIGINT DEFAULT 0,
                ce_ltp REAL DEFAULT 0,
                pe_ltp REAL DEFAULT 0,
                PRIMARY KEY (ts, instrument, strike)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS candle_volume_alert_log (
                ts TIMESTAMP NOT NULL,
                alert_key TEXT PRIMARY KEY,
                instrument TEXT NOT NULL,
                strike INTEGER NOT NULL,
                side TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        conn.close()
        return True
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        st.warning(f"Supabase setup failed, using local SQLite only: {exc}")
        return False


def read_snapshots():
    if st.session_state.get("use_supabase"):
        conn = get_supabase_conn()
        if conn is not None:
            try:
                return pd.read_sql_query("SELECT * FROM option_minute_snapshot ORDER BY ts ASC", conn)
            except Exception as exc:
                st.warning(f"Supabase read failed, using local SQLite: {exc}")
            finally:
                conn.close()

    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query("SELECT * FROM option_minute_snapshot ORDER BY ts ASC", conn)
    finally:
        conn.close()


def read_alert_log(limit=30):
    if st.session_state.get("use_supabase"):
        conn = get_supabase_conn()
        if conn is not None:
            try:
                return pd.read_sql_query(
                    """
                    SELECT ts, alert_key AS key, instrument, strike, side, message
                    FROM candle_volume_alert_log
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    conn,
                    params=(limit,),
                )
            except Exception as exc:
                st.warning(f"Supabase alert log read failed, using local SQLite: {exc}")
            finally:
                conn.close()

    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(
            "SELECT * FROM alert_log ORDER BY ts DESC LIMIT ?",
            conn,
            params=(limit,),
        )
    finally:
        conn.close()


def alert_already_sent(key):
    if st.session_state.get("use_supabase"):
        conn = get_supabase_conn()
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM candle_volume_alert_log WHERE alert_key = %s", (key,))
                return cur.fetchone() is not None
            except Exception:
                pass
            finally:
                conn.close()

    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT 1 FROM alert_log WHERE key = ?", (key,)).fetchone() is not None
    finally:
        conn.close()


def mark_alert_sent(key, instrument, strike, side, message):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO alert_log VALUES (?, ?, ?, ?, ?, ?)",
            (get_ist_now().isoformat(sep=" "), key, instrument, int(strike), side, message),
        )
        conn.commit()
    finally:
        conn.close()

    if st.session_state.get("use_supabase"):
        pg_conn = get_supabase_conn()
        if pg_conn is not None:
            try:
                pg_conn.autocommit = True
                cur = pg_conn.cursor()
                cur.execute(
                    """
                    INSERT INTO candle_volume_alert_log
                    (ts, alert_key, instrument, strike, side, message)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (alert_key) DO NOTHING
                    """,
                    (get_ist_now(), key, instrument, int(strike), side, message),
                )
            except Exception as exc:
                st.warning(f"Supabase alert write failed: {exc}")
            finally:
                pg_conn.close()


def send_db_backup_to_telegram():
    try:
        df_snapshots = read_snapshots()
        df_alerts = read_alert_log(limit=100000)
        backup_path = os.path.join(tempfile.gettempdir(), f"candle_volume_backup_{get_ist_now().strftime('%Y%m%d_%H%M')}.db")
        conn = sqlite3.connect(backup_path)
        df_snapshots.to_sql("option_minute_snapshot", conn, if_exists="replace", index=False)
        df_alerts.to_sql("alert_log", conn, if_exists="replace", index=False)
        conn.close()

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        caption = f"Candle Volume Radar DB Backup - {get_ist_now().strftime('%Y-%m-%d %H:%M')}"
        with open(backup_path, "rb") as db_file:
            response = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": db_file},
                timeout=30,
            )
        os.remove(backup_path)
        if response.json().get("ok"):
            st.success("Telegram DB backup sent successfully.")
        else:
            st.error(f"Telegram backup failed: {response.text}")
    except Exception as exc:
        st.error(f"Backup failed: {exc}")



def get_live_spot(fyers, symbol):
    try:
        res = fyers.quotes(data={"symbols": symbol})
        if res and res.get("s") == "ok":
            for row in res.get("d", []):
                if row.get("s") == "ok":
                    return float(row.get("v", {}).get("lp", 0))
    except Exception:
        pass
    return 0.0


def fetch_option_snapshot(fyers, name, cfg):
    spot = get_live_spot(fyers, cfg["symbol"])
    if spot <= 0:
        return spot, None, pd.DataFrame(), f"{name}: spot quote unavailable"

    atm = int(round(spot / cfg["strike_step"]) * cfg["strike_step"])
    target_strikes = {atm + i * cfg["strike_step"] for i in range(-10, 11)}

    try:
        response = fyers.optionchain(
            data={
                "symbol": cfg["symbol"],
                "strikecount": cfg["strike_count"],
                "timestamp": "",
                "greeks": "0",
            }
        )
    except Exception as exc:
        return spot, atm, pd.DataFrame(), f"{name}: optionchain error {exc}"

    if not response or response.get("s") != "ok":
        return spot, atm, pd.DataFrame(), f"{name}: optionchain unavailable"

    rows = {}
    for contract in response.get("data", {}).get("optionsChain", []):
        strike = contract.get("strike_price")
        opt_type = contract.get("option_type")
        if strike not in target_strikes or opt_type not in {"CE", "PE"}:
            continue
        row = rows.setdefault(
            int(strike),
            {
                "instrument": name,
                "atm": atm,
                "strike": int(strike),
                "ce_vol": 0,
                "pe_vol": 0,
                "ce_oi": 0,
                "pe_oi": 0,
                "ce_ltp": 0.0,
                "pe_ltp": 0.0,
            },
        )
        prefix = "ce" if opt_type == "CE" else "pe"
        row[f"{prefix}_vol"] = int(contract.get("volume") or 0)
        row[f"{prefix}_oi"] = int(contract.get("oi") or 0)
        row[f"{prefix}_ltp"] = float(contract.get("ltp") or 0.0)

    return spot, atm, pd.DataFrame(rows.values()), None


def store_snapshot(ts, df_rows):
    if df_rows.empty:
        return

    records = []
    for _, row in df_rows.iterrows():
        records.append(
            (
                ts.isoformat(sep=" "),
                row["instrument"],
                int(row["atm"]),
                int(row["strike"]),
                int(row["ce_vol"]),
                int(row["pe_vol"]),
                int(row["ce_oi"]),
                int(row["pe_oi"]),
                float(row["ce_ltp"]),
                float(row["pe_ltp"]),
            )
        )

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            """
            INSERT OR REPLACE INTO option_minute_snapshot
            (ts, instrument, atm, strike, ce_vol, pe_vol, ce_oi, pe_oi, ce_ltp, pe_ltp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        conn.commit()
    finally:
        conn.close()

    if st.session_state.get("use_supabase"):
        pg_conn = get_supabase_conn()
        if pg_conn is not None:
            try:
                pg_conn.autocommit = True
                cur = pg_conn.cursor()
                cur.executemany(
                    """
                    INSERT INTO option_minute_snapshot
                    (ts, instrument, atm, strike, ce_vol, pe_vol, ce_oi, pe_oi, ce_ltp, pe_ltp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts, instrument, strike) DO UPDATE SET
                        atm = EXCLUDED.atm,
                        ce_vol = EXCLUDED.ce_vol,
                        pe_vol = EXCLUDED.pe_vol,
                        ce_oi = EXCLUDED.ce_oi,
                        pe_oi = EXCLUDED.pe_oi,
                        ce_ltp = EXCLUDED.ce_ltp,
                        pe_ltp = EXCLUDED.pe_ltp
                    """,
                    records,
                )
            except Exception as exc:
                st.warning(f"Supabase snapshot write failed: {exc}")
            finally:
                pg_conn.close()


def build_deltas(df):
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values(["instrument", "strike", "ts"])
    grouped = df.groupby(["instrument", "strike"], sort=False)
    df["ce_1m_vol"] = grouped["ce_vol"].diff().clip(lower=0).fillna(0)
    df["pe_1m_vol"] = grouped["pe_vol"].diff().clip(lower=0).fillna(0)
    return df


def detect_volume_alerts(delta_df, cfg_by_name, spike_mult, min_fresh_volume, dominance_ratio, min_history, repeat_count):
    alerts = []
    if delta_df.empty:
        return alerts

    latest_ts = delta_df["ts"].max()
    latest_rows = delta_df[delta_df["ts"] == latest_ts]

    for _, latest in latest_rows.iterrows():
        instrument = latest["instrument"]
        strike = int(latest["strike"])
        hist = delta_df[
            (delta_df["instrument"] == instrument)
            & (delta_df["strike"] == strike)
            & (delta_df["ts"] < latest_ts)
        ]
        if len(hist) < min_history:
            continue

        ce_now = float(latest["ce_1m_vol"])
        pe_now = float(latest["pe_1m_vol"])
        ce_avg = float(hist["ce_1m_vol"].mean())
        pe_avg = float(hist["pe_1m_vol"].mean())
        atm_tag = " ATM" if int(latest["atm"]) == strike else ""
        lot_size = cfg_by_name[instrument]["lot_size"]
        time_label = latest_ts.strftime("%H:%M")

        if pe_now >= max(min_fresh_volume, pe_avg * spike_mult) and pe_now >= max(ce_now * dominance_ratio, 1):
            multiple = pe_now / pe_avg if pe_avg > 0 else 0
            key = f"{instrument}_PE_{strike}_{latest_ts.strftime('%Y%m%d%H%M')}"
            msg = (
                f"🚨 <b>UNUSUAL VOLUME DETECTED</b>\n"
                f"<b>{instrument} {strike}{atm_tag} PE</b> | {time_label}\n"
                f"Fresh 1-min PE volume: <b>{format_indian_num(pe_now)}</b> ({pe_now / lot_size:,.0f} lots)\n"
                f"Morning/today avg per candle: {format_indian_num(pe_avg)}\n"
                f"This candle is <b>{multiple:.1f}x huge</b> compared with today's normal pace.\n"
                f"Same strike CE volume only: {format_indian_num(ce_now)}\n"
                f"📌 Institutions/traders look interested in <b>PE side</b>."
            )
            alerts.append((key, instrument, strike, "PE", msg, repeat_count))

        if ce_now >= max(min_fresh_volume, ce_avg * spike_mult) and ce_now >= max(pe_now * dominance_ratio, 1):
            multiple = ce_now / ce_avg if ce_avg > 0 else 0
            key = f"{instrument}_CE_{strike}_{latest_ts.strftime('%Y%m%d%H%M')}"
            msg = (
                f"🚨 <b>UNUSUAL VOLUME DETECTED</b>\n"
                f"<b>{instrument} {strike}{atm_tag} CE</b> | {time_label}\n"
                f"Fresh 1-min CE volume: <b>{format_indian_num(ce_now)}</b> ({ce_now / lot_size:,.0f} lots)\n"
                f"Morning/today avg per candle: {format_indian_num(ce_avg)}\n"
                f"This candle is <b>{multiple:.1f}x huge</b> compared with today's normal pace.\n"
                f"Same strike PE volume only: {format_indian_num(pe_now)}\n"
                f"📌 Institutions/traders look interested in <b>CE side</b>."
            )
            alerts.append((key, instrument, strike, "CE", msg, repeat_count))

    return alerts


def latest_detail_frame(delta_df, focus_index):
    if delta_df.empty:
        return pd.DataFrame()

    latest_ts = delta_df["ts"].max()
    latest = delta_df[(delta_df["ts"] == latest_ts) & (delta_df["instrument"] == focus_index)].copy()
    if latest.empty:
        return pd.DataFrame()

    hist = delta_df[(delta_df["ts"] < latest_ts) & (delta_df["instrument"] == focus_index)]
    avg = hist.groupby("strike")[["ce_1m_vol", "pe_1m_vol"]].mean().rename(
        columns={"ce_1m_vol": "ce_avg_vol", "pe_1m_vol": "pe_avg_vol"}
    )
    latest = latest.join(avg, on="strike")
    latest[["ce_avg_vol", "pe_avg_vol"]] = latest[["ce_avg_vol", "pe_avg_vol"]].fillna(0)
    latest["ce_spike_x"] = np.where(latest["ce_avg_vol"] > 0, latest["ce_1m_vol"] / latest["ce_avg_vol"], 0)
    latest["pe_spike_x"] = np.where(latest["pe_avg_vol"] > 0, latest["pe_1m_vol"] / latest["pe_avg_vol"], 0)
    latest["Dominant Side"] = np.where(
        latest["pe_1m_vol"] > latest["ce_1m_vol"],
        "PE",
        np.where(latest["ce_1m_vol"] > latest["pe_1m_vol"], "CE", "EVEN"),
    )
    latest["Strike"] = latest.apply(
        lambda r: f"{int(r['strike'])} ATM" if int(r["strike"]) == int(r["atm"]) else f"{int(r['strike'])}",
        axis=1,
    )
    latest["CE 1m Vol"] = latest["ce_1m_vol"].apply(format_indian_num)
    latest["PE 1m Vol"] = latest["pe_1m_vol"].apply(format_indian_num)
    latest["CE Avg/Candle"] = latest["ce_avg_vol"].apply(format_indian_num)
    latest["PE Avg/Candle"] = latest["pe_avg_vol"].apply(format_indian_num)
    latest["CE Spike x"] = latest["ce_spike_x"].map(lambda x: f"{x:.1f}x" if x else "-")
    latest["PE Spike x"] = latest["pe_spike_x"].map(lambda x: f"{x:.1f}x" if x else "-")
    latest["CE Cum Vol"] = latest["ce_vol"].apply(format_indian_num)
    latest["PE Cum Vol"] = latest["pe_vol"].apply(format_indian_num)
    return latest[
        [
            "Strike",
            "Dominant Side",
            "CE 1m Vol",
            "CE Avg/Candle",
            "CE Spike x",
            "PE 1m Vol",
            "PE Avg/Candle",
            "PE Spike x",
            "CE Cum Vol",
            "PE Cum Vol",
            "ce_ltp",
            "pe_ltp",
        ]
    ].rename(
        columns={
            "ce_ltp": "CE LTP",
            "pe_ltp": "PE LTP",
        }
    )


def render_latest_table(delta_df, focus_index):
    view = latest_detail_frame(delta_df, focus_index)
    if view.empty:
        st.info("Waiting for the first two 1-minute snapshots to calculate candle volume.")
        return
    st.dataframe(view, use_container_width=True, hide_index=True)


def available_strikes(delta_df, focus_index):
    if delta_df.empty:
        return []
    focus = delta_df[delta_df["instrument"] == focus_index]
    if focus.empty:
        return []
    return sorted(int(x) for x in focus["strike"].dropna().unique())


def strike_history_frame(delta_df, focus_index, strike):
    if delta_df.empty or strike is None:
        return pd.DataFrame()

    hist = delta_df[(delta_df["instrument"] == focus_index) & (delta_df["strike"] == int(strike))].copy()
    if hist.empty:
        return pd.DataFrame()

    hist = hist.sort_values("ts")
    ce_avg = hist["ce_1m_vol"].replace(0, np.nan).mean()
    pe_avg = hist["pe_1m_vol"].replace(0, np.nan).mean()
    hist["Time"] = hist["ts"].dt.strftime("%H:%M")
    hist["CE 1m Vol Raw"] = hist["ce_1m_vol"]
    hist["PE 1m Vol Raw"] = hist["pe_1m_vol"]
    hist["CE Spike x"] = np.where(ce_avg and ce_avg > 0, hist["ce_1m_vol"] / ce_avg, 0)
    hist["PE Spike x"] = np.where(pe_avg and pe_avg > 0, hist["pe_1m_vol"] / pe_avg, 0)
    hist["Dominant Side"] = np.where(
        hist["pe_1m_vol"] > hist["ce_1m_vol"],
        "PE",
        np.where(hist["ce_1m_vol"] > hist["pe_1m_vol"], "CE", "EVEN"),
    )
    hist["CE 1m Vol"] = hist["ce_1m_vol"].apply(format_indian_num)
    hist["PE 1m Vol"] = hist["pe_1m_vol"].apply(format_indian_num)
    hist["CE Spike x"] = hist["CE Spike x"].map(lambda x: f"{x:.1f}x" if x else "-")
    hist["PE Spike x"] = hist["PE Spike x"].map(lambda x: f"{x:.1f}x" if x else "-")
    hist["CE Cum Vol"] = hist["ce_vol"].apply(format_indian_num)
    hist["PE Cum Vol"] = hist["pe_vol"].apply(format_indian_num)
    return hist[
        [
            "Time",
            "Dominant Side",
            "CE 1m Vol",
            "PE 1m Vol",
            "CE Spike x",
            "PE Spike x",
            "CE Cum Vol",
            "PE Cum Vol",
            "CE 1m Vol Raw",
            "PE 1m Vol Raw",
        ]
    ]


def highlight_high_low(series):
    if series.name not in {"CE 1m Vol", "PE 1m Vol"}:
        return [""] * len(series)

    raw_name = f"{series.name} Raw"
    raw_values = series.index.map(lambda idx: series.attrs.get(raw_name, {}).get(idx, np.nan))
    raw_series = pd.Series(raw_values, index=series.index, dtype="float64")
    nonzero = raw_series[raw_series > 0]
    styles = [""] * len(series)
    if nonzero.empty:
        return styles

    max_val = nonzero.max()
    min_val = nonzero.min()
    for pos, value in enumerate(raw_series):
        if pd.isna(value) or value <= 0:
            continue
        if value == max_val:
            styles[pos] = "background-color: #064e3b; color: #bbf7d0; font-weight: 700;"
        elif value == min_val:
            styles[pos] = "background-color: #4c0519; color: #fecdd3; font-weight: 700;"
    return styles


def styled_strike_history(history):
    if history.empty:
        return history

    view = history.drop(columns=["CE 1m Vol Raw", "PE 1m Vol Raw"]).copy()
    ce_raw = history["CE 1m Vol Raw"].to_dict()
    pe_raw = history["PE 1m Vol Raw"].to_dict()

    def style_col(series):
        if series.name == "CE 1m Vol":
            series.attrs["CE 1m Vol Raw"] = ce_raw
        if series.name == "PE 1m Vol":
            series.attrs["PE 1m Vol Raw"] = pe_raw
        return highlight_high_low(series)

    return view.style.apply(style_col, axis=0)


for key, default in {
    "authenticated": False,
    "fyers": None,
    "auto_login_attempted": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

init_db()

with st.sidebar:
    st.header("Candle Volume Radar")
    storage_mode = st.radio("Storage", ["Supabase Cloud", "Local SQLite"], horizontal=False)
    st.session_state.use_supabase = storage_mode == "Supabase Cloud"
    if st.session_state.use_supabase:
        if init_supabase():
            st.success("Supabase connected")
    else:
        st.info("Using local SQLite")
    st.markdown("---")
    focus_index = st.radio("View strike details for", list(INSTRUMENTS.keys()), horizontal=False)
    selected = st.multiselect(
        "Track and alert indices",
        list(INSTRUMENTS.keys()),
        default=list(dict.fromkeys([focus_index] + list(INSTRUMENTS.keys()))),
    )
    if focus_index not in selected:
        selected = list(dict.fromkeys([focus_index] + selected))
    st.markdown("---")
    st.subheader("Telegram")
    st.success("Active" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "Not configured")
    if st.button("Send Test Alert"):
        send_telegram("✅ <b>Candle Volume Spike Radar is live</b>", repeat=2)
        st.success("Test alert sent")
    if st.button("Send DB Backup to Telegram"):
        send_db_backup_to_telegram()
    st.markdown("---")
    st.subheader("Spike Rules")
    spike_mult = st.number_input("Spike vs today's average (x)", value=3.0, min_value=1.0, step=0.5)
    min_lakh = st.number_input("Minimum 1-min volume (lakh)", value=2.0, min_value=0.1, step=0.5)
    dominance_ratio = st.number_input("Dominance over opposite side (x)", value=1.5, min_value=1.0, step=0.1)
    min_history = st.number_input("Minimum previous candles", value=5, min_value=2, max_value=60, step=1)
    repeat_count = st.slider("Telegram repeats", 1, 10, 3)
    st.markdown("---")
    if st.button("Reconnect Fyers"):
        st.session_state.auto_login_attempted = False
        st.session_state.authenticated = False
        st.session_state.fyers = None
        st.rerun()
    if st.button("Clear Local Data"):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()
        st.success("Local radar DB cleared")
        st.rerun()

st_autorefresh(interval=60000, key="one_minute_radar_refresh")

if not st.session_state.authenticated and not st.session_state.auto_login_attempted:
    st.session_state.auto_login_attempted = True
    with st.spinner("Auto-connecting to Fyers..."):
        establish_gateway()

if not st.session_state.authenticated or st.session_state.fyers is None:
    st.warning("Auto-login is pending or failed. Use Reconnect Fyers from the sidebar.")
    st.stop()

if not selected:
    st.warning("Select at least one index in the sidebar.")
    st.stop()

st.title("1-Min Option Candle Volume Spike Radar")
st.caption("Tracks ATM ±10 strikes for NIFTY, BANKNIFTY, and SENSEX. Alerts fire when one side's fresh 1-minute volume spikes versus today's average and dominates the opposite side.")

current_ts = minute_floor(get_ist_now())
status_cards = []
errors = []

for name in selected:
    cfg = INSTRUMENTS[name]
    spot, atm, rows, error = fetch_option_snapshot(st.session_state.fyers, name, cfg)
    if error:
        errors.append(error)
        continue
    store_snapshot(current_ts, rows)
    status_cards.append({"Index": name, "Spot": f"{spot:,.2f}", "ATM": atm, "Tracked Strikes": len(rows)})

if errors:
    for err in errors:
        st.warning(err)

if status_cards:
    st.dataframe(pd.DataFrame(status_cards), use_container_width=True, hide_index=True)

snapshot_df = read_snapshots()
if not snapshot_df.empty:
    snapshot_df["ts"] = pd.to_datetime(snapshot_df["ts"])
    snapshot_df = snapshot_df[snapshot_df["ts"].dt.date == get_ist_now().date()]
delta_df = build_deltas(snapshot_df)
if not delta_df.empty:
    delta_df = delta_df[delta_df["instrument"].isin(selected)]
alerts = detect_volume_alerts(
    delta_df,
    INSTRUMENTS,
    spike_mult=spike_mult,
    min_fresh_volume=min_lakh * 100000,
    dominance_ratio=dominance_ratio,
    min_history=int(min_history),
    repeat_count=repeat_count,
)

for key, instrument, strike, side, message, repeat in alerts:
    if alert_already_sent(key):
        continue
    send_telegram(message, repeat=repeat)
    mark_alert_sent(key, instrument, strike, side, message)
    st.toast(message.replace("<b>", "").replace("</b>", "")[:140], icon="🚨")

alert_log = read_alert_log()
if not alert_log.empty:
    st.subheader("Live Telegram Alert Feed")
    for _, alert in alert_log.head(12).iterrows():
        color = "#ff4d4f" if alert["side"] == "PE" else "#22c55e"
        clean = str(alert["message"]).replace("<b>", "").replace("</b>", "").replace("\n", " | ")
        st.markdown(
            f"""
            <div style="border-left:4px solid {color};background:#111827;padding:10px 14px;margin:8px 0;border-radius:4px;">
                <b style="color:{color};">{alert['instrument']} {alert['strike']} {alert['side']}</b><br>
                <span style="color:#d1d5db;">{clean}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.subheader(f"{focus_index} Strike-Wise 1-Min Candle Volume")
render_latest_table(delta_df, focus_index)

if not delta_df.empty:
    detail = latest_detail_frame(delta_df, focus_index)
    if not detail.empty:
        ce_top = detail.sort_values("CE 1m Vol", key=lambda s: s.str.replace(",", "").astype(float), ascending=False).head(5)
        pe_top = detail.sort_values("PE 1m Vol", key=lambda s: s.str.replace(",", "").astype(float), ascending=False).head(5)
        left, right = st.columns(2)
        with left:
            st.markdown(f"**Top {focus_index} CE Fresh Volume**")
            st.dataframe(ce_top[["Strike", "CE 1m Vol", "CE Avg/Candle", "CE Spike x", "PE 1m Vol"]], use_container_width=True, hide_index=True)
        with right:
            st.markdown(f"**Top {focus_index} PE Fresh Volume**")
            st.dataframe(pe_top[["Strike", "PE 1m Vol", "PE Avg/Candle", "PE Spike x", "CE 1m Vol"]], use_container_width=True, hide_index=True)

    strikes = available_strikes(delta_df, focus_index)
    if strikes:
        state_key = f"selected_strike_{focus_index}"
        if state_key not in st.session_state or st.session_state[state_key] not in strikes:
            latest_focus = delta_df[delta_df["instrument"] == focus_index]
            atm_candidates = latest_focus["atm"].dropna().astype(int).unique()
            default_strike = int(atm_candidates[-1]) if len(atm_candidates) else strikes[len(strikes) // 2]
            st.session_state[state_key] = default_strike if default_strike in strikes else strikes[len(strikes) // 2]

        st.subheader(f"{focus_index} Interactive Strike History")
        selected_strike = st.selectbox(
            "Select strike to view full 1-minute candle volume history",
            strikes,
            index=strikes.index(st.session_state[state_key]),
            key=f"strike_select_{focus_index}",
        )
        st.session_state[state_key] = int(selected_strike)

        cols_per_row = 4
        for start in range(0, len(strikes), cols_per_row):
            cols = st.columns(cols_per_row)
            for col, strike in zip(cols, strikes[start:start + cols_per_row]):
                label = f"Strike {strike}"
                if strike == st.session_state[state_key]:
                    label = f"Selected {strike}"
                if col.button(label, key=f"{focus_index}_strike_btn_{strike}", use_container_width=True):
                    st.session_state[state_key] = strike
                    st.rerun()

        selected_strike = st.session_state[state_key]
        history = strike_history_frame(delta_df, focus_index, selected_strike)
        st.markdown(f"**{focus_index} {selected_strike} CE/PE 1-Min Candle Volume From Morning**")
        if history.empty:
            st.info("No candle history available for this strike yet.")
        else:
            ce_high = history["CE 1m Vol Raw"].max()
            pe_high = history["PE 1m Vol Raw"].max()
            ce_low = history.loc[history["CE 1m Vol Raw"] > 0, "CE 1m Vol Raw"].min()
            pe_low = history.loc[history["PE 1m Vol Raw"] > 0, "PE 1m Vol Raw"].min()
            metric_cols = st.columns(4)
            metric_cols[0].metric("Highest CE 1m Vol", format_indian_num(ce_high))
            metric_cols[1].metric("Lowest CE 1m Vol", format_indian_num(ce_low if pd.notna(ce_low) else 0))
            metric_cols[2].metric("Highest PE 1m Vol", format_indian_num(pe_high))
            metric_cols[3].metric("Lowest PE 1m Vol", format_indian_num(pe_low if pd.notna(pe_low) else 0))
            st.dataframe(styled_strike_history(history), use_container_width=True, hide_index=True)

st.subheader("Recent Raw Snapshots")
if snapshot_df.empty:
    st.info("No snapshots stored yet.")
else:
    recent = snapshot_df.tail(200).copy()
    recent["ts"] = pd.to_datetime(recent["ts"]).dt.strftime("%H:%M")
    st.dataframe(recent.sort_values(["ts", "instrument", "strike"], ascending=[False, True, True]), use_container_width=True, hide_index=True)
