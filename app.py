"""
================================================================================
NIFTY Pro Momentum Engine — Streamlit Cloud Edition (v8.0)
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

# ─── 1. PAGE CONFIGURATION ───
st.set_page_config(page_title="Pro Momentum Engine", page_icon="📈", layout="centered")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)

IST = pytz.timezone("Asia/Kolkata")
def get_ist_now():
    return dt.datetime.now(IST).replace(tzinfo=None)

# ─── 2. INDICATOR MATH ───
def compute_keltner(df, period=20, mult=1.0):
    high, low, close = df["high"], df["low"], df["close"]
    basis = close.ewm(span=period, adjust=False).mean()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return basis + mult * atr, basis, basis - mult * atr

def compute_adx_adxr(df, period=14):
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

def compute_chop(df, period=14):
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

def compute_loxx_hlhvb(df, period=40, dev=1.0):
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
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_sl"] = df["close"].ewm(span=13, adjust=False).mean()
    df["kc_upper"], df["kc_basis"], df["kc_lower"] = compute_keltner(df)
    df["kc_basis_prev"] = df["kc_basis"].shift(1)
    df["hlhvb_up"], df["hlhvb_me"], df["hlhvb_dn"] = compute_loxx_hlhvb(df)
    df["hlhvb_me_prev"] = df["hlhvb_me"].shift(1)
    df["loxx_width"] = df["hlhvb_up"] - df["hlhvb_dn"]
    df["loxx_width_avg"] = df["loxx_width"].rolling(window=10).mean()
    df["adx"], df["adxr"], df["plus_di"], df["minus_di"] = compute_adx_adxr(df)
    df["chop"] = compute_chop(df)
    df["adx_prev"], df["adxr_prev"], df["chop_prev"] = df["adx"].shift(1), df["adxr"].shift(1), df["chop"].shift(1)
    return df

# ─── 3. BACKGROUND TRADING ENGINE ───
def run_trading_bot(stop_event, sheet_id, nifty_expiry, is_paper_trading):
    UNDERLYING = "NIFTY"
    INDEX_TOKEN = "99926000"
    STRIKE_STEP = 50
    QUANTITY = 65
    INTERVAL = "THREE_MINUTE"
    INDICATOR_WARMUP_DAYS = 5
    STOP_LOSS_PCT = 10.0
    MAX_TRADES_PER_DAY = 3
    ENTRY_WINDOW_START, ENTRY_WINDOW_END = dt.time(9, 30), dt.time(14, 30)
    EOD_EXIT_TIME, MARKET_OPEN = dt.time(15, 0), dt.time(9, 15)

    ACTIVE_POSITION = None
    DAILY_TRADE_COUNT = 0
    TRADING_DATE = get_ist_now().date()

    def send_telegram_alert(message):
        try:
            url = f"https://api.telegram.org/bot{st.secrets['TELEGRAM_BOT_TOKEN']}/sendMessage"
            requests.post(url, json={"chat_id": st.secrets["TELEGRAM_CHAT_ID"], "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            logging.error(f"Telegram failed: {e}")

    def login():
        from SmartApi import SmartConnect
        obj = SmartConnect(api_key=st.secrets["API_KEY"])
        totp = pyotp.TOTP(st.secrets["TOTP_SECRET"]).now()
        data = obj.generateSession(st.secrets["CLIENT_ID"], st.secrets["PASSWORD"], totp)
        if not data.get("status"): raise RuntimeError("Login failed")
        return obj

    def get_sheet_client():
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        # Securely read Google Cloud credentials from Streamlit Secrets
        creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
        return gspread.authorize(creds)
        
    def append_to_sheet(sh, tab_name, header, row_data):
        try: ws = sh.worksheet(tab_name)
        except:
            ws = sh.add_worksheet(title=tab_name, rows=2000, cols=len(header))
            ws.update([header], value_input_option="USER_ENTERED")
        if not ws.get_all_values(): ws.update([header], value_input_option="USER_ENTERED")
        ws.append_row(row_data, value_input_option="USER_ENTERED")

    def get_candles(smart_obj, token, from_dt, to_dt, exchange="NSE"):
        params = {"exchange": exchange, "symboltoken": token, "interval": INTERVAL,
                  "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"), "todate": to_dt.strftime("%Y-%m-%d %H:%M")}
        for _ in range(4):
            try:
                resp = smart_obj.getCandleData(params)
                if resp.get("status"):
                    df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
                    return df, smart_obj
                time.sleep(2)
                smart_obj = login()
            except: time.sleep(1)
        return pd.DataFrame(), smart_obj

    def execute_order(action, symbol, token, qty, price, smart_obj):
        if is_paper_trading:
            logging.info(f"[PAPER] {action}: {symbol} @ {price}")
            return {"status": True}
        order_params = {
            "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
            "transactiontype": action, "exchange": "NFO", "ordertype": "MARKET",
            "producttype": "INTRADAY", "duration": "DAY", "quantity": str(qty)
        }
        return smart_obj.placeOrder(order_params)

    mode_text = "PAPER TRADING" if is_paper_trading else "LIVE MONEY"
    send_telegram_alert(f"🚀 *NIFTY Engine Started*\nMode: `{mode_text}`\nExpiry: {nifty_expiry.strftime('%d-%b-%Y')}")
    
    try:
        smart = login()
        gc = get_sheet_client()
        sh = gc.open_by_key(sheet_id)
        resp = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=30)
        lookup = {inst["symbol"]: inst["token"] for inst in resp.json() if inst.get("name") == "NIFTY" and inst.get("instrumenttype") == "OPTIDX"}
    except Exception as e:
        send_telegram_alert(f"⚠️ *Startup Error:* {e}")
        return

    while not stop_event.is_set():
        now = get_ist_now()
        current_time = now.time()

        if current_time < MARKET_OPEN:
            time.sleep(10)
            continue
        if current_time >= dt.time(15, 15):
            send_telegram_alert("🛑 *Market Closed. Engine Stopped.*")
            break

        sec_to_wait = (3 - (now.minute % 3)) * 60 - now.second
        if sec_to_wait <= 0: sec_to_wait = 180
        
        sleep_end = now + dt.timedelta(seconds=sec_to_wait + 10)
        while get_ist_now() < sleep_end:
            if stop_event.is_set():
                send_telegram_alert("🛑 *Engine Stopped via Dashboard.*")
                return
            time.sleep(1)

        day_from = dt.datetime.combine(TRADING_DATE, MARKET_OPEN)
        spot_df, smart = get_candles(smart, INDEX_TOKEN, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now())
        if spot_df.empty: continue

        spot_df = add_intraday_indicators(spot_df)
        day_df = spot_df[spot_df["timestamp"].dt.date == TRADING_DATE].reset_index(drop=True)
        if len(day_df) < 2: continue

        row = day_df.iloc[-2]
        ts, close, high, low, ema_sl = row["timestamp"], row["close"], row["high"], row["low"], row["ema_sl"]
        t = ts.time()

        # ── 1. Exits ──
        if ACTIVE_POSITION is not None:
            opt_df, smart = get_candles(smart, ACTIVE_POSITION["token"], day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now(), exchange="NFO")
            if not opt_df.empty:
                opt_df["ema_sl"] = opt_df["close"].ewm(span=13, adjust=False).mean()
                opt_row = opt_df[opt_df["timestamp"] == ts]
                curr_price = opt_row.iloc[0]["close"] if not opt_row.empty else opt_df.iloc[-1]["close"]
                opt_sl = opt_row.iloc[0]["ema_sl"] if not opt_row.empty else opt_df.iloc[-1]["ema_sl"]

                pnl_pct = ((curr_price - ACTIVE_POSITION["entry_price"]) / ACTIVE_POSITION["entry_price"]) * 100.0
                exit_reason = None
                if pnl_pct <= -STOP_LOSS_PCT: exit_reason = "HARD_STOP_LOSS_HIT"
                elif t >= EOD_EXIT_TIME: exit_reason = "EOD_SQUAREOFF"
                elif ACTIVE_POSITION["direction"] == "CE" and close < ema_sl: exit_reason = "SPOT_EMA13_CROSSDOWN"
                elif ACTIVE_POSITION["direction"] == "PE" and close > ema_sl: exit_reason = "SPOT_EMA13_CROSSUP"

                if exit_reason:
                    execute_order("SELL", ACTIVE_POSITION["symbol"], ACTIVE_POSITION["token"], QUANTITY, curr_price, smart)
                    msg = (f"🔴 *NIFTY Exit ({mode_text})* 🔴\n\n*Symbol:* {ACTIVE_POSITION['symbol']}\n"
                           f"*Time:* {ts.strftime('%H:%M')}\n*Spot Price:* {close:.2f}\n*Exit Price:* Rs {curr_price:.2f}\n"
                           f"*SL - Spot 13 EMA:* {ema_sl:.2f}\n*SL - Prem 13 EMA:* {opt_sl:.2f}\n"
                           f"*Reason:* {exit_reason}\n*PnL:* {pnl_pct:.2f}%")
                    send_telegram_alert(msg)
                    
                    trade_row = [
                        TRADING_DATE.isoformat(), nifty_expiry.isoformat(), ACTIVE_POSITION["trade_num"], 
                        mode_text, ACTIVE_POSITION["direction"], ACTIVE_POSITION["entry_ts"].strftime("%H:%M:%S"),
                        ts.strftime("%H:%M:%S"), float(round(ACTIVE_POSITION["spot_entry"], 2)),
                        ACTIVE_POSITION["atm_strike"], ACTIVE_POSITION["symbol"], float(round(ACTIVE_POSITION["entry_price"], 2)),
                        float(round(curr_price, 2)), exit_reason, float(round(pnl_pct, 2))
                    ]
                    append_to_sheet(sh, f"Trades_{TRADING_DATE.isoformat()}", ["Date", "Expiry", "Num", "Mode", "Dir", "Entry Time", "Exit Time", "Spot Entry", "Strike", "Symbol", "Entry Price", "Exit Price", "Reason", "PnL %"], trade_row)
                    ACTIVE_POSITION = None

        # ── 2. Entries ──
        momentum_ok = (row["adx"] > 20.0) and (row["adx"] > row["adxr"]) and (row["adx"] > row["adx_prev"]) and (row["chop"] < row["chop_prev"])
        loxx_ok = row["loxx_width"] <= row["loxx_width_avg"] * 0.9 if pd.notna(row["loxx_width_avg"]) else True
        loxx_up = row["hlhvb_me"] > row["hlhvb_me_prev"] if pd.notna(row["hlhvb_me_prev"]) else False
        loxx_dn = row["hlhvb_me"] < row["hlhvb_me_prev"] if pd.notna(row["hlhvb_me_prev"]) else False

        signal = None
        if (close > row["kc_upper"]) and (row["ema9"] > row["ema21"]) and momentum_ok and (row["plus_di"] > row["minus_di"]) and (row["kc_basis"] > row["kc_basis_prev"]) and (low > row["kc_lower"]) and (close > row["hlhvb_dn"]) and loxx_up and loxx_ok:
            signal = "CE"
        elif (close < row["kc_lower"]) and (row["ema9"] < row["ema21"]) and momentum_ok and (row["minus_di"] > row["plus_di"]) and (row["kc_basis"] < row["kc_basis_prev"]) and (high < row["kc_upper"]) and (close < row["hlhvb_up"]) and loxx_dn and loxx_ok:
            signal = "PE"

        # Log Heartbeat
        hb_status = f"ACTIVE ({ACTIVE_POSITION['direction']}) | Opt: {ACTIVE_POSITION.get('latest_opt_price', 0.0):.1f} | SL: {ACTIVE_POSITION.get('latest_opt_sl', 0.0):.1f}" if ACTIVE_POSITION else ("SIGNAL: " + signal if signal else "WAITING")
        hb_row = [ts.strftime("%Y-%m-%d %H:%M:%S"), UNDERLYING, float(round(close, 2)), hb_status, float(round(row["loxx_width"], 2)) if pd.notna(row["loxx_width"]) else 0.0, float(round(row["loxx_width_avg"], 2)) if pd.notna(row["loxx_width_avg"]) else 0.0, bool(row["kc_basis"] > row["kc_basis_prev"]), bool(row["kc_basis"] < row["kc_basis_prev"]), bool(momentum_ok)]
        append_to_sheet(sh, f"Heartbeat_{TRADING_DATE.isoformat()}", ["Time", "Index", "Spot", "Status", "Loxx Width", "Loxx Avg", "KC Up", "KC Dn", "Mom Accel"], hb_row)

        if signal and (ACTIVE_POSITION is None) and (DAILY_TRADE_COUNT < MAX_TRADES_PER_DAY) and (ENTRY_WINDOW_START <= t <= ENTRY_WINDOW_END):
            strike = int(round(close / STRIKE_STEP) * STRIKE_STEP)
            exp_str = nifty_expiry.strftime("%d%b%y").upper()
            sym = f"NIFTY{exp_str}{strike}{signal}"
            tok = lookup.get(sym)

            if tok:
                opt_df, smart = get_candles(smart, tok, day_from - dt.timedelta(days=INDICATOR_WARMUP_DAYS), get_ist_now(), exchange="NFO")
                if not opt_df.empty:
                    opt_df["ema_sl"] = opt_df["close"].ewm(span=13, adjust=False).mean()
                    opt_row = opt_df[opt_df["timestamp"] == ts]
                    ent_price = opt_row.iloc[0]["close"] if not opt_row.empty else opt_df.iloc[-1]["close"]
                    opt_sl = opt_row.iloc[0]["ema_sl"] if not opt_row.empty else opt_df.iloc[-1]["ema_sl"]

                    execute_order("BUY", sym, tok, QUANTITY, ent_price, smart)
                    DAILY_TRADE_COUNT += 1
                    ACTIVE_POSITION = {"symbol": sym, "token": tok, "direction": signal, "entry_price": ent_price, "spot_entry": close, "atm_strike": strike, "trade_num": DAILY_TRADE_COUNT, "entry_ts": ts}
                    
                    msg = (f"🟢 *NIFTY Entry ({mode_text})* 🟢\n\n*Symbol:* {sym}\n*Time:* {ts.strftime('%H:%M')}\n"
                           f"*Spot Price:* {close:.2f}\n*Entry Price:* Rs {ent_price:.2f}\n*SL - Spot 13 EMA:* {ema_sl:.2f}\n*SL - Prem 13 EMA:* {opt_sl:.2f}")
                    send_telegram_alert(msg)

# ─── 4. GLOBAL THREADING STATE ───
if "bot_thread" not in st.session_state: st.session_state.bot_thread = None
if "stop_event" not in st.session_state: st.session_state.stop_event = threading.Event()
if "is_running" not in st.session_state: st.session_state.is_running = False

# ─── 5. USER INTERFACE ───
st.title("📈 Pro Momentum Engine")

tab_live, tab_config = st.tabs(["🔴 Live Trading", "⚙️ Configuration"])

with tab_config:
    st.subheader("System Configuration")
    sheet_id_input = st.text_input("Google Sheet ID", value="1tiVgr1CdbKVrnf-HJM1cVDYy8ltrLo6VnRaTK9IJn_4")
    nifty_expiry_input = st.date_input("NIFTY Expiry Date", value=dt.date(2026, 9, 1))
    st.info("Changes made here will be applied the next time you click 'Start Engine'.")

with tab_live:
    st.subheader("Execution Controls")
    execution_mode = st.radio("Execution Mode", options=["Paper Trading (Simulation)", "Live Money (Real Execution)"], horizontal=True, disabled=st.session_state.is_running)
    is_paper_trading = (execution_mode == "Paper Trading (Simulation)")
    st.divider()

    if not st.session_state.is_running:
        if st.button("▶️ Start Engine", use_container_width=True, type="primary"):
            st.session_state.stop_event.clear()
            st.session_state.bot_thread = threading.Thread(
                target=run_trading_bot, 
                args=(st.session_state.stop_event, sheet_id_input, nifty_expiry_input, is_paper_trading),
                daemon=True
            )
            st.session_state.bot_thread.start()
            st.session_state.is_running = True
            st.rerun()
    else:
        st.success("🟢 Engine is currently monitoring the market.")
        if st.button("⏹️ Stop Engine", use_container_width=True):
            st.session_state.stop_event.set()
            st.session_state.bot_thread.join(timeout=5)
            st.session_state.is_running = False
            st.rerun()
                 
