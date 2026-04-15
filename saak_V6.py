import os
import time
import threading
import logging
from datetime import datetime
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import telebot
from flask import Flask, jsonify, render_template
from flask_cors import CORS
# =========================
# CONFIGURATION
# =========================
TELEGRAM_TOKEN   = "8386293337:AAE5TJOM3VfrUb0dF313eBsRQxf_Rkt4ylI"
TELEGRAM_CHAT_ID = "7858967749"

MT5_LOGIN     = 24367452
MT5_PASSWORD  = "UY61&jYZ"
MT5_SERVER    = "VantageInternational-Demo"

SYMBOL                = "XAUUSD"
GRID_STEP_PRICE       = 10.0
SINGLE_TRADE_TP_PRICE = 10.0
TARGET_PROFIT         = 50.0
TARGET_PROFIT_G2      = 10.0
DAILY_SL_USD          = 3000.0
GRID_LOT_MULTIPLIERS  = [2, 4, 4]
FIXED_LOT             = 0.05
MAGIC                 = 777777
COMMISSION_PER_LOT_RT = 7.0

BIAS_MODE             = "H4_H1"
MIN_CONFIDENCE        = 40
SR_LOOKBACK           = 50

FILLING_PRIORITY = [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]
discovered_filling = mt5.ORDER_FILLING_RETURN

# =========================
# STATE
# =========================
lock = threading.RLock()  # RLock to prevent deadlock if add_log is called inside a locked section
state = {
    "running": False,
    "basket_active": False,
    "direction": None,
    "entry_price": None,
    "trades": [],
    "triggered": [],
    "session_pnl": 0.0,
    "closed_trades": 0,
    "daily_loss": 0.0,
    "max_daily_loss": DAILY_SL_USD,
    "live_price": 0.0,
    "live_spread": 0.0,
    "live_pnl": 0.0,
    "m30_bias": "N/A",
    "h1_bias": "N/A",
    "h4_bias": "N/A",
    "d1_bias": "N/A",
    "last_bar_time": None,
    "closed_on_bar": None,
    "tp_triggered": False,
    "g2_be_touched": False,
    "g2_be_bar": 0,
    "log": [],
    "telegram_status": "OFFLINE",
}
last_heartbeat = [0]  # mutable container for heartbeat timer

def add_log(msg, type_="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "type": type_}
    with lock:
        state["log"].insert(0, entry)
        if len(state["log"]) > 100: state["log"].pop()
    print(f"[{entry['time']}] {msg}")

# =========================
# TELEGRAM BOT
# =========================
tbot = None
if TELEGRAM_TOKEN and len(TELEGRAM_TOKEN) > 10:
    try:
        tbot = telebot.TeleBot(TELEGRAM_TOKEN)
        state["telegram_status"] = "ONLINE"
        
        @tbot.message_handler(commands=['start'])
        def t_start(m):
            with lock: state["running"] = True
            tbot.reply_to(m, "▶️ SAAK Bot STARTED via Telegram.")
            add_log("Telegram Kill-switch reversed: Bot Started", "info")

        @tbot.message_handler(commands=['stop'])
        def t_stop(m):
            with lock: state["running"] = False
            tbot.reply_to(m, "🛑 SAAK Bot STOPPED. Emergency protocol activated.")
            add_log("Telegram Kill-switch triggered: Bot Paused", "error")

        def _poll():
            while True:
                try: tbot.polling(none_stop=True)
                except Exception as e: time.sleep(5)
        threading.Thread(target=_poll, daemon=True).start()
    except Exception as e:
        print("Telegram init failed:", e)

def tg_say(msg):
    if tbot and TELEGRAM_CHAT_ID:
        try: tbot.send_message(TELEGRAM_CHAT_ID, msg)
        except: pass

# =========================
# INDICATORS (EXACT REPLICA)
# =========================
EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

SR_ZONE_ATR_MULT = 0.3
SR_MIN_TOUCHES = 2
SR_NEAR_ATR_MULT = 0.5

def calc_ema(series, period): return series.ewm(span=period, adjust=False).mean()
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_macd(series, fast=12, slow=26, signal=9):
    ml = calc_ema(series, fast) - calc_ema(series, slow)
    sl = calc_ema(ml, signal)
    return ml, sl, ml - sl

def calc_atr(df, period=14):
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def add_indicators(df):
    df = df.copy()
    df["ema50"] = calc_ema(df["close"], EMA_FAST)
    df["ema200"] = calc_ema(df["close"], EMA_SLOW)
    df["rsi"] = calc_rsi(df["close"], RSI_PERIOD)
    df["atr"] = calc_atr(df, ATR_PERIOD)
    ml, sl, hist = calc_macd(df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    df["macd"], df["macd_signal"], df["macd_hist"] = ml, sl, hist
    df["body"] = (df["close"] - df["open"]).abs()
    df["upper_wick"] = df["high"] - df[["open","close"]].max(axis=1)
    df["lower_wick"] = df[["open","close"]].min(axis=1) - df["low"]
    df["range"] = df["high"] - df["low"]
    return df

def find_sr_levels(df_slice, atr):
    highs, lows = [], []
    data = df_slice.reset_index(drop=True)
    if len(data) < 5: return []
    for i in range(2, len(data)-2):
        h, l = data["high"].iloc[i], data["low"].iloc[i]
        if h == data["high"].iloc[i-2:i+3].max(): highs.append(h)
        if l == data["low"].iloc[i-2:i+3].min(): lows.append(l)
    def cluster(prices):
        if not prices: return []
        thr = atr * SR_ZONE_ATR_MULT
        levels, used = [], [False]*len(prices)
        for i, p in enumerate(prices):
            if used[i]: continue
            cl = [p]
            used[i] = True
            for j in range(i+1, len(prices)):
                if not used[j] and abs(prices[j]-p) <= thr:
                    cl.append(prices[j])
                    used[j] = True
            if len(cl) >= SR_MIN_TOUCHES:
                levels.append((sum(cl)/len(cl), len(cl)))
        return sorted(levels)
    return cluster(highs + lows)

def detect_trend_score(row, prev_row, df20):
    price, e50, e200 = row["close"], row["ema50"], row["ema200"]
    if price > e50 > e200: ema_score = 2
    elif price < e50 < e200: ema_score = -2
    elif price > e200: ema_score = 1
    elif price < e200: ema_score = -1
    else: ema_score = 0

    mh, pmh = row["macd_hist"], prev_row["macd_hist"]
    if mh > 0 and mh > pmh: macd_score = 1
    elif mh < 0 and mh < pmh: macd_score = -1
    else: macd_score = 0

    if len(df20) < 11: return ema_score + macd_score
    hs, ls = df20["high"].values, df20["low"].values
    hh = hs[-1]>hs[-5]>hs[-10]
    hl = ls[-1]>ls[-5]>ls[-10]
    lh = hs[-1]<hs[-5]<hs[-10]
    ll = ls[-1]<ls[-5]<ls[-10]
    if hh and hl: swing = 2
    elif lh and ll: swing = -2
    elif hh or hl: swing = 1
    elif lh or ll: swing = -1
    else: swing = 0

    return ema_score + macd_score + swing

def detect_patterns(c0, c1, c2, atr):
    patterns = []
    b0, b1 = c0["body"], c1["body"]
    bull0, bear0 = c0["close"]>c0["open"], c0["close"]<c0["open"]
    bull1, bear1 = c1["close"]>c1["open"], c1["close"]<c1["open"]
    bull2, bear2 = c2["close"]>c2["open"], c2["close"]<c2["open"]

    if bear1 and bull0 and c0["close"]>c1["open"] and c0["open"]<c1["close"] and b0>b1*1.1:
        patterns.append(("Bullish Engulfing","BUY",3))
    if bull1 and bear0 and c0["close"]<c1["open"] and c0["open"]>c1["close"] and b0>b1*1.1:
        patterns.append(("Bearish Engulfing","SELL",3))

    lwr = c0["lower_wick"]/c0["range"] if c0["range"]>0 else 0
    if lwr>=0.6 and c0["upper_wick"]<b0*0.5 and b0>=atr*0.1:
        patterns.append(("Bullish Pin Bar","BUY",2))
    uwr = c0["upper_wick"]/c0["range"] if c0["range"]>0 else 0
    if uwr>=0.6 and c0["lower_wick"]<b0*0.5 and b0>=atr*0.1:
        patterns.append(("Bearish Pin Bar","SELL",2))

    if bear2 and c1["body"]<atr*0.3 and bull0 and c0["close"]>(c2["open"]+c2["close"])/2:
        patterns.append(("Morning Star","BUY",3))
    if bull2 and c1["body"]<atr*0.3 and bear0 and c0["close"]<(c2["open"]+c2["close"])/2:
        patterns.append(("Evening Star","SELL",3))

    if c1["high"]<c2["high"] and c1["low"]>c2["low"] and bull0 and c0["close"]>c2["high"]:
        patterns.append(("Inside Bar Breakout","BUY",2))
    if c1["high"]<c2["high"] and c1["low"]>c2["low"] and bear0 and c0["close"]<c2["low"]:
        patterns.append(("Inside Bar Breakdown","SELL",2))

    if bull0 and bull1 and bull2 and c0["close"]>c1["close"]>c2["close"] and b0>=atr*0.3:
        patterns.append(("3 White Soldiers","BUY",3))
    if bear0 and bear1 and bear2 and c0["close"]<c1["close"]<c2["close"] and b0>=atr*0.3:
        patterns.append(("3 Black Crows","SELL",3))
    return patterns

def score_signal(df_window):
    if len(df_window) < 25: return None, 0, None
    c0 = df_window.iloc[-1]
    c1 = df_window.iloc[-2]
    c2 = df_window.iloc[-3]
    prev = c1
    atr_val, rsi = c0.get("atr", 0), c0.get("rsi", 50)
    
    trend_score = detect_trend_score(c0, prev, df_window.tail(20))
    patterns = detect_patterns(c0, c1, c2, atr_val)
    sr_levels = find_sr_levels(df_window.tail(SR_LOOKBACK), atr_val)
    price = c0["close"]

    near_sup = any(abs(price-lvl)<=atr_val*SR_NEAR_ATR_MULT for lvl,_ in sr_levels if lvl<price)
    near_res = any(abs(price-lvl)<=atr_val*SR_NEAR_ATR_MULT for lvl,_ in sr_levels if lvl>price)

    bull_score = bear_score = 0
    best_bull = best_bear = None

    if trend_score > 0: bull_score += min(trend_score, 4)
    elif trend_score < 0: bear_score += min(abs(trend_score), 4)

    for name, direction, score in patterns:
        if direction == "BUY":
            bull_score += score
            if not best_bull or score > best_bull[1]: best_bull = (name, score)
        elif direction == "SELL":
            bear_score += score
            if not best_bear or score > best_bear[1]: best_bear = (name, score)

    if rsi <= 35: bull_score += 2
    elif rsi >= 65: bear_score += 2
    if near_sup: bull_score += 2
    if near_res: bear_score += 2

    mh, pmh = c0.get("macd_hist", 0), prev.get("macd_hist", 0)
    if mh > 0 and mh > pmh: bull_score += 1
    elif mh < 0 and mh < pmh: bear_score += 1

    MAX = 14.0
    if bull_score > bear_score and best_bull:
        return "BUY", min(int(bull_score/MAX*100), 95), best_bull[0]
    elif bear_score > bull_score and best_bear:
        return "SELL", min(int(bear_score/MAX*100), 95), best_bear[0]
    return None, 0, None

# =========================
# MT5 OPERATIONS
# =========================
def connect_mt5():
    if not mt5.initialize(): return False
    if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER): return False
    return True

def get_rates(tf, n=120):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, n)
    if rates is None or len(rates) == 0: return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def get_candle_bias(tf):
    # Returns the bias of the LIVE, evolving candle for a timeframe
    rates = get_rates(tf, 3)
    if rates is None or len(rates) < 1: return "N/A"
    last = rates.iloc[-1]
    if last["close"] > last["open"]: return "BULL"
    if last["close"] < last["open"]: return "BEAR"
    return "NEUTRAL"

def send_order(direction, lot, comment="GRID"):
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick: return None
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if direction == "BUY" else tick.bid
    for mode in FILLING_PRIORITY:
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": SYMBOL,
            "volume": float(lot),
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mode,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            add_log(f"FILL {direction} {lot}L @ {price:.2f} (mode={mode})", "buy" if direction=="BUY" else "sell")
            return res
    add_log(f"REJECTED order {direction} (all modes tried)", "error")
    return None

def close_all():
    positions = mt5.positions_get(symbol=SYMBOL) or []
    closed = 0
    for p in positions:
        if p.magic != MAGIC: continue
        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick: continue
        close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        price = tick.bid if p.type == 0 else tick.ask
        for mode in FILLING_PRIORITY:
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "volume": p.volume,
                "type": close_type,
                "position": p.ticket,
                "price": price,
                "deviation": 20,
                "magic": MAGIC,
                "comment": "CLOSE",
                "type_filling": mode
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
                break
    return closed

def get_positions_pnl():
    positions = mt5.positions_get(symbol=SYMBOL) or []
    return sum(p.profit for p in positions if p.magic == MAGIC)

def get_basket_tp(entry_p, direction, n, current_pos):
    if n <= 0 or not current_pos:
        # Fallback: use entry price + single TP to avoid ZeroDivisionError
        return entry_p + SINGLE_TRADE_TP_PRICE if direction == "BUY" else entry_p - SINGLE_TRADE_TP_PRICE
    if n == 1:
        return entry_p + SINGLE_TRADE_TP_PRICE if direction == "BUY" else entry_p - SINGLE_TRADE_TP_PRICE
    
    total_lots = sum(p.volume for p in current_pos)
    if total_lots == 0:
        return entry_p + SINGLE_TRADE_TP_PRICE if direction == "BUY" else entry_p - SINGLE_TRADE_TP_PRICE
    total_cost = sum(p.price_open * p.volume * 100 for p in current_pos)
    
    target = TARGET_PROFIT if n == 2 else (TARGET_PROFIT_G2 if n == 3 else 0.1)
    
    if direction == "BUY":
        return (target + total_cost) / (total_lots * 100)
    return (total_cost - target) / (total_lots * 100)

# =========================
# MAIN BOT LOOP
# =========================
def bot_thread():
    add_log("Initializing MT5...", "info")
    if not connect_mt5():
        add_log("MT5 Connection Failed. Check terminal.", "error")
        return

    add_log("MT5 Connected. Awaiting ticks...", "info")
    grid_lots = [round(FIXED_LOT * m, 2) for m in GRID_LOT_MULTIPLIERS]
    
    while True:
      try:
        time.sleep(0.5)
        
        with lock:
            running = state["running"]
            b_active = state["basket_active"]
            direction = state["direction"]
            entry_p = state["entry_price"]
            trig = set(state["triggered"])

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None: continue
        
        mid = (tick.ask + tick.bid) / 2
        pnl = get_positions_pnl()  # MT5 call OUTSIDE lock to avoid deadlock
        with lock:
            state["live_price"] = mid
            state["live_spread"] = int((tick.ask - tick.bid) * 100) # converting to rough points
            state["live_pnl"] = pnl
        
        # [SCAN BIAS EVERY 500MS]
        m30_b = get_candle_bias(mt5.TIMEFRAME_M30)
        h1_b  = get_candle_bias(mt5.TIMEFRAME_H1)
        h4_b  = get_candle_bias(mt5.TIMEFRAME_H4)
        d1_b  = get_candle_bias(mt5.TIMEFRAME_D1)
        with lock:
            state["m30_bias"], state["h1_bias"], state["h4_bias"], state["d1_bias"] = m30_b, h1_b, h4_b, d1_b

        # -- BASKET MANAGEMENT --
        if b_active and entry_p is not None:
            # Heartbeat: log every 30s so user knows bot is alive
            now_ts = time.time()
            if now_ts - last_heartbeat[0] >= 30:
                last_heartbeat[0] = now_ts
                add_log(f"BASKET ALIVE | Dir: {direction} | PnL: ${pnl:.2f} | Price: {mid:.2f}", "info")
            # 1. Grid Check
            for i, lot in enumerate(grid_lots):
                if i in trig: continue
                step = (i + 1) * GRID_STEP_PRICE
                grid_price = (entry_p - step) if direction == "BUY" else (entry_p + step)
                hit = (direction == "BUY" and mid <= grid_price) or (direction == "SELL" and mid >= grid_price)
                if hit:
                    res = send_order(direction, lot, f"GRID{i+1}")
                    if res:
                        tg_say(f"🔔 SAAK GRID ENTRY\nDirection: {direction}\nPrice: {res.price}\nLot: {lot}L\nLayer: {i+1}")
                        trig.add(i)
                        with lock: state["triggered"] = list(trig)

            # 2. Performance Tracking
            all_pos = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
            n = len(all_pos)
            total_lots = sum(p.volume for p in all_pos)
            
            # Detected New Candle
            current_m15_bar = (int(tick.time) // 900) * 900
            new_candle_started = False
            with lock:
                if state.get("last_m15_bar") is None:
                    state["last_m15_bar"] = current_m15_bar
                elif current_m15_bar > state["last_m15_bar"]:
                    new_candle_started = True
                    state["last_m15_bar"] = current_m15_bar

            def liquidate(reason, is_profit):
                nonlocal pnl, n
                close_all()
                with lock:
                    state["basket_active"] = False
                    state["session_pnl"] += pnl
                    state["closed_trades"] += n
                    state["tp_triggered"] = False
                    state["g2_be_touched"] = False
                    if not is_profit: state["daily_loss"] += abs(pnl)
                    state["closed_on_bar"] = state["last_bar_time"]
                    state["last_bar_time"] = None  # Reset so entry logic doesn't skip next candle
                tg_say(f"{'✅' if is_profit else '🛑'} SAAK BASKET CLOSED\nReason: {reason}\nPnL: ${pnl:.2f}\nTotal Trades: {n}")
                add_log(f"BASKET CLOSED: {reason} -> ${pnl:.2f}", "tp" if is_profit else "sl")

            # 3. Stop Loss Guard (Daily)
            if pnl <= -(DAILY_SL_USD - state["daily_loss"]):
                liquidate("DAILY SL OVERSHOOT GUARD", False)
                continue

            # 4. TP Latching (v9pro)
            tp_price = get_basket_tp(entry_p, direction, n, all_pos)
            
            tp_just_touched = False
            with lock:
                if not state["tp_triggered"]:
                    is_tp_touched = (direction == "BUY" and mid >= tp_price) or (direction == "SELL" and mid <= tp_price)
                    if is_tp_touched:
                        state["tp_triggered"] = True
                        tp_just_touched = True
                tp_latched = state["tp_triggered"]
            if tp_just_touched:
                add_log(f"TP Level Touched ({tp_price:.2f}). Latching exit for candle close.", "info")

            # 5. Level 4 Breakeven & Trailing (n >= 4)
            with lock:
                be_touched = state["g2_be_touched"]
                
            if n >= 4:
                if not be_touched and pnl >= 5.0:
                    with lock:
                        state["g2_be_touched"] = True
                        be_touched = True
                    add_log("Level 4 breakeven hit (+$5). Trailing -$100 and Candle-End exit active.", "warn")
                
                if be_touched:
                    if pnl <= -100.0:
                        liquidate("Level 4 TRAILING SL (-$100)", False)
                        continue

            # 6. Resolve Candle-End Exits
            if new_candle_started:
                if tp_latched and pnl > 0:
                    liquidate("TP TARGET REACHED (Candle Close)", True)
                    continue
                if n >= 4 and be_touched:
                    liquidate("Level 4 CANDLE END EXIT", pnl >= 0)
                    continue
            


        # -- ENTRY LOGIC (Strict Candle Opening: First 60s of M15 Market Time) --
        if not running or b_active: continue
        
        is_candle_start = (int(tick.time) % 900 < 60)
        if not is_candle_start: continue
        
        m15_rates = get_rates(mt5.TIMEFRAME_M15, SR_LOOKBACK + 30)
        if m15_rates is None or len(m15_rates) < 25: continue
        
        last_closed = m15_rates.iloc[-2]
        bar_t = str(last_closed["time"])
        
        with lock:
            if bar_t == state["closed_on_bar"] or bar_t == state["last_bar_time"]:
                continue # Skip repetitive checks for efficiency 
            state["last_bar_time"] = bar_t

        # Higher biases already computed at top of loop

        b_bull = b_bear = True
        if BIAS_MODE == "M30_H1_H4":
            b_bull = (h4_b == "BULL" and h1_b == "BULL" and m30_b == "BULL")
            b_bear = (h4_b == "BEAR" and h1_b == "BEAR" and m30_b == "BEAR")
        elif BIAS_MODE == "H4_D1":
            b_bull = (h4_b == "BULL" and d1_b == "BULL")
            b_bear = (h4_b == "BEAR" and d1_b == "BEAR")
        elif BIAS_MODE == "H4_H1":
            b_bull = (h4_b == "BULL" and h1_b == "BULL")
            b_bear = (h4_b == "BEAR" and h1_b == "BEAR")
        elif BIAS_MODE == "H4":
            b_bull, b_bear = (h4_b == "BULL"), (h4_b == "BEAR")

        is_bull = last_closed["close"] > last_closed["open"]
        is_bear = last_closed["close"] < last_closed["open"]

        df_ind = add_indicators(m15_rates)
        sig_dir, conf, pat = score_signal(df_ind.iloc[:-1])

        if sig_dir == "BUY" and is_bull and conf >= MIN_CONFIDENCE and b_bull:
            go_dir = "BUY"
        elif sig_dir == "SELL" and is_bear and conf >= MIN_CONFIDENCE and b_bear:
            go_dir = "SELL"
        else:
            continue

        res = send_order(go_dir, FIXED_LOT, f"ENTRY-{pat}")
        if res:
            with lock:
                state["basket_active"] = True
                state["direction"] = go_dir
                state["entry_price"] = res.price
                state["triggered"] = []
                state["tp_triggered"] = False
                state["g2_be_touched"] = False
            
            tg_say(f"🚀 SAAK BASE ENTRY\nDir: {go_dir}\nPrice: {res.price}\nConf: {conf}%\nPat: {pat}")
            add_log(f"NEW BASKET {go_dir} (Conf: {conf}%, Pat: {pat})", "tp")

      except Exception as e:
        add_log(f"BOT LOOP ERROR: {e}", "error")
        time.sleep(2)  # Prevent rapid error-loop

# =========================
# FLASK API
# =========================
app = Flask(__name__, template_folder="templates")
CORS(app)
@app.route("/")
def index(): return render_template("dashboard.html")

@app.route("/state")
def api_state():
    with lock:
        s = dict(state)
        s["bias_mode"] = BIAS_MODE
        s["positions"] = []
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions:
            tick = mt5.symbol_info_tick(SYMBOL)
            for p in positions:
                if p.magic != MAGIC: continue
                cur = (tick.bid if p.type == 0 else tick.ask) if tick else 0
                s["positions"].append({
                    "ticket": p.ticket,
                    "type": "BUY" if p.type == 0 else "SELL",
                    "price_open": p.price_open,
                    "price_cur": cur,
                    "lot": p.volume,
                    "pnl": p.profit
                })
    return jsonify(s)

@app.route("/start", methods=["POST"])
def _start():
    with lock: state["running"] = True
    add_log("Bot STARTED via dashboard", "info")
    return jsonify({"ok": True})

@app.route("/stop", methods=["POST"])
def _stop():
    with lock: state["running"] = False
    add_log("Bot STOPPED via dashboard", "info")
    return jsonify({"ok": True})

@app.route("/close_all", methods=["POST"])
def _cl_all():
    with lock:
        state["basket_active"] = False
        state["g2_be_touched"] = False
        state["g2_be_bar"] = 0
    close_all()
    add_log("Emergency CLOSE ALL via dashboard", "warn")
    return jsonify({"ok": True})

@app.route("/reset_session", methods=["POST"])
def _reset():
    with lock:
        state["session_pnl"] = 0.0
        state["daily_loss"] = 0.0
        state["closed_trades"] = 0
    return jsonify({"ok": True})

if __name__ == "__main__":
    threading.Thread(target=bot_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
