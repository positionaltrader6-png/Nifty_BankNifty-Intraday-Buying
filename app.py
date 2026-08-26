"""
================================================================================
NIFTY Pro Momentum Engine — Streamlit Dashboard (Live v7.3 + Backtest Suite)
================================================================================
"""

import streamlit as st
import threading
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
st.set_page_config(page_title="NIFTY Momentum Engine", page_icon="📈", layout="wide")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)

IST = pytz.timezone("Asia/Kolkata")
def get_ist_now():
    return dt.datetime.now(IST).replace(tzinfo=None)

UNDERLYING = "NIFTY"
INDEX_TOKEN = "99926000"
STRIKE_STEP = 50
LOT_SIZE = 65
INTERVAL = "THREE_MINUTE"
INDICATOR_WARMUP_DAYS = 5
LOXX_MULT_NIFTY = 0.9

KC_PERIOD = 20
KC_ATR_MULT = 1.0
EMA_FAST = 9
EMA_SLOW = 21
EMA_SL_PERIOD = 13
ADX_PERIOD = 14
ADX_MIN = 20.0
CHOP_PERIOD = 14
LOXX_PERIOD = 40
LOXX_DEV = 1.0

STOP_LOSS_PCT = 10.0
MAX_TRADES_PER_DAY = 3
ENTRY_WINDOW_START = dt.time(9, 30)
ENTRY_WINDOW_END = dt.time(14, 30)
EOD_EXIT_TIME = dt.time(15, 0)
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

DATA_FRESHNESS_RETRIES = 3
DATA_FRESHNESS_RETRY_DELAY = 5

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENT_MASTER_URL_FALLBACK = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

TRADE_HEADERS = [
    "trading_date", "expiry_date", "trade_num", "direction",
    "entry_time", "exit_time", "spot_at_entry", "atm_strike", "option_symbol",
    "entry_price", "exit_price", "exit_reason", "pnl_pct",
    "spot_13_ema_sl", "premium_13_ema_sl"
]

HEARTBEAT_HEADERS = [
    "timestamp", "underlying", "spot_close", "signal_state",
    "loxx_width", "loxx_width_avg", "kc_sloping_up", "kc_sloping_down", "momentum_accelerating"
]

# ── Costs & Slippage (NSE Options) ──
ENABLE_TRANSACTION_COSTS = True
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.0625 / 100
EXCHANGE_TXN_PCT = 0.03503 / 100
GST_PCT = 18.0 / 100
SEBI_CHARGES_PCT = 0.0001 / 100
STAMP_DUTY_PCT = 0.003 / 100
SLIPPAGE_PCT = 0.10 / 100

# ─── 2. AUTHENTICATION & API HELPERS ───
def login():
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=st.secrets["API_KEY"])
    totp = pyotp.TOTP(st.secrets["TOTP_SECRET"]).now()
    data = obj.generateSession(st.secrets["CLIENT_ID"], st.secrets["PASSWORD"], totp)
    if not data.get("status"):
        raise RuntimeError(f"SmartAPI login failed: {data}")
    return obj

def get_sheet_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    return gspread.authorize(creds)

def send_telegram_alert(message):
    try:
        token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram failed: {e}")

def build_instrument_lookup():
    try:
        resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        resp = requests.get(INSTRUMENT_MASTER_URL_FALLBACK, timeout=30)
        resp.raise_for_status()
    instruments = resp.json()
    lookup = {}
    for inst in instruments:
        if inst.get("name") == UNDERLYING and inst.get("instrumenttype") == "OPTIDX":
            lookup[inst["symbol"]] = inst["token"]
    return lookup

def fetch_option_token(lookup, expiry_date, strike, side):
    exp_str = expiry_date.strftime("%d%b%y").upper()
    symbol = f"NIFTY{exp_str}{strike}{side}"
    return symbol, lookup.get(symbol)

def get_ltp(smart_obj, tradingsymbol, symboltoken, exchange="NFO"):
    for attempt in range(1, 4):
        try:
            resp = smart_obj.ltpData(exchange, tradingsymbol, symboltoken)
            if not resp.get("status"):
                time.sleep(1)
                smart_obj = login()
                continue
            ltp = resp.get("data", {}).get("ltp")
            if ltp is not None:
                return float(ltp), smart_obj
            time.sleep(1)
        except Exception as e:
            logging.warning(f"LTP fetch failed for {tradingsymbol} ({attempt}/3): {e}")
            time.sleep(1)
    return None, smart_obj

def get_candles_with_relogin(smart_obj, token, from_dt, to_dt, exchange="NSE", interval=INTERVAL):
    params = {
        "exchange": exchange, "symboltoken": token, "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"), "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    for attempt in range(1, 5):
        try:
            resp = smart_obj.getCandleData(params)
            if not resp.get("status"):
                time.sleep(2)
                smart_obj = login()
                continue
            df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
            return df, smart_obj
        except Exception:
            time.sleep(1)
    return pd.DataFrame(), smart_obj

def append_to_sheet(sh, tab_name, header, row_data):
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=len(header))
        ws.update([header], value_input_option="USER_ENTERED")
    if not ws.get_all_values():
        ws.update([header], value_input_option="USER_ENTERED")
    ws.append_row(row_data, value_input_option="USER_ENTERED")

# ─── 3. TECHNICAL INDICATORS ───
def compute_keltner(df, period=KC_PERIOD, mult=KC_ATR_MULT):
    high, low, close = df["high"], df["low"], df["close"]
    basis = close.ewm(span=period, adjust=False).mean()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return basis + mult * atr, basis, basis - mult * atr

def compute_adx_adxr(df, period=ADX_PERIOD):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    adxr = (adx + adx.shift(period)) / 2.0
    return adx, adxr, plus_di, minus_di

def compute_chop(df, period=CHOP_PERIOD):
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift(1)).abs()
    tr3 = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_sum = tr.rolling(period).sum()
    max_high = df['high'].rolling(period).max()
    min_low = df['low'].rolling(period).min()
    denom = (max_high - min_low).replace(0, np.nan)
    return 100 * np.log10(atr_sum / denom) / np.log10(period)

def compute_lwma(series, period):
    weights = np.arange(1, period + 1)
    sum_weights = weights.sum()
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / sum_weights, raw=True)

def compute_loxx_hlhvb(df, period=LOXX_PERIOD, dev=LOXX_DEV):
    half_period = int(period / 2)
    hull_period = int(np.sqrt(period))
    wma_half = compute_lwma(df["close"], half_period)
    wma_full = compute_lwma(df["close"], period)
    raw_hma = 2.0 * wma_half - wma_full
    buffer_me = compute_lwma(raw_hma, hull_period)
    hl_range = df["high"] - df["low"]
    deviation = hl_range.rolling(period).std(ddof=1)
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

# ─── 4. LIVE ENGINE THREAD (v7.3 HYBRID MATCHED) ───
def run_live_trading_engine(stop_event, sheet_id, nifty_expiry):
    ACTIVE_POSITION = None
    DAILY_TRADE_COUNT = 0
    TRADING_DATE = get_ist_now().date()
    mode_text = "PAPER TRADING"

    send_telegram_alert(f"🚀 *NIFTY Pro Momentum Engine v7.3 Started*\nMode: `{mode_text}`\nExpiry: {nifty_expiry.strftime('%d-%b-%Y')}")

    try:
        smart = login()
        gc = get_sheet_client()
        sh = gc.open_by_key(sheet_id)
        lookup = build_instrument_lookup()
    except Exception as e:
        send_telegram_alert(f"⚠️ *Startup Error:* {e}")
        return

    def execute_order(action, symbol, token, qty, price, smart_obj):
        logging.info(f"[PAPER] {action}: {symbol} @ {price}")
        return {"status": True}

    last_processed_ts = None

    while not stop_event.is_set():
        now = get_ist_now()
        current_time = now.time()

        if current_time < MARKET_OPEN:
            time.sleep(10)
            continue
        if current_time >= MARKET_CLOSE:
            send_telegram_alert("🛑 *Market Closed (3:30 PM). Engine Shutting Down.*")
            break

        seconds_until_next_candle = (3 - (now.minute % 3)) * 60 - now.second
        if seconds_until_next_candle <= 0:
            seconds_until_next_candle = 180

        sleep_end = now + dt.timedelta(seconds=seconds_until_next_candle + 15)
        while get_ist_now() < sleep_end:
            if stop_event.is_set():
                send_telegram_alert("🛑 *Engine Stopped via Dashboard.*")
                return
            time.sleep(1)

        day_from = dt.datetime.combine(TRADING_DATE, MARKET_OPEN)
        spot_df, smart = get_candles_with_relogin(smart, INDEX_TOKEN, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now())
        if spot_df.empty:
            continue

        spot_df = add_intraday_indicators(spot_df)
        day_df = spot_df[spot_df["timestamp"].dt.date == TRADING_DATE].reset_index(drop=True)

        if len(day_df) < 2:
            for fresh_attempt in range(1, DATA_FRESHNESS_RETRIES + 1):
                time.sleep(DATA_FRESHNESS_RETRY_DELAY)
                spot_df, smart = get_candles_with_relogin(smart, INDEX_TOKEN, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now())
                if spot_df.empty:
                    continue
                spot_df = add_intraday_indicators(spot_df)
                day_df = spot_df[spot_df["timestamp"].dt.date == TRADING_DATE].reset_index(drop=True)
                if len(day_df) >= 2:
                    break

        if len(day_df) < 2:
            continue

        row = day_df.iloc[-2]
        ts, close, high, low, ema_sl = row["timestamp"], row["close"], row["high"], row["low"], row["ema_sl"]
        t = ts.time()

        if last_processed_ts == ts:
            continue
        last_processed_ts = ts

        # ── 1. Positional Management & Exits ──
        if ACTIVE_POSITION is not None:
            symbol = ACTIVE_POSITION["symbol"]
            opt_df, smart = get_candles_with_relogin(smart, ACTIVE_POSITION["token"], day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now(), exchange="NFO")
            if not opt_df.empty:
                opt_df["ema_sl"] = opt_df["close"].ewm(span=EMA_SL_PERIOD, adjust=False).mean()
                opt_row = opt_df[opt_df["timestamp"] == ts]
                candle_opt_price = opt_row.iloc[0]["close"] if not opt_row.empty else opt_df.iloc[-1]["close"]
                opt_ema_sl = opt_row.iloc[0]["ema_sl"] if not opt_row.empty else opt_df.iloc[-1]["ema_sl"]

                ltp_price, smart = get_ltp(smart, symbol, ACTIVE_POSITION["token"], exchange="NFO")
                current_opt_price = ltp_price if ltp_price is not None else candle_opt_price

                ACTIVE_POSITION["latest_opt_price"] = current_opt_price
                ACTIVE_POSITION["latest_opt_sl"] = opt_ema_sl

                entry_price = ACTIVE_POSITION["entry_price"]
                pnl_pct = ((current_opt_price - entry_price) / entry_price) * 100.0

                exit_reason = None
                if pnl_pct <= -STOP_LOSS_PCT:
                    exit_reason = "HARD_STOP_LOSS_HIT"
                elif t >= EOD_EXIT_TIME:
                    exit_reason = "EOD_SQUAREOFF"
                elif ACTIVE_POSITION["direction"] == "CE" and close < ema_sl:
                    exit_reason = "SPOT_EMA13_CROSSDOWN"
                elif ACTIVE_POSITION["direction"] == "PE" and close > ema_sl:
                    exit_reason = "SPOT_EMA13_CROSSUP"

                if exit_reason is None:
                    active_sl = ema_sl
                    last_sl = ACTIVE_POSITION.get("last_alerted_sl", active_sl)
                    sl_improved = (
                        (ACTIVE_POSITION["direction"] == "CE" and active_sl > last_sl + 0.01) or
                        (ACTIVE_POSITION["direction"] == "PE" and active_sl < last_sl - 0.01)
                    )
                    if sl_improved:
                        trail_msg = (
                            f"🔵 *NIFTY SL Trailed* 🔵\n\n*Symbol:* {symbol}\n*Time:* {ts.strftime('%H:%M')}\n"
                            f"*Spot Price:* {close:.2f}\n*Current PnL:* {pnl_pct:.2f}%\n*Revised Spot 13 EMA SL:* {active_sl:.2f}"
                        )
                        send_telegram_alert(trail_msg)
                        ACTIVE_POSITION["last_alerted_sl"] = active_sl

                if exit_reason:
                    execute_order("SELL", symbol, ACTIVE_POSITION["token"], LOT_SIZE, current_opt_price, smart)
                    msg = (
                        f"🔴 *NIFTY Exit ({mode_text})* 🔴\n\n*Symbol:* {symbol}\n*Time:* {ts.strftime('%H:%M')}\n"
                        f"*Spot Price:* {close:.2f}\n*Exit Price:* Rs {current_opt_price:.2f}\n*SL - Spot 13 EMA:* {ema_sl:.2f}\n"
                        f"*SL - Premium 13 EMA:* {opt_ema_sl:.2f}\n*Reason:* {exit_reason}\n*PnL:* {pnl_pct:.2f}%"
                    )
                    send_telegram_alert(msg)

                    trade_row = [
                        TRADING_DATE.isoformat(), nifty_expiry.isoformat(), int(ACTIVE_POSITION["trade_num"]), str(ACTIVE_POSITION["direction"]),
                        ACTIVE_POSITION["entry_ts"].strftime("%H:%M:%S"), ts.strftime("%H:%M:%S"), float(round(ACTIVE_POSITION["spot_entry"], 2)),
                        int(ACTIVE_POSITION["atm_strike"]), str(symbol), float(round(entry_price, 2)), float(round(current_opt_price, 2)),
                        str(exit_reason), float(round(pnl_pct, 2)), float(round(ema_sl, 2)), float(round(opt_ema_sl, 2))
                    ]
                    append_to_sheet(sh, f"Live_Trades_{TRADING_DATE.isoformat()}", TRADE_HEADERS, trade_row)
                    ACTIVE_POSITION = None

        # ── 2. Entries ──
        kc_up, kc_basis, kc_low = row["kc_upper"], row["kc_basis"], row["kc_lower"]
        kc_basis_prev = row["kc_basis_prev"]
        hlhvb_up, hlhvb_me, hlhvb_dn = row["hlhvb_up"], row["hlhvb_me"], row["hlhvb_dn"]
        hlhvb_me_prev = row["hlhvb_me_prev"]
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
        is_valid_loxx_width = (loxx_width <= loxx_width_avg * LOXX_MULT_NIFTY) if pd.notna(loxx_width_avg) else True
        is_loxx_sloping_up = (hlhvb_me > hlhvb_me_prev) if pd.notna(hlhvb_me_prev) else False
        is_loxx_sloping_down = (hlhvb_me < hlhvb_me_prev) if pd.notna(hlhvb_me_prev) else False
        loxx_ce_valid = (close > hlhvb_dn) and is_loxx_sloping_up
        loxx_pe_valid = (close < hlhvb_up) and is_loxx_sloping_down

        signal = None
        if (close > kc_up) and (ema9 > ema21_entry) and momentum_accelerating and (plus_di > minus_di) and is_kc_sloping_up and is_valid_ce_span and loxx_ce_valid and is_valid_loxx_width:
            signal = "CE"
        elif (close < kc_low) and (ema9 < ema21_entry) and momentum_accelerating and (minus_di > plus_di) and is_kc_sloping_down and is_valid_pe_span and loxx_pe_valid and is_valid_loxx_width:
            signal = "PE"

        hb_status = f"ACTIVE ({ACTIVE_POSITION['direction']}) | Opt: {ACTIVE_POSITION.get('latest_opt_price', 0.0):.1f} | SL: {ACTIVE_POSITION.get('latest_opt_sl', 0.0):.1f}" if ACTIVE_POSITION else ("SIGNAL: " + signal if signal else "WAITING")
        hb_row = [
            ts.strftime("%Y-%m-%d %H:%M:%S"), UNDERLYING, float(round(close, 2)), hb_status,
            float(round(loxx_width, 2)) if pd.notna(loxx_width) else 0.0,
            float(round(loxx_width_avg, 2)) if pd.notna(loxx_width_avg) else 0.0,
            bool(is_kc_sloping_up), bool(is_kc_sloping_down), bool(momentum_accelerating)
        ]
        append_to_sheet(sh, f"Live_Heartbeat_{TRADING_DATE.isoformat()}", HEARTBEAT_HEADERS, hb_row)

        if signal and (ACTIVE_POSITION is None) and (DAILY_TRADE_COUNT < MAX_TRADES_PER_DAY) and (ENTRY_WINDOW_START <= t <= ENTRY_WINDOW_END):
            atm_strike = int(round(close / STRIKE_STEP) * STRIKE_STEP)
            symbol, token = fetch_option_token(lookup, nifty_expiry, atm_strike, signal)

            if token:
                opt_df, smart = get_candles_with_relogin(smart, token, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now(), exchange="NFO")
                if not opt_df.empty:
                    opt_df["ema_sl"] = opt_df["close"].ewm(span=EMA_SL_PERIOD, adjust=False).mean()
                    opt_row = opt_df[opt_df["timestamp"] == ts]
                    candle_entry = opt_row.iloc[0]["close"] if not opt_row.empty else op
