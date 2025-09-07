# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة --- #
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import re
import time
import sqlite3
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest, RetryAfter, TimedOut

# Gracefully handle optional scipy import
try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logging.warning("Library 'scipy' not found. RSI Divergence strategy will be disabled.")


# --- الإعدادات الأساسية --- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)


if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120

# Use current directory for easier local testing
APP_ROOT = '.'
DB_FILE = os.path.join(APP_ROOT, 'trading_bot_v15.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'settings.json')


EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) --- #
LOG_FILE = os.path.join(APP_ROOT, 'bot_v15.log')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)


# --- متغيرات الحالة العامة للبوت --- #
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}, "status_snapshot": {"last_scan_start_time": "N/A", "last_scan_end_time": "N/A", "markets_found": 0, "signals_found": 0, "active_trades_count": 0, "scan_in_progress": False}}

# --- إدارة الإعدادات --- #
DEFAULT_SETTINGS = {
    "virtual_portfolio_balance_usdt": 1000.0, "virtual_trade_size_percentage": 5.0, "max_concurrent_trades": 5, "top_n_symbols_by_volume": 250, "concurrent_workers": 10, "market_regime_filter_enabled": True,
    "active_scanners": ["momentum_breakout", "breakout_squeeze", "rsi_divergence"],
    "use_dynamic_risk_management": True, "atr_period": 14, "atr_sl_multiplier": 2.0, "risk_reward_ratio": 1.5,
    "take_profit_percentage": 4.0, "stop_loss_percentage": 2.0, "trailing_sl_enabled": True, "trailing_sl_activate_percent": 2.0, "trailing_sl_percent": 1.5,
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68},
    "mean_reversion": {"bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_oversold_level": 30},
    "breakout_squeeze": {"bbands_period": 20, "bbands_stddev": 2.0, "squeeze_threshold_percent": 3.5},
    "rsi_divergence": {"rsi_period": 14, "lookback_period": 35, "peak_trough_lookback": 5}
}

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
            updated = False
            for key, value in DEFAULT_SETTINGS.items():
                if key not in bot_data["settings"]:
                    bot_data["settings"][key] = value; updated = True
                elif isinstance(value, dict):
                     for sub_key, sub_value in value.items():
                         if sub_key not in bot_data["settings"].get(key, {}):
                             bot_data["settings"][key][sub_key] = sub_value; updated = True
            if updated: save_settings()
        else:
            bot_data["settings"] = DEFAULT_SETTINGS
            save_settings()
        logging.info(f"Settings loaded successfully from {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to load settings: {e}")
        bot_data["settings"] = DEFAULT_SETTINGS


def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
        logging.info(f"Settings saved successfully to {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

# --- إدارة قاعدة البيانات --- #
def init_database():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, exchange TEXT, symbol TEXT, entry_price REAL, take_profit REAL, stop_loss REAL, quantity REAL, entry_value_usdt REAL, status TEXT, exit_price REAL, closed_at TEXT, exit_value_usdt REAL, pnl_usdt REAL, trailing_sl_active BOOLEAN, highest_price REAL, reason TEXT)
        ''')
        conn.commit()
        conn.close()
        logging.info(f"Database initialized successfully at: {DB_FILE}")
    except Exception as e:
        logging.error(f"Failed to initialize database at {DB_FILE}: {e}")

def log_recommendation_to_db(signal):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO trades (timestamp, exchange, symbol, entry_price, take_profit, stop_loss, quantity, entry_value_usdt, status, trailing_sl_active, highest_price, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'], signal['entry_price'], signal['take_profit'], signal['stop_loss'], signal['quantity'], signal['entry_value_usdt'], 'نشطة', False, signal['entry_price'], signal['reason']))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    except Exception as e:
        logging.error(f"Failed to log recommendation to DB: {e}")
        return None

# --- وحدات المسح المتقدمة (Scanners) --- #
def analyze_momentum_breakout(df, params):
    try:
        last, prev = df.iloc[-2], df.iloc[-3]
        bbu_col = f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"
        macd_col = f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"
        macds_col = f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"
        rsi_col = f"RSI_{params['rsi_period']}"
        
        if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and last[rsi_col] < params['rsi_max_level']):
             return {"reason": "Momentum Breakout", "type": "long"}
    except (KeyError, IndexError): return None # Handle cases where columns or rows don't exist
    return None

def analyze_mean_reversion(df, params):
    try:
        last = df.iloc[-2]
        bbl_col = f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"
        rsi_col = f"RSI_{params['rsi_period']}"
        
        if (last['close'] < last[bbl_col] and last[rsi_col] < params['rsi_oversold_level']):
            return {"reason": "Mean Reversion (Oversold Bounce)", "type": "long"}
    except (KeyError, IndexError): return None
    return None

def analyze_breakout_squeeze(df, params):
    try:
        last, prev = df.iloc[-2], df.iloc[-3]
        bbu_col = f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"
        
        if prev['bb_width'] < params['squeeze_threshold_percent'] and last['close'] > last[bbu_col]:
            return {"reason": f"Breakout Squeeze (BBW < {params['squeeze_threshold_percent']}%)", "type": "long"}
    except (KeyError, IndexError): return None
    return None

def find_divergence_points(series, lookback):
    if not SCIPY_AVAILABLE: return [], []
    peaks, _ = find_peaks(series, distance=lookback)
    troughs, _ = find_peaks(-series, distance=lookback)
    return peaks, troughs

def analyze_rsi_divergence(df, params):
    if not SCIPY_AVAILABLE: return None
    try:
        rsi_col = f"RSI_{params['rsi_period']}"
        if df[rsi_col].isnull().all(): return None

        subset = df.iloc[-params['lookback_period']:].copy()

        price_peaks_idx, _ = find_divergence_points(subset['high'], params['peak_trough_lookback'])
        price_troughs_idx, _ = find_divergence_points(-subset['low'], params['peak_trough_lookback'])
        rsi_peaks_idx, _ = find_divergence_points(subset[rsi_col], params['peak_trough_lookback'])
        rsi_troughs_idx, _ = find_divergence_points(-subset[rsi_col], params['peak_trough_lookback'])

        if len(price_troughs_idx) >= 2 and len(rsi_troughs_idx) >= 2:
            p_low1_idx, p_low2_idx = price_troughs_idx[-2], price_troughs_idx[-1]
            r_low1_idx, r_low2_idx = rsi_troughs_idx[-2], rsi_troughs_idx[-1]
            if subset.iloc[p_low2_idx]['low'] < subset.iloc[p_low1_idx]['low'] and subset.iloc[r_low2_idx][rsi_col] > subset.iloc[r_low1_idx][rsi_col]:
                return {"reason": "Bullish RSI Divergence", "type": "long"}

        if len(price_peaks_idx) >= 2 and len(rsi_peaks_idx) >= 2:
            p_high1_idx, p_high2_idx = price_peaks_idx[-2], price_peaks_idx[-1]
            r_high1_idx, r_high2_idx = rsi_peaks_idx[-2], rsi_peaks_idx[-1]
            if subset.iloc[p_high2_idx]['high'] > subset.iloc[p_high1_idx]['high'] and subset.iloc[r_high2_idx][rsi_col] < subset.iloc[r_high1_idx][rsi_col]:
                return {"reason": "Bearish RSI Divergence", "type": "bearish_signal"}
    except Exception as e:
        logging.warning(f"RSI Divergence analysis failed: {e}")
    return None

SCANNERS = {
    "momentum_breakout": analyze_momentum_breakout,
    "mean_reversion": analyze_mean_reversion,
    "breakout_squeeze": analyze_breakout_squeeze,
    "rsi_divergence": analyze_rsi_divergence,
}

# --- الدوال الأساسية للبوت --- #
async def initialize_exchanges():
    async def connect(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
        try:
            await exchange.load_markets(); bot_data["exchanges"][ex_id] = exchange
            logging.info(f"Connected to {ex_id} (spot markets only).")
        except Exception as e:
            logging.error(f"Failed for {ex_id}: {e}"); await exchange.close()
    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    all_tickers = []
    async def fetch(ex_id, ex):
        try: return [dict(t, exchange=ex_id) for t in (await ex.fetch_tickers()).values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()])
    for res in results: all_tickers.extend(res)
    
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].upper().endswith('/USDT') and not any(k in t['symbol'].upper() for k in ['UP','DOWN','3L','3S','BEAR','BULL'])]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    
    unique_symbols = {}
    for ticker in sorted_tickers:
        if ticker['symbol'] not in unique_symbols:
            unique_symbols[ticker['symbol']] = {'exchange': ticker['exchange'], 'symbol': ticker['symbol']}
            
    final_list = list(unique_symbols.values())[:bot_data["settings"]['top_n_symbols_by_volume']]
    logging.info(f"Aggregated top {len(final_list)} unique USDT pairs by volume.")
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    return final_list

async def worker(queue, results_list, settings):
    while not queue.empty():
        try:
            market_info = await queue.get()
            exchange = bot_data["exchanges"].get(market_info['exchange'])
            if not exchange: queue.task_done(); continue

            ohlcv = await exchange.fetch_ohlcv(market_info['symbol'], TIMEFRAME, limit=150)
            if len(ohlcv) < 50: queue.task_done(); continue

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            df.ta.atr(length=settings['atr_period'], append=True)
            for scanner_name in settings['active_scanners']:
                params = settings.get(scanner_name, {})
                if scanner_name == "momentum_breakout":
                    df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
                elif scanner_name in ["mean_reversion", "breakout_squeeze"]:
                    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
                    if f"BBU_{params['bbands_period']}_{params['bbands_stddev']}" in df.columns:
                        df['bb_width'] = (df[f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"] - df[f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"]) / df['close'] * 100
                elif scanner_name == "rsi_divergence":
                    df.ta.rsi(length=params['rsi_period'], append=True)

            for scanner_name in settings['active_scanners']:
                analysis_result = SCANNERS.get(scanner_name)(df, settings.get(scanner_name, {}))
                if analysis_result and analysis_result.get("type") == "long":
                    entry_price = df.iloc[-2]['close']
                    current_atr = df.iloc[-2].get(f"ATRr_{settings['atr_period']}", 0)

                    if settings.get("use_dynamic_risk_management", False) and pd.notna(current_atr) and current_atr > 0:
                        risk_per_unit = current_atr * settings['atr_sl_multiplier']
                        stop_loss = entry_price - risk_per_unit
                        take_profit = entry_price + (risk_per_unit * settings['risk_reward_ratio'])
                    else:
                        stop_loss = entry_price * (1 - settings['stop_loss_percentage'] / 100)
                        take_profit = entry_price * (1 + settings['take_profit_percentage'] / 100)

                    signal = {"symbol": market_info['symbol'], "exchange": market_info['exchange'].capitalize(), "entry_price": entry_price, "take_profit": take_profit, "stop_loss": stop_loss, "timestamp": df.index[-2], "reason": analysis_result['reason']}
                    results_list.append(signal)
                    break 
            queue.task_done()
        except Exception: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']
    status.update({"scan_in_progress": True, "last_scan_start_time": datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), "signals_found": 0})
    settings = bot_data["settings"]

    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'")
        active_trades_count = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        logging.error(f"DB Error in perform_scan: {e}"); active_trades_count = settings.get("max_concurrent_trades", 5)

    if settings.get('market_regime_filter_enabled', True) and not await check_market_regime():
        logging.info("Skipping scan: Bearish market regime."); status['scan_in_progress'] = False; return

    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("Scan complete: No markets."); status['scan_in_progress'] = False; return

    queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
    signals = []; worker_tasks = [asyncio.create_task(worker(queue, signals, settings)) for _ in range(settings['concurrent_workers'])]
    await queue.join(); [task.cancel() for task in worker_tasks]
    
    new_trades_entered, opportunities_identified = 0, 0
    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        if (time.time() - last_signal_time.get(signal['symbol'], 0)) <= (SCAN_INTERVAL_SECONDS * 4): continue
        
        trade_amount_usdt = settings["virtual_portfolio_balance_usdt"] * (settings["virtual_trade_size_percentage"] / 100)
        signal.update({'quantity': trade_amount_usdt / signal['entry_price'], 'entry_value_usdt': trade_amount_usdt})

        if active_trades_count < settings.get("max_concurrent_trades", 5):
            if trade_id := log_recommendation_to_db(signal):
                signal['trade_id'] = trade_id
                await send_telegram_message(context.bot, signal, is_new=True)
                active_trades_count += 1; new_trades_entered += 1
        else:
            await send_telegram_message(context.bot, signal, is_opportunity=True)
            opportunities_identified += 1
        
        await asyncio.sleep(0.5); last_signal_time[signal['symbol']] = time.time()

    if len(signals) > 0:
        summary = f"🔹 *ملخص الفحص* 🔹\n\n- إشارات: *{len(signals)}*\n- صفقات جديدة: *{new_trades_entered}*\n- فرص: *{opportunities_identified}*"
        await send_telegram_message(context.bot, {'custom_message': summary, 'target_chat': TELEGRAM_CHAT_ID})

    status['signals_found'] = new_trades_entered + opportunities_identified
    status['last_scan_end_time'] = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False

async def send_telegram_message(bot, signal_data, is_new=False, is_opportunity=False, status_update=None, update_type=None):
    message, target_chat = "", TELEGRAM_CHAT_ID
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"

    if 'custom_message' in signal_data:
        message, target_chat = signal_data['custom_message'], signal_data['target_chat']
    elif is_new or is_opportunity:
        entry_price = signal_data['entry_price']
        tp_percent = ((signal_data['take_profit'] - entry_price) / entry_price * 100)
        sl_percent = ((signal_data['stop_loss'] - entry_price) / entry_price * 100)
        title = "✅ توصية صفقة جديدة ✅" if is_new else "💡 فرصة تداول محتملة 💡"
        trade_value_line = f"▫️ قيمة الصفقة: *${signal_data['entry_value_usdt']:,.2f}*\n" if is_new else ""
        trade_id_line = f"\n*للمتابعة: `/check {signal_data['trade_id']}`*" if is_new else ""
        message = (
            f"{title}\n\n"
            f"▫️ العملة: `{signal_data['symbol']}`\n"
            f"▫️ المنصة: *{signal_data['exchange']}*\n"
            f"▫️ الاستراتيجية: `{signal_data['reason']}`\n"
            f"{trade_value_line}"
            f"━━━━━━━━━━━━━━\n"
            f"📈 الدخول: *{format_price(entry_price)} $*\n"
            f"🎯 الهدف: *{format_price(signal_data['take_profit'])} $* `({tp_percent:+.2f}%)`\n"
            f"🛑 الوقف: *{format_price(signal_data['stop_loss'])} $* `({sl_percent:+.2f}%)`"
            f"{trade_id_line}")
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
    elif status_update in ['ناجحة', 'فاشلة']:
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100) if signal_data.get('entry_value_usdt', 0) > 0 else 0
        icon, title, pnl_label = ("🎯", "هدف محقق!", "الربح") if status_update == 'ناجحة' else ("🛑", "وقف الخسارة", "الخسارة")
        message = f"{icon} *{title}* {icon}\n\n*العملة:* `{signal_data['symbol']}`\n*{pnl_label}:* `~${abs(signal_data.get('pnl_usdt', 0)):.2f} ({pnl_percent:+.2f}%)`"
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين أرباح* | `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى `${format_price(signal_data['stop_loss'])}`."

    if not message: return
    try: await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Failed to send message to {target_chat}: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'")
        active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e: logging.error(f"DB error in track_open_trades: {e}"); return
    
    bot_data['status_snapshot']['active_trades_count'] = len(active_trades)
    if not active_trades: return

    async def check_trade(trade):
        if not (exchange := bot_data["exchanges"].get(trade['exchange'].lower())): return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol'])
            if not (current_price := ticker.get('last') or ticker.get('close')): return None

            if current_price >= trade['take_profit']: return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']: return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price}

            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade.get('highest_price', current_price), current_price)
                if not trade.get('trailing_sl_active') and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price'] * (1 + (settings['trailing_sl_activate_percent'] - settings['trailing_sl_percent']) / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade.get('trailing_sl_active'):
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade.get('highest_price', 0): return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db, portfolio_pnl = [], 0.0
    for result in filter(None, results):
        original_trade = next((t for t in active_trades if t['id'] == result['id']), None)
        if not original_trade: continue
        
        status = result['status']
        if status in ['ناجحة', 'فاشلة']:
            pnl = (result['exit_price'] - original_trade['entry_price']) * original_trade['quantity']; portfolio_pnl += pnl
            closed_at = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')
            exit_value = result['exit_price'] * original_trade['quantity']
            updates_to_db.append(("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?", (status, result['exit_price'], closed_at, exit_value, pnl, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result, 'pnl_usdt': pnl}, status_update=status)
        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, 'stop_loss': result['new_sl']}, update_type='tsl_activation')
        elif status == 'update_sl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], result['id'])))
        elif status == 'update_peak':
            updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], result['id'])))

    if updates_to_db:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            for q, p in updates_to_db: cursor.execute(q, p)
            conn.commit(); conn.close()
        except Exception as e: logging.error(f"DB update failed in track_open_trades: {e}")
    
    if portfolio_pnl != 0.0:
        bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl; save_settings()

async def check_market_regime():
    try:
        if not (binance := bot_data["exchanges"].get('binance')): return True
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma50'] = ta.sma(df['close'], length=50)
        return df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception: return True

# --- FIX 1: The main fix for the `duplicate labels` error is here ---
async def fetch_historical_data_paginated(symbol, timeframe, limit):
    logging.info(f"Fetching {limit} candles for {symbol}...")
    exchange = ccxt.binance({'options': {'defaultType': 'spot'}})
    all_ohlcv = []
    try:
        since = None
        while len(all_ohlcv) < limit:
            fetch_limit = min(limit - len(all_ohlcv), 1000)
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=fetch_limit)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv)
            if since == ohlcv[-1][0]: break
            since = ohlcv[0][0] - 1
            
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # This line removes any duplicate timestamps (rows) from the data
        df.drop_duplicates(subset='timestamp', keep='first', inplace=True)
        
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df.iloc[-limit:]
        logging.info(f"Successfully fetched and cleaned {len(df)} candles for {symbol}.")
        return df
    except Exception as e:
        logging.error(f"Error fetching paginated data for {symbol}: {e}")
        return None
    finally:
        await exchange.close()

def analyze_backtest_results(trades, symbol, timeframe, limit):
    if not trades: return ("\n*لم يتم تنفيذ أي صفقات.*\n\nقد يكون هذا بسبب أن شروط الاستراتيجيات لم تتحقق خلال الفترة المختارة.")
    df_trades = pd.DataFrame(trades)
    total_trades = len(df_trades)
    wins = df_trades[df_trades['status'] == 'Take Profit']
    losses = df_trades[df_trades['status'] == 'Stop Loss']
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
    total_pnl = df_trades['pnl'].sum()
    avg_win = wins['pnl'].mean() if not wins.empty else 0
    avg_loss = losses['pnl'].mean() if not losses.empty else 0
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    df_trades['cumulative_pnl'] = df_trades['pnl'].cumsum()
    df_trades['peak'] = df_trades['cumulative_pnl'].cummax()
    df_trades['drawdown'] = df_trades['peak'] - df_trades['cumulative_pnl']
    max_drawdown = df_trades['drawdown'].max()
    return (
        f"--- 📜 *تقرير الاختبار التاريخي* ---\n\n"
        f"`{symbol}` | `{timeframe}` | `{limit}` شمعة\n\n"
        f"▫️ الصفقات: `{total_trades}` (✅{len(wins)} | ❌{len(losses)})\n"
        f"📈 معدل النجاح: `{win_rate:.2f}%`\n"
        f"💰 إجمالي الربح/الخسارة: `{total_pnl:+.4f}`\n"
        f"⚖️ المخاطرة/العائد: `1:{risk_reward:.2f}`\n"
        f"📉 أقصى تراجع: `-{max_drawdown:.4f}`")

async def run_backtest_logic(update: Update, symbol: str, timeframe: str, limit: int):
    try:
        df = await fetch_historical_data_paginated(symbol, timeframe, limit)
        if df is None or len(df) < 50:
            await update.message.reply_text(f"لم أتمكن من جلب بيانات كافية لـ `{symbol}`."); return

        trades, active_trade, settings = [], None, bot_data["settings"]

        logging.info(f"Backtest: Calculating all indicators for {symbol}...")
        df.ta.atr(length=settings['atr_period'], append=True)
        for scanner_name in settings['active_scanners']:
            params = settings.get(scanner_name, {})
            if scanner_name == "momentum_breakout":
                df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
            elif scanner_name in ["mean_reversion", "breakout_squeeze"]:
                df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
                bbu, bbl = f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"
                if bbu in df.columns: df['bb_width'] = (df[bbu] - df[bbl]) / df['close'] * 100
            elif scanner_name == "rsi_divergence": df.ta.rsi(length=params['rsi_period'], append=True)
        logging.info("Backtest: Indicator calculation complete.")

        for i in range(50, len(df)):
            current_candle = df.iloc[i]
            if active_trade:
                if current_candle['low'] <= active_trade['stop_loss']: active_trade.update({'status': 'Stop Loss', 'exit_price': active_trade['stop_loss']})
                elif current_candle['high'] >= active_trade['take_profit']: active_trade.update({'status': 'Take Profit', 'exit_price': active_trade['take_profit']})
                if 'status' in active_trade:
                    active_trade['pnl'] = (active_trade['exit_price'] - active_trade['entry_price'])
                    trades.append(active_trade); active_trade = None
                    continue
            
            if not active_trade:
                historical_slice = df.iloc[:i]
                for scanner_name in settings['active_scanners']:
                    result = SCANNERS.get(scanner_name)(historical_slice, settings.get(scanner_name, {}))
                    if result and result.get('type') == 'long':
                        entry_price = df.iloc[i-1]['close']
                        atr = df.iloc[i-1].get(f"ATRr_{settings['atr_period']}", 0)
                        if pd.isna(atr) or atr == 0: continue
                        risk = atr * settings['atr_sl_multiplier']
                        active_trade = {'entry_price': entry_price, 'stop_loss': entry_price - risk, 'take_profit': entry_price + (risk * settings['risk_reward_ratio'])}
                        break

        report = analyze_backtest_results(trades, symbol, timeframe, limit)
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Error during backtest execution: {e}", exc_info=True)
        await update.message.reply_text(f"حدث خطأ أثناء تشغيل الاختبار: {e}")

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [["📊 الإحصائيات", "📈 الصفقات النشطة"], ["🧪 اختبار تاريخي", "⚙️ الإعدادات"], ["👀 الحالة", "ℹ️ مساعدة"], ["🔬 فحص يدوي الآن"]]
settings_menu_keyboard = [["🎭 تفعيل/تعطيل الماسحات"], ["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في محاكي التداول (v15)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))
async def scan_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot'].get('scan_in_progress', False):
        await update.message.reply_text("⚠️ فحص آخر قيد التنفيذ."); return
    await update.message.reply_text("⏳ بدء الفحص اليدوي...")
    context.job_queue.run_once(perform_scan, 0, name='manual_scan')
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.callback_query.message).reply_text("اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))

def get_scanners_keyboard():
    active = bot_data["settings"].get("active_scanners", [])
    keyboard = [[InlineKeyboardButton(f"{'✅' if name in active else '❌'} {name}", callback_data=f"toggle_{name}")] for name in SCANNERS.keys()]
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keyboard)

async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.callback_query.message).reply_text("اختر الماسحات للتفعيل/التعطيل:", reply_markup=get_scanners_keyboard())
async def toggle_scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    scanner_name = query.data.split("_", 1)[1]
    active = bot_data["settings"].get("active_scanners", []).copy()
    if scanner_name in active: active.remove(scanner_name)
    else: active.append(scanner_name)
    bot_data["settings"]["active_scanners"] = active; save_settings()
    try: await query.edit_message_text("اختر الماسحات للتفعيل/التعطيل:", reply_markup=get_scanners_keyboard())
    except BadRequest as e:
        if "Message is not modified" not in str(e): raise

async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, (dict, list))])
    await update.message.reply_text(f"لتعديل معيار:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير المتاحة:*\n{params}", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True))
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("*/start* - بدء\n*/scan* - فحص يدوي\n*/report* - تقرير يومي\n*/check <ID>* - متابعة صفقة\n*/backtest <S> <T> <C>* - اختبار تاريخي\n*/debug* - فحص الحالة", parse_mode=ParseMode.MARKDOWN)
async def backtest_instructions_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("لإجراء اختبار تاريخي:\n`/backtest SYMBOL TIMEFRAME CANDLES`\n*مثال:* `/backtest BTC/USDT 1h 4000`", parse_mode=ParseMode.MARKDOWN)
async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3: return await backtest_instructions_command(update, context)
    try:
        symbol, timeframe, limit = context.args[0].upper(), context.args[1], int(context.args[2])
        await update.message.reply_text(f"⏳ بدء الاختبار التاريخي لـ `{symbol}`...", parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(run_backtest_logic(update, symbol, timeframe, limit))
    except (ValueError, IndexError): await update.message.reply_text("❌ خطأ في المدخلات.")
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status")
        stats = cursor.fetchall(); conn.close()
        counts = {s: c for s, c, p in stats}; pnl = {s: (p or 0) for s, c, p in stats}
        active, successful, failed = counts.get('نشطة', 0), counts.get('ناجحة', 0), counts.get('فاشلة', 0)
        closed = successful + failed
        win_rate = (successful / closed * 100) if closed > 0 else 0
        message = (f"*📊 إحصائيات المحفظة*\n\n"
                   f"📈 الرصيد: `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n"
                   f"💰 إجمالي الربح/الخسارة: `${sum(pnl.values()):+.2f}`\n\n"
                   f"▫️ الصفقات: `{sum(counts.values())}` (`{active}` نشطة)\n"
                   f"✅ الناجحة: `{successful}` | الربح: `${pnl.get('ناجحة', 0):.2f}`\n"
                   f"❌ الفاشلة: `{failed}` | الخسارة: `${abs(pnl.get('فاشلة', 0)):.2f}`\n"
                   f"📈 معدل النجاح: `{win_rate:.2f}%`")
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Error in stats: {e}"); await update.message.reply_text("حدث خطأ.")

# --- FIX 2: The `SyntaxError` is fixed here ---
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d')
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
        cursor.execute("SELECT status, pnl_usdt FROM trades WHERE DATE(closed_at) = ?", (today_str,))
        closed_today = cursor.fetchall(); conn.close()
        if not closed_today:
            message = f"🗓️ *التقرير اليومي {today_str}*\n\nلم يتم إغلاق أي صفقات اليوم."
        else:
            wins, losses, total_pnl = 0, 0, 0.0
            for status, pnl in closed_today:
                if status == 'ناجحة':
                    wins += 1
                else:
                    losses += 1
                if pnl is not None: total_pnl += pnl
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            message = (f"🗓️ *التقرير اليومي {today_str}*\n\n"
                       f"▫️ الصفقات المغلقة: `{total}` (✅{wins} | ❌{losses})\n"
                       f"📈 معدل النجاح: `{win_rate:.2f}%`\n"
                       f"💰 الربح/الخسارة: `${total_pnl:+.2f}`")
        await send_telegram_message(context.bot, {'custom_message': message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})
    except Exception as e: logging.error(f"Failed to generate daily report: {e}")

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ إعداد التقرير اليومي...")
    await send_daily_report(context)
async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']
    next_scan = "N/A"
    if not status['scan_in_progress'] and context.job_queue:
        if job := context.job_queue.get_jobs_by_name('perform_scan'):
            next_scan = job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S')
    message = (f"🤖 *حالة البوت*\n\n"
               f"{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ في وضع الاستعداد'}\n"
               f"- آخر فحص: `{status['last_scan_start_time']}`\n"
               f"- الصفقات النشطة: `{status['active_trades_count']}`\n"
               f"- الفحص التالي: `{next_scan}`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report = ["*🔍 تقرير التشخيص*"]
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); c = conn.cursor(); c.execute("SELECT COUNT(*) FROM trades"); count = c.fetchone()[0]; conn.close()
        report.append(f"✅ *قاعدة البيانات:* متصلة ({count} trades).")
    except Exception as e: report.append(f"❌ *قاعدة البيانات:* فشل! ({e})")
    report.append("\n*📡 حالة المنصات:*")
    report.extend([f"  - `{ex}`: {'✅' if ex in bot_data.get('exchanges', {}) else '❌'}" for ex in EXCHANGES_TO_SCAN])
    report.append("\n*⚙️ المهام الخلفية:*")
    if context.job_queue:
        for name in ['perform_scan', 'track_open_trades', 'daily_report']:
            report.append(f"  - `{name}`: {'✅' if context.job_queue.get_jobs_by_name(name) else '❌'}")
    await update.message.reply_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)
async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    target = update.callback_query.message if trade_id_from_callback else update.message
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"
    try:
        trade_id = trade_id_from_callback or int(context.args[0])
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; c = conn.cursor()
        c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)); trade = c.fetchone() and dict(c.fetchone()); conn.close()
        if not trade: return await target.reply_text(f"لم أجد صفقة بالرقم `{trade_id}`.")
        
        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            message = f"📋 *ملخص الصفقة #{trade_id}*\n\n- العملة: `{trade['symbol']}`\n- الحالة: `{trade['status']}`\n- الربح/الخسارة: `${trade.get('pnl_usdt', 0):+.2f} ({pnl_percent:+.2f}%)`"
        else:
            if not (exchange := bot_data["exchanges"].get(trade['exchange'].lower())): return await target.reply_text("المنصة غير متصلة.")
            ticker = await exchange.fetch_ticker(trade['symbol'])
            if not (current_price := ticker.get('last') or ticker.get('close')): return await target.reply_text(f"لم أجد السعر الحالي لـ `{trade['symbol']}`.")
            
            pnl = (current_price - trade['entry_price']) * trade['quantity']
            pnl_percent = (pnl / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            message = (f"📈 *متابعة حية للصفقة #{trade_id}*\n\n"
                       f"▫️ `{trade['symbol']}` | `نشطة`\n"
                       f"▫️ الدخول: `${format_price(trade['entry_price'])}`\n"
                       f"▫️ الحالي: `${format_price(current_price)}`\n\n"
                       f"💰 الربح/الخسارة: `${pnl:+.2f} ({pnl_percent:+.2f}%)`")
        await target.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await target.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e: logging.error(f"Error in check_trade: {e}", exc_info=True); await target.reply_text("حدث خطأ.")
async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; c = conn.cursor()
        c.execute("SELECT id, symbol, entry_value_usdt FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
        trades = c.fetchall(); conn.close()
        if not trades: return await update.message.reply_text("لا توجد صفقات نشطة.")
        keyboard = [[InlineKeyboardButton(f"#{t['id']} | {t['symbol']} | ${t['entry_value_usdt']:.2f}", callback_data=f"check_{t['id']}")] for t in trades]
        await update.message.reply_text("اختر صفقة لمتابعتها:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e: logging.error(f"Error in show_active_trades: {e}"); await update.message.reply_text("حدث خطأ.")
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data.startswith("toggle_"): await toggle_scanner_callback(update, context)
    elif data == "back_to_settings":
        await query.message.delete()
        await context.bot.send_message(query.message.chat_id, "اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif data.startswith("check_"): await check_trade_command(update, context, trade_id_from_callback=int(data.split("_")[1]))
async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handlers = {"📊 الإحصائيات": stats_command, "📈 الصفقات النشطة": show_active_trades_command, "ℹ️ مساعدة": help_command, "🧪 اختبار تاريخي": backtest_instructions_command, "⚙️ الإعدادات": show_settings_menu, "👀 الحالة": background_status_command, "🔬 فحص يدوي الآن": scan_now_command, "🔧 تعديل المعايير": show_set_parameter_instructions, "🔙 القائمة الرئيسية": start_command, "🔙 قائمة الإعدادات": show_settings_menu, "🎭 تفعيل/تعطيل الماسحات": show_scanners_menu}
    text = update.message.text
    if text in handlers: return await handlers[text](update, context)
    
    if match := re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text):
        param, val_str = match.groups()
        settings = bot_data["settings"]
        if param in settings and not isinstance(settings[param], (dict, list)):
            try:
                current_val = settings[param]
                if isinstance(current_val, bool): new_val = val_str.lower() in ['true', '1', 'on']
                elif isinstance(current_val, int): new_val = int(val_str)
                else: new_val = float(val_str)
                settings[param] = new_val; save_settings()
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_val}`.")
            except ValueError: await update.message.reply_text(f"❌ قيمة غير صالحة.")
        else: await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود.")
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE): logging.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
async def post_init(app: Application):
    await initialize_exchanges()
    if not bot_data["exchanges"]: return logging.critical("CRITICAL: Failed to connect to any exchange.")
    
    if job_queue := app.job_queue:
        job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
        job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
        job_queue.run_daily(send_daily_report, time=dt_time(hour=23, minute=55, tzinfo=EGYPT_TZ), name='daily_report')
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *محاكي التداول (v15) جاهز للعمل!*", parse_mode=ParseMode.MARKDOWN)
async def post_shutdown(app: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logging.info("Connections closed.")

def main():
    print("🚀 Starting Pro Trading Simulator Bot (v15)...")
    load_settings(); init_database()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_now_command))
    app.add_handler(CommandHandler("report", daily_report_command))
    app.add_handler(CommandHandler("check", check_trade_command))
    app.add_handler(CommandHandler("backtest", backtest_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    app.add_error_handler(error_handler)
    
    print("✅ Bot is now running...")
    app.run_polling()

if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error: {e}", exc_info=True)
