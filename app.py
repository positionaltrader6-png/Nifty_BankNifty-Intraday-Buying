"""
================================================================================
Intraday Option BUYING — Streamlit Live Engine & Backtesting Suite (v7.3 Hybrid)
================================================================================
Features:
1. Live Trading Engine: Parallel evaluation of NIFTY & BANKNIFTY via ThreadPoolExecutor.
2. Direct Fetch Logic: Matches the verified iloc[-2] fetching from the standalone .py script.
3. State Recovery & Sheet Persistence: Open_Positions tab tracking across restarts.
4. Multi-Strategy Backtesting Suite: Spot 13 EMA, Premium 13/15/21 EMA exits.
5. Dynamic Sidebar Configuration: Expiries and Loxx Multipliers adjustable in real-time.
================================================================================
"""

import streamlit as st
import threading
import concurrent.futures
import datetime as dt
import time
import logging
import pyotp
import pandas as pd
import numpy as np
import requests
import gspread
import pytz
from google.oauth2.service_account import Credentials

# ─── 1. PAGE SETUP & GLOBAL CONFIG ───
st.set_page_config(page_title="Pro Momentum Engine v7.3", page_icon="📈", layout="wide")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)

IST = pytz.timezone("Asia/Kolkata")
def get_ist_now():
    return dt.datetime.now(IST).replace(tzinfo=None)

def get_secret(key, default=""):
    return st.secrets.get(key, default) if hasattr(st, "secrets") else default

API_KEY = get_secret("API_KEY", "Masked")
CLIENT_ID = get_secret("CLIENT_ID", "Masked")
PASSWORD = get_secret("PASSWORD", "Masked")
TOTP_SECRET = get_secret("TOTP_SECRET", "Masked")

GOOGLE_SHEET_ID = get_secret("GOOGLE_SHEET_ID", "1X9pcz5Cgj697wPgRjSBu-DescbaZkq_KLw6xrpw8vJE")
GOOGLE_SERVICE_ACCOUNT_JSON = "service_account.json"

TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", "8981928115:AAEtIU9VAix-mwD52zis38Wql4eD8kLEKcI")
TELEGRAM_CHAT_ID = get_secret("TELEGRAM_CHAT_ID", "8672437349")

UNDERLYING_INDICES = ["NIFTY", "BANKNIFTY"]
INTERVAL = "THREE_MINUTE"
INDICATOR_WARMUP_DAYS = 5

KC_PERIOD, KC_ATR_MULT = 20, 1.0
EMA_FAST, EMA_SLOW, EMA_SL_PERIOD = 9, 21, 13
ADX_PERIOD, ADX_MIN, CHOP_PERIOD = 14, 20.0, 14
LOXX_PERIOD, LOXX_DEV = 40, 1.0

DATA_FRESHNESS_RETRIES = 3
DATA_FRESHNESS_RETRY_DELAY = 5

STOP_LOSS_PCT = 10.0
MAX_TRADES_PER_DAY = 3
ENTRY_WINDOW_START = dt.time(9, 30)
ENTRY_WINDOW_END = dt.time(14, 30)
EOD_EXIT_TIME = dt.time(15, 0)
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

ENABLE_TRANSACTION_COSTS = True
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT, EXCHANGE_TXN_PCT, GST_PCT = 0.0625 / 100, 0.03503 / 100, 18.0 / 100
SEBI_CHARGES_PCT, STAMP_DUTY_PCT, SLIPPAGE_PCT = 0.0001 / 100, 0.003 / 100, 0.10 / 100

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENT_MASTER_URL_FALLBACK = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

HEARTBEAT_HEADERS = ["timestamp", "underlying", "spot_close", "signal_state", "loxx_width", "loxx_width_avg", "kc_sloping_up", "kc_sloping_down", "momentum_accelerating"]
TRADE_HEADERS = ["trading_date", "expiry_date", "trade_num", "direction", "entry_time", "exit_time", "spot_at_entry", "atm_strike", "option_symbol", "entry_price", "exit_price", "exit_reason", "pnl_pct"]
OPEN_POSITIONS_HEADERS = ["underlying", "trading_date", "direction", "entry_ts", "entry_price", "spot_entry", "atm_strike", "symbol", "token", "trade_num", "latest_opt_price", "latest_opt_sl", "last_alerted_sl", "trade_sheet"]
TRADE_MONITOR_HEADERS = ["timestamp", "event", "spot_price", "premium_price", "spot_13ema_sl", "premium_13ema_sl", "pnl_pct", "notes"]

# ─── 2. SIDEBAR CONFIGURATION ───
st.sidebar.header("⚙️ Index Parameters")

nifty_expiry = st.sidebar.date_input("NIFTY Expiry Date", value=dt.date(2026, 9, 1), key="sb_nifty_exp")
nifty_loxx = st.sidebar.number_input("NIFTY Loxx Multiplier", value=1.0, step=0.1, key="sb_nifty_loxx")

bn_expiry = st.sidebar.date_input("BANKNIFTY Expiry Date", value=dt.date(2026, 9, 29), key="sb_bn_exp")
bn_loxx = st.sidebar.number_input("BANKNIFTY Loxx Multiplier", value=1.0, step=0.1, key="sb_bn_loxx")

sheet_id_input = st.sidebar.text_input("Google Sheet ID", value=GOOGLE_SHEET_ID, key="sb_sheet_id")

INDEX_PARAMS = {
    "NIFTY": {"token": "99926000", "strike_step": 50, "lot_size": 65, "loxx_mult": nifty_loxx, "exit_mode": "SPOT_EMA", "expiry": nifty_expiry},
    "BANKNIFTY": {"token": "99926009", "strike_step": 100, "lot_size": 15, "loxx_mult": bn_loxx, "exit_mode": "SPOT_EMA", "expiry": bn_expiry}
}

# ─── 3. AUTHENTICATION & API HELPERS ───
def login():
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
    if not data.get("status"): raise RuntimeError(f"SmartAPI login failed: {data}")
    return obj

def get_sheet_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if hasattr(st, "secrets") and "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

def send_telegram_alert(message):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e: logging.error(f"Telegram alert failed: {e}")

def build_instrument_lookup(underlying):
    try: resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    except requests.exceptions.HTTPError: resp = requests.get(INSTRUMENT_MASTER_URL_FALLBACK, timeout=30)
    return {inst["symbol"]: inst["token"] for inst in resp.json() if inst.get("name") == underlying and inst.get("instrumenttype") == "OPTIDX"}

def expiry_to_symbol_str(expiry_date): return expiry_date.strftime("%d%b%y").upper()

def fetch_option_token(lookup, underlying, expiry_date, strike, side):
    symbol = f"{underlying}{expiry_to_symbol_str(expiry_date)}{strike}{side}"
    return symbol, lookup.get(symbol)

def get_ltp(smart_obj, tradingsymbol, symboltoken, exchange="NFO"):
    for attempt in range(1, 4):
        try:
            resp = smart_obj.ltpData(exchange, tradingsymbol, symboltoken)
            if not resp.get("status"):
                time.sleep(1); smart_obj = login(); continue
            ltp = resp.get("data", {}).get("ltp")
            if ltp is not None: return float(ltp), smart_obj
            time.sleep(1)
        except Exception as e:
            logging.warning(f"LTP fetch failed for {tradingsymbol} ({attempt}/3): {e}")
            time.sleep(1)
    return None, smart_obj

def get_candles_with_relogin(smart_obj, token, from_dt, to_dt, exchange="NSE", interval=INTERVAL):
    params = {"exchange": exchange, "symboltoken": token, "interval": interval, "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"), "todate": to_dt.strftime("%Y-%m-%d %H:%M")}
    for attempt in range(1, 5):
        try:
            resp = smart_obj.getCandleData(params)
            if not resp.get("status"):
                time.sleep(2); smart_obj = login(); continue
            df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
            return df, smart_obj
        except Exception: time.sleep(1)
    return pd.DataFrame(), smart_obj

# ─── 4. SHEETS & PERSISTENCE HELPERS ───
def _with_retry(fn, max_retries=5, base_delay=2.0):
    for attempt in range(1, max_retries + 1):
        try: return fn()
        except gspread.exceptions.APIError as e:
            status = getattr(e.response, "status_code", None)
            if status in (429, 500, 503) and attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1))); continue
            raise

def append_to_sheet(sh, tab_name, header, row_data):
    try: ws = _with_retry(lambda: sh.worksheet(tab_name))
    except gspread.WorksheetNotFound:
        ws = _with_retry(lambda: sh.add_worksheet(title=tab_name, rows=2000, cols=len(header)))
        _with_retry(lambda: ws.update([header], value_input_option="USER_ENTERED"))
    existing = _with_retry(lambda: ws.get_all_values())
    if not existing: _with_retry(lambda: ws.update([header], value_input_option="USER_ENTERED"))
    _with_retry(lambda: ws.append_row(row_data, value_input_option="USER_ENTERED"))

def _get_or_create_open_positions_ws(sh):
    try: ws = _with_retry(lambda: sh.worksheet(OPEN_POSITIONS_TAB))
    except gspread.WorksheetNotFound:
        ws = _with_retry(lambda: sh.add_worksheet(title=OPEN_POSITIONS_TAB, rows=10, cols=len(OPEN_POSITIONS_HEADERS)))
        _with_retry(lambda: ws.update([OPEN_POSITIONS_HEADERS], value_input_option="USER_ENTERED"))
    existing = _with_retry(lambda: ws.get_all_values())
    if not existing: _with_retry(lambda: ws.update([OPEN_POSITIONS_HEADERS], value_input_option="USER_ENTERED"))
    return ws

def save_open_position_to_sheet(sh, underlying, position, trading_date):
    ws = _get_or_create_open_positions_ws(sh)
    rows = _with_retry(lambda: ws.get_all_values())
    row_data = [
        str(underlying), trading_date.isoformat(), str(position["direction"]),
        position["entry_ts"].strftime("%Y-%m-%d %H:%M:%S"), float(round(position["entry_price"], 2)),
        float(round(position["spot_entry"], 2)), str(position["atm_strike"]), str(position["symbol"]),
        str(position["token"]), int(position["trade_num"]),
        float(round(position.get("latest_opt_price", position["entry_price"]), 2)),
        float(round(position.get("latest_opt_sl", 0.0), 2)),
        float(round(position.get("last_alerted_sl", 0.0), 2)),
        str(position.get("trade_sheet", ""))
    ]
    existing_row_idx = None
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0] == underlying:
            existing_row_idx = i; break
    if existing_row_idx: _with_retry(lambda: ws.update(f"A{existing_row_idx}:N{existing_row_idx}", [row_data], value_input_option="USER_ENTERED"))
    else: _with_retry(lambda: ws.append_row(row_data, value_input_option="USER_ENTERED"))

def clear_open_position_from_sheet(sh, underlying):
    ws = _get_or_create_open_positions_ws(sh)
    rows = _with_retry(lambda: ws.get_all_values())
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0] == underlying:
            _with_retry(lambda: ws.delete_rows(i)); break

def load_open_positions_from_sheet(sh, trading_date):
    ws = _get_or_create_open_positions_ws(sh)
    rows = _with_retry(lambda: ws.get_all_values())
    recovered = {}
    stale_row_indices = []
    for i, r in enumerate(rows[1:], start=2):
        if not r or not r[0]: continue
        underlying = r[0]
        if r[1] != trading_date.isoformat():
            stale_row_indices.append(i); continue
        recovered[underlying] = {
            "direction": r[2], "entry_ts": dt.datetime.strptime(r[3], "%Y-%m-%d %H:%M:%S"), "entry_price": float(r[4]),
            "spot_entry": float(r[5]), "atm_strike": r[6], "symbol": r[7], "token": r[8], "trade_num": int(r[9]),
            "latest_opt_price": float(r[10]), "latest_opt_sl": float(r[11]), "last_alerted_sl": float(r[12]), "trade_sheet": r[13] if len(r) > 13 else ""
        }
    for i in sorted(stale_row_indices, reverse=True): _with_retry(lambda idx=i: ws.delete_rows(idx))
    return recovered

def make_trade_sheet_name(underlying, trade_num, trading_date):
    return f"Trade_{underlying}_{trading_date.isoformat()}_{trade_num}"[:99]

def log_trade_event(sh, tab_name, event, spot_price, premium_price, spot_sl, premium_sl, pnl_pct, notes=""):
    row = [get_ist_now().strftime("%Y-%m-%d %H:%M:%S"), str(event), float(round(spot_price, 2)), float(round(premium_price, 2)), float(round(spot_sl, 2)), float(round(premium_sl, 2)), float(round(pnl_pct, 2)), str(notes)]
    append_to_sheet(sh, tab_name, TRADE_MONITOR_HEADERS, row)

# ─── 5. TECHNICAL INDICATORS ───
def compute_keltner(df, period=KC_PERIOD, mult=KC_ATR_MULT):
    high, low, close = df["high"], df["low"], df["close"]
    basis = close.ewm(span=period, adjust=False).mean()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return basis + mult * atr, basis, basis - mult * atr

def compute_adx_adxr(df, period=ADX_PERIOD):
    high, low, close = df["high"], df["low"], df["close"]
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, (adx + adx.shift(period)) / 2.0, plus_di, minus_di

def compute_chop(df, period=CHOP_PERIOD):
    tr1, tr2, tr3 = df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_sum = tr.rolling(period).sum()
    denom = (df['high'].rolling(period).max() - df['low'].rolling(period).min()).replace(0, np.nan)
    return 100 * np.log10(atr_sum / denom) / np.log10(period)

def compute_lwma(series, period):
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def compute_loxx_hlhvb(df, period=LOXX_PERIOD, dev=LOXX_DEV):
    wma_half = compute_lwma(df["close"], int(period / 2))
    wma_full = compute_lwma(df["close"], period)
    buffer_me = compute_lwma(2.0 * wma_half - wma_full, int(np.sqrt(period)))
    deviation = (df["high"] - df["low"]).rolling(period).std(ddof=1)
    return buffer_me + (deviation * dev), buffer_me, buffer_me - (deviation * dev)

def add_intraday_indicators(df):
    df = df.copy().reset_index(drop=True)
    df["ema9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema_sl"] = df["close"].ewm(span=EMA_SL_PERIOD, adjust=False).mean()
    df["kc_upper"], df["kc_basis"], df["kc_lower"] = compute_keltner(df, KC_PERIOD, KC_ATR_MULT)
    df["kc_basis_prev"] = df["kc_basis"].shift(1)
    df["hlhvb_up"], df["hlhvb_me"], df["hlhvb_dn"] = compute_loxx_hlhvb(df, LOXX_PERIOD, LOXX_DEV)
    df["hlhvb_me_prev"] = df["hlhvb_me"].shift(1)
    df["loxx_width"] = df["hlhvb_up"] - df["hlhvb_dn"]
    df["loxx_width_avg"] = df["loxx_width"].rolling(window=LOXX_PERIOD).mean()
    df["adx"], df["adxr"], df["plus_di"], df["minus_di"] = compute_adx_adxr(df)
    df["chop"] = compute_chop(df)
    df["adx_prev"], df["adxr_prev"], df["chop_prev"] = df["adx"].shift(1), df["adxr"].shift(1), df["chop"].shift(1)
    return df

# ─── 6. LIVE EVALUATION & EXECUTION LOGIC ───
def evaluate_candle(smart, option_lookup, gc, sh, underlying, index_token, strike_step, expiry_date, active_positions, daily_trade_counts, last_processed, trading_date):
    day_from = dt.datetime.combine(trading_date, MARKET_OPEN)
    now = get_ist_now()
    config = INDEX_PARAMS[underlying]

    spot_df, smart = get_candles_with_relogin(smart, index_token, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), now, exchange="NSE", interval=INTERVAL)
    if spot_df.empty: return

    spot_df = add_intraday_indicators(spot_df)
    day_df = spot_df[spot_df["timestamp"].dt.date == trading_date].reset_index(drop=True)

    if len(day_df) < 2:
        for fresh_attempt in range(1, DATA_FRESHNESS_RETRIES + 1):
            logging.warning(f"[{underlying}] Only {len(day_df)} candle(s) for today (attempt {fresh_attempt}/{DATA_FRESHNESS_RETRIES}) — retrying in {DATA_FRESHNESS_RETRY_DELAY}s...")
            time.sleep(DATA_FRESHNESS_RETRY_DELAY)
            now = get_ist_now()
            spot_df, smart = get_candles_with_relogin(smart, index_token, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), now, exchange="NSE", interval=INTERVAL)
            if spot_df.empty: continue
            spot_df = add_intraday_indicators(spot_df)
            day_df = spot_df[spot_df["timestamp"].dt.date == trading_date].reset_index(drop=True)
            if len(day_df) >= 2: break

    if len(day_df) < 2:
        logging.error(f"[{underlying}] Giving up this cycle — candle data still not synced.")
        return

    # ── Using EXACT logic from live_engine_v73-10_2.py ──
    row = day_df.iloc[-2]
    ts, t = row["timestamp"], row["timestamp"].time()

    if last_processed.get(underlying) == ts: return
    last_processed[underlying] = ts

    close, high, low, ema_sl = row["close"], row["high"], row["low"], row["ema_sl"]
    position = active_positions[underlying]

    # ── 1. Positional Management & Exits ──
    if position is not None:
        symbol = position["symbol"]
        opt_df, smart = get_candles_with_relogin(smart, position["token"], day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), now, exchange="NFO", interval=INTERVAL)

        if not opt_df.empty:
            opt_df["ema_sl"] = opt_df["close"].ewm(span=EMA_SL_PERIOD, adjust=False).mean()
            opt_row = opt_df[opt_df["timestamp"] == ts]

            if not opt_row.empty:
                candle_opt_price, opt_ema_sl = opt_row.iloc[0]["close"], opt_row.iloc[0]["ema_sl"]
            else:
                candle_opt_price = opt_df.iloc[-2]["close"] if len(opt_df) > 1 else opt_df.iloc[-1]["close"]
                opt_ema_sl = opt_df.iloc[-2]["ema_sl"] if len(opt_df) > 1 else opt_df.iloc[-1]["ema_sl"]

            ltp_price, smart = get_ltp(smart, symbol, position["token"], exchange="NFO")
            current_opt_price = ltp_price if ltp_price is not None else candle_opt_price
            if ltp_price is None: logging.warning(f"[{underlying}] LTP fetch failed for {symbol}, falling back to candle close.")

            active_positions[underlying]["latest_opt_price"] = current_opt_price
            active_positions[underlying]["latest_opt_sl"] = opt_ema_sl

            entry_price = position["entry_price"]
            pnl_pct = ((current_opt_price - entry_price) / entry_price) * 100.0

            exit_reason = None
            if pnl_pct <= -STOP_LOSS_PCT: exit_reason = "HARD_STOP_LOSS_HIT"
            elif t >= EOD_EXIT_TIME: exit_reason = "EOD_SQUAREOFF"
            elif config["exit_mode"] == "SPOT_EMA":
                if position["direction"] == "CE" and close < ema_sl: exit_reason = f"SPOT_EMA{EMA_SL_PERIOD}_CROSSDOWN"
                elif position["direction"] == "PE" and close > ema_sl: exit_reason = f"SPOT_EMA{EMA_SL_PERIOD}_CROSSUP"
            elif config["exit_mode"] == "PREMIUM_EMA":
                if current_opt_price < opt_ema_sl: exit_reason = f"PREMIUM_EMA{EMA_SL_PERIOD}_CROSSDOWN"

            if exit_reason is None:
                trade_sheet = position.get("trade_sheet")
                if trade_sheet: log_trade_event(sh, trade_sheet, "MONITOR", close, current_opt_price, ema_sl, opt_ema_sl, pnl_pct)
                monitor_msg = f"🔵 *{underlying} Update* 🔵\n\n*Symbol:* `{symbol}`\n*Time:* {ts.strftime('%H:%M')}\n*Spot Price:* {close:.2f}\n*Premium Price:* Rs {current_opt_price:.2f}\n*Current PnL:* {pnl_pct:.2f}%\n*SL - Spot 13 EMA:* {ema_sl:.2f}\n*SL - Premium 13 EMA:* {opt_ema_sl:.2f}"
                send_telegram_alert(monitor_msg)
                active_sl = ema_sl if config["exit_mode"] == "SPOT_EMA" else opt_ema_sl
                active_positions[underlying]["last_alerted_sl"] = active_sl
                save_open_position_to_sheet(sh, underlying, active_positions[underlying], trading_date)

            if exit_reason:
                msg = f"🔴 *{underlying} Exit* 🔴\n\n*Symbol:* `{symbol}`\n*Time:* {ts.strftime('%H:%M')}\n*Spot Price:* {close:.2f}\n*Exit Price:* Rs {current_opt_price:.2f}\n*SL - Spot 13 EMA:* {ema_sl:.2f}\n*SL - Premium 13 EMA:* {opt_ema_sl:.2f}\n*Reason:* {exit_reason}\n*PnL:* {pnl_pct:.2f}%"
                send_telegram_alert(msg)
                trade_row = [trading_date.isoformat(), expiry_date.isoformat(), int(position["trade_num"]), str(position["direction"]), position["entry_ts"].strftime("%H:%M:%S"), ts.strftime("%H:%M:%S"), float(round(position["spot_entry"], 2)), int(position["atm_strike"]), str(symbol), float(round(entry_price, 2)), float(round(current_opt_price, 2)), str(exit_reason), float(round(pnl_pct, 2))]
                append_to_sheet(sh, f"Live_Trades_{trading_date.isoformat()}", TRADE_HEADERS, trade_row)
                trade_sheet = position.get("trade_sheet")
                if trade_sheet: log_trade_event(sh, trade_sheet, "EXIT", close, current_opt_price, ema_sl, opt_ema_sl, pnl_pct, notes=exit_reason)
                clear_open_position_from_sheet(sh, underlying)
                active_positions[underlying] = None

    # ── 2. Entry Signal Evaluation ──
    kc_up, kc_basis, kc_low, kc_basis_prev = row["kc_upper"], row["kc_basis"], row["kc_lower"], row["kc_basis_prev"]
    hlhvb_up, hlhvb_me, hlhvb_dn, hlhvb_me_prev = row["hlhvb_up"], row["hlhvb_me"], row["hlhvb_dn"], row["hlhvb_me_prev"]
    loxx_width, loxx_width_avg = row["loxx_width"], row["loxx_width_avg"]
    ema9, ema21_entry = row["ema9"], row["ema21"]
    adx, adxr, adx_prev, adxr_prev = row["adx"], row["adxr"], row["adx_prev"], row["adxr_prev"]
    plus_di, minus_di, chop, chop_prev = row["plus_di"], row["minus_di"], row["chop"], row["chop_prev"]

    adx_rising = (adx > adx_prev) if pd.notna(adx) and pd.notna(adx_prev) else False
    adxr_rising = (adxr > adxr_prev) if pd.notna(adxr) and pd.notna(adxr_prev) else False
    chop_falling = (chop < chop_prev) if pd.notna(chop) and pd.notna(chop_prev) else False
    momentum_accelerating = (adx > ADX_MIN) and (adx > adxr) and adx_rising and adxr_rising and chop_falling

    is_kc_sloping_up = (kc_basis > kc_basis_prev) if pd.notna(kc_basis_prev) else False
    is_kc_sloping_down = (kc_basis < kc_basis_prev) if pd.notna(kc_basis_prev) else False
    is_valid_ce_span = (low > kc_low)
    is_valid_pe_span = (high < kc_up)
    is_valid_loxx_width = (loxx_width <= loxx_width_avg * config["loxx_mult"]) if pd.notna(loxx_width_avg) else True
    is_loxx_sloping_up = (hlhvb_me > hlhvb_me_prev) if pd.notna(hlhvb_me_prev) else False
    is_loxx_sloping_down = (hlhvb_me < hlhvb_me_prev) if pd.notna(hlhvb_me_prev) else False
    loxx_ce_valid = (close > hlhvb_dn) and is_loxx_sloping_up
    loxx_pe_valid = (close < hlhvb_up) and is_loxx_sloping_down

    signal = None
    if (close > kc_up) and (ema9 > ema21_entry) and momentum_accelerating and (plus_di > minus_di) and is_kc_sloping_up and is_valid_ce_span and loxx_ce_valid and is_valid_loxx_width:
        signal = "CE"
    elif (close < kc_low) and (ema9 < ema21_entry) and momentum_accelerating and (minus_di > plus_di) and is_kc_sloping_down and is_valid_pe_span and loxx_pe_valid and is_valid_loxx_width:
        signal = "PE"

    if active_positions[underlying]:
        pos = active_positions[underlying]
        curr_opt, curr_sl = pos.get("latest_opt_price", 0.0), pos.get("latest_opt_sl", 0.0)
        status_str = f"ACTIVE ({pos['direction']}) | Opt: {curr_opt:.1f} | SL: {curr_sl:.1f}"
    else:
        status_str = f"SIGNAL: {signal}" if signal else "WAITING"

    heartbeat_row = [ts.strftime("%Y-%m-%d %H:%M:%S"), str(underlying), float(round(close, 2)), status_str, float(round(loxx_width, 2)) if pd.notna(loxx_width) else 0.0, float(round(loxx_width_avg, 2)) if pd.notna(loxx_width_avg) else 0.0, bool(is_kc_sloping_up), bool(is_kc_sloping_down), bool(momentum_accelerating)]
    append_to_sheet(sh, f"Live_Heartbeat_{trading_date.isoformat()}", HEARTBEAT_HEADERS, heartbeat_row)

    can_enter = (active_positions[underlying] is None) and (daily_trade_counts[underlying] < MAX_TRADES_PER_DAY) and (ENTRY_WINDOW_START <= t <= ENTRY_WINDOW_END)
    if can_enter and signal:
        atm_strike = int(round(close / strike_step) * strike_step)
        symbol, token = fetch_option_token(option_lookup, underlying, expiry_date, atm_strike, signal)

        if token is None:
            logging.error(f"[{underlying}] Could not resolve token for '{symbol}'. Entry skipped.")
            return

        opt_df, smart = get_candles_with_relogin(smart, token, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), now, exchange="NFO", interval=INTERVAL)
        if opt_df.empty:
            for opt_attempt in range(1, DATA_FRESHNESS_RETRIES + 1):
                time.sleep(DATA_FRESHNESS_RETRY_DELAY)
                now = get_ist_now()
                opt_df, smart = get_candles_with_relogin(smart, token, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), now, exchange="NFO", interval=INTERVAL)
                if not opt_df.empty: break

        if not opt_df.empty:
            opt_df["ema_sl"] = opt_df["close"].ewm(span=EMA_SL_PERIOD, adjust=False).mean()
            opt_row = opt_df[opt_df["timestamp"] == ts]
            
            if not opt_row.empty:
                candle_entry_price, opt_ema_sl = opt_row.iloc[0]["close"], opt_row.iloc[0]["ema_sl"]
            else:
                candle_entry_price = opt_df.iloc[-2]["close"] if len(opt_df) > 1 else opt_df.iloc[-1]["close"]
                opt_ema_sl = opt_df.iloc[-2]["ema_sl"] if len(opt_df) > 1 else opt_df.iloc[-1]["ema_sl"]

            ltp_price, smart = get_ltp(smart, symbol, token, exchange="NFO")
            entry_price = ltp_price if ltp_price is not None else candle_entry_price

            daily_trade_counts[underlying] += 1
            active_positions[underlying] = {
                "direction": signal, "entry_ts": ts, "entry_price": entry_price, "spot_entry": close,
                "atm_strike": atm_strike, "symbol": symbol, "token": token, "trade_num": daily_trade_counts[underlying],
                "latest_opt_price": entry_price, "latest_opt_sl": opt_ema_sl,
                "last_alerted_sl": ema_sl if config["exit_mode"] == "SPOT_EMA" else opt_ema_sl,
                "trade_sheet": make_trade_sheet_name(underlying, daily_trade_counts[underlying], trading_date)
            }
            save_open_position_to_sheet(sh, underlying, active_positions[underlying], trading_date)
            log_trade_event(sh, active_positions[underlying]["trade_sheet"], "ENTRY", close, entry_price, ema_sl, opt_ema_sl, 0.0, notes=signal)

            msg = f"🟢 *{underlying} Entry* 🟢\n\n*Symbol:* `{symbol}`\n*Time:* {ts.strftime('%H:%M')}\n*Spot Price:* {close:.2f}\n*Entry Price:* Rs {entry_price:.2f}\n*SL - Spot 13 EMA:* {ema_sl:.2f}\n*SL - Premium 13 EMA:* {opt_ema_sl:.2f}"
            send_telegram_alert(msg)

# ─── 7. LIVE ENGINE THREAD ───
def run_live_trading_engine(stop_event, sheet_id):
    trading_date = get_ist_now().date()
    active_positions = {"NIFTY": None, "BANKNIFTY": None}
    daily_trade_counts = {"NIFTY": 0, "BANKNIFTY": 0}
    last_processed = {"NIFTY": None, "BANKNIFTY": None}

    try:
        smart = login()
        gc = get_sheet_client()
        sh = gc.open_by_key(sheet_id)

        recovered = load_open_positions_from_sheet(sh, trading_date)
        for und, pos in recovered.items():
            if und in active_positions:
                active_positions[und] = pos
                daily_trade_counts[und] = max(daily_trade_counts.get(und, 0), pos["trade_num"])
                send_telegram_alert(f"♻️ *{und} Position Recovered:* `{pos['symbol']}` @ Rs {pos['entry_price']:.2f}")

        lookups = {und: build_instrument_lookup(und) for und in UNDERLYING_INDICES}
    except Exception as e:
        send_telegram_alert(f"⚠️ *Startup Failure:* {e}")
        return

    send_telegram_alert("🚀 *Pro Engine v7.3 Started*")

    while not stop_event.is_set():
        now = get_ist_now()
        current_time = now.time()

        if current_time < MARKET_OPEN:
            time.sleep(10)
            continue
        if current_time >= MARKET_CLOSE:
            send_telegram_alert("🛑 *Market Closed (3:30 PM). Engine Terminated.*")
            break

        seconds_until_next_candle = (3 - (now.minute % 3)) * 60 - now.second
        if seconds_until_next_candle <= 0: seconds_until_next_candle = 180

        sleep_end = now + dt.timedelta(seconds=seconds_until_next_candle + 15)
        while get_ist_now() < sleep_end:
            if stop_event.is_set(): return
            time.sleep(1)

        def safe_evaluate(und):
            try: evaluate_candle(smart, lookups[und], gc, sh, und, INDEX_PARAMS[und]["token"], INDEX_PARAMS[und]["strike_step"], INDEX_PARAMS[und]["expiry"], active_positions, daily_trade_counts, last_processed, trading_date)
            except Exception as e: logging.error(f"Error {und}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            concurrent.futures.wait([executor.submit(safe_evaluate, und) for und in UNDERLYING_INDICES])

# ─── 8. BACKTEST SUITE ENGINE ───
def apply_slippage(action, price):
    if not ENABLE_TRANSACTION_COSTS or SLIPPAGE_PCT == 0:
        return price, 0.0
    adj = price * SLIPPAGE_PCT
    return (round(price - adj, 2), round(adj, 2)) if action == "SELL" else (round(price + adj, 2), round(adj, 2))

def compute_charges(action, price, qty):
    if not ENABLE_TRANSACTION_COSTS: return 0.0
    turnover = price * qty
    stt = turnover * STT_SELL_PCT if action == "SELL" else 0.0
    exchange = turnover * EXCHANGE_TXN_PCT
    gst = (BROKERAGE_PER_ORDER + exchange) * GST_PCT
    sebi = turnover * SEBI_CHARGES_PCT
    stamp = turnover * STAMP_DUTY_PCT if action == "BUY" else 0.0
    return round(BROKERAGE_PER_ORDER + stt + exchange + gst + sebi + stamp, 2)

def run_backtest_suite(underlying, trade_date, expiry_date):
    smart = login()
    lookup = build_instrument_lookup(underlying)
    config = INDEX_PARAMS[underlying]

    warmup_from = dt.datetime.combine(trade_date - dt.timedelta(days=INDICATOR_WARMUP_DAYS), MARKET_OPEN)
    day_to = dt.datetime.combine(trade_date, MARKET_CLOSE)

    spot_df_raw, smart = get_candles_with_relogin(smart, config["token"], warmup_from, day_to, exchange="NSE", interval=INTERVAL)
    if spot_df_raw.empty: raise ValueError(f"No Spot data retrieved for {underlying} on {trade_date}")

    day_df = add_intraday_indicators(spot_df_raw)
    day_df = day_df[day_df["timestamp"].dt.date == trade_date].reset_index(drop=True)

    strategies = [
        {"name": "SPOT_13_EMA", "mode": "SPOT_EMA", "period": 13},
        {"name": "PREM_13_EMA", "mode": "PREMIUM_EMA", "period": 13},
        {"name": "PREM_15_EMA", "mode": "PREMIUM_EMA", "period": 15},
        {"name": "PREM_21_EMA", "mode": "PREMIUM_EMA", "period": 21}
    ]

    all_trades_results = {}
    summary_rows = []
    option_cache = {}

    for strat in strategies:
        trades, position, trades_count = [], None, 0

        for idx, row in day_df.iterrows():
            ts, t, close = row["timestamp"], row["timestamp"].time(), row["close"]
            if t > dt.time(15, 10): break

            if position is not None:
                matching_candle = position["opt_df"][position["opt_df"]["timestamp"] == ts]
                if not matching_candle.empty:
                    current_price = matching_candle.iloc[0]["close"]
                    pnl_pct = ((current_price - position["entry_price"]) / position["entry_price"]) * 100.0

                    exit_reason = None
                    if pnl_pct <= -STOP_LOSS_PCT: exit_reason = "HARD_STOP_LOSS_HIT"
                    elif t >= EOD_EXIT_TIME: exit_reason = "EOD_SQUAREOFF"
                    elif strat["mode"] == "SPOT_EMA":
                        if position["direction"] == "CE" and close < row["ema_sl"]: exit_reason = f"SPOT_EMA{strat['period']}_CROSSDOWN"
                        elif position["direction"] == "PE" and close > row["ema_sl"]: exit_reason = f"SPOT_EMA{strat['period']}_CROSSUP"
                    elif strat["mode"] == "PREMIUM_EMA":
                        if current_price < matching_candle.iloc[0]["ema_sl"]: exit_reason = f"PREMIUM_EMA{strat['period']}_CROSSDOWN"

                    if exit_reason:
                        exit_exec, _ = apply_slippage("SELL", current_price)
                        total_charges = round(compute_charges("BUY", position["entry_price"], config["lot_size"]) + compute_charges("SELL", exit_exec, config["lot_size"]), 2)
                        gross = round((exit_exec - position["entry_price"]) * config["lot_size"], 2)

                        trades.append({
                            "trading_date": trade_date.isoformat(), "expiry_date": expiry_date.isoformat(),
                            "trade_num": position["trade_num"], "direction": position["direction"],
                            "entry_time": position["entry_ts"].strftime("%H:%M:%S"), "exit_time": ts.strftime("%H:%M:%S"),
                            "spot_at_entry": position["spot_entry"], "atm_strike": position["atm_strike"],
                            "option_symbol": position["symbol"], "entry_price": position["entry_price"],
                            "exit_price": exit_exec, "exit_reason": exit_reason, "pnl_pct": round(pnl_pct, 2),
                            "lot_size": config["lot_size"], "gross_pnl": gross, "total_charges": total_charges, "net_pnl": round(gross - total_charges, 2)
                        })
                        position = None

            if (position is None) and (trades_count < MAX_TRADES_PER_DAY) and (ENTRY_WINDOW_START <= t <= ENTRY_WINDOW_END):
                momentum = (row["adx"] > ADX_MIN) and (row["adx"] > row["adxr"]) and (row["adx"] > row["adx_prev"]) and (row["adxr"] > row["adxr_prev"]) and (row["chop"] < row["chop_prev"])
                loxx_valid = (row["loxx_width"] <= row["loxx_width_avg"] * config["loxx_mult"])

                signal = None
                if close > row["kc_upper"] and row["ema9"] > row["ema21"] and momentum and row["plus_di"] > row["minus_di"] and loxx_valid and close > row["hlhvb_dn"]: signal = "CE"
                elif close < row["kc_lower"] and row["ema9"] < row["ema21"] and momentum and row["minus_di"] > row["plus_di"] and loxx_valid and close < row["hlhvb_up"]: signal = "PE"

                if signal:
                    atm_strike = int(round(close / config["strike_step"]) * config["strike_step"])
                    sym, tok = fetch_option_token(lookup, underlying, expiry_date, atm_strike, signal)
                    if tok:
                        if tok not in option_cache:
                            opt_df, smart = get_candles_with_relogin(smart, tok, warmup_from, day_to, exchange="NFO", interval=INTERVAL)
                            if not opt_df.empty: option_cache[tok] = opt_df.copy()
                        if tok in option_cache and not option_cache[tok].empty:
                            option_cache[tok]["ema_sl"] = option_cache[tok]["close"].ewm(span=strat["period"], adjust=False).mean()
                            if not option_cache[tok][option_cache[tok]["timestamp"] == ts].empty:
                                raw_entry = option_cache[tok][option_cache[tok]["timestamp"] == ts].iloc[0]["close"]
                                trades_count += 1
                                position = {"direction": signal, "entry_ts": ts, "entry_price": apply_slippage("BUY", raw_entry)[0], "spot_entry": close, "atm_strike": atm_strike, "symbol": sym, "token": tok, "trade_num": trades_count, "opt_df": option_cache[tok]}

        all_trades_results[strat["name"]] = pd.DataFrame(trades)
        summary_rows.append({"Strategy": strat["name"], "Date": trade_date.isoformat(), "Expiry": expiry_date.isoformat(), "Trades": len(trades), "Win Rate (%)": round((sum(1 for t in trades if t["net_pnl"] > 0) / len(trades)) * 100, 2) if trades else 0.0, "Gross PnL (₹)": round(sum(t["gross_pnl"] for t in trades), 2), "Charges (₹)": round(sum(t["total_charges"] for t in trades), 2), "Net PnL (₹)": round(sum(t["net_pnl"] for t in trades), 2)})

    return pd.DataFrame(summary_rows), all_trades_results

# ─── 9. STREAMLIT DASHBOARD TABS ───
if "bot_thread" not in st.session_state: st.session_state.bot_thread = None
if "stop_event" not in st.session_state: st.session_state.stop_event = threading.Event()
if "is_running" not in st.session_state: st.session_state.is_running = False

st.title("📈 Pro Momentum Engine (v7.3 Hybrid)")
tab_live, tab_backtest = st.tabs(["🔴 Live Trading", "🧪 Dynamic Backtest"])

with tab_live:
    if not st.session_state.is_running:
        if st.button("▶️ Start Live Engine (Parallel Threads)", type="primary"):
            st.session_state.stop_event.clear()
            st.session_state.bot_thread = threading.Thread(target=run_live_trading_engine, args=(st.session_state.stop_event, sheet_id_input), daemon=True)
            st.session_state.bot_thread.start()
            st.session_state.is_running = True
            st.rerun()
    else:
        st.success("🟢 Engine is active. NIFTY and BANKNIFTY are evaluating concurrently.")
        if st.button("⏹️ Stop Live Engine"):
            st.session_state.stop_event.set()
            st.session_state.bot_thread.join(timeout=5)
            st.session_state.is_running = False
            st.rerun()

with tab_backtest:
    st.markdown("**Note:** Changing the Loxx Multiplier in the sidebar instantly applies to this backtest.")
    bt_und = st.selectbox("Index", ["NIFTY", "BANKNIFTY"])
    bt_date = st.date_input("Trade Date", value=dt.date(2026, 8, 27))
    if st.button("🚀 Run Backtest with Sidebar Parameters"):
        with st.spinner(f"Running {bt_und} backtest at Loxx {INDEX_PARAMS[bt_und]['loxx_mult']}..."):
            try:
                res_df, trades_dict = run_backtest_suite(bt_und, bt_date, INDEX_PARAMS[bt_und]["expiry"])
                if not res_df.empty: 
                    st.dataframe(res_df, use_container_width=True)
                    st.write("### Strategy Trade Logs")
                    t_tabs = st.tabs(["Spot 13 EMA", "Prem 13 EMA", "Prem 15 EMA", "Prem 21 EMA"])
                    strat_keys = ["SPOT_13_EMA", "PREM_13_EMA", "PREM_15_EMA", "PREM_21_EMA"]
                    for i, key in enumerate(strat_keys):
                        with t_tabs[i]:
                            df_res = trades_dict[key]
                            if not df_res.empty: st.dataframe(df_res, use_container_width=True)
                            else: st.info(f"No trades generated under {key}.")
                else: 
                    st.info("No trades triggered for these parameters.")
            except Exception as e:
                st.error(f"Backtest execution failed: {e}")
