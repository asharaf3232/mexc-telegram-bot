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

# [NEW] Gracefully handle optional scipy import
try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logging.warning("Library 'scipy' not found. RSI Divergence strategy will be disabled.")


# --- الإعدادات الأساسية --- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
# [FEATURE] Add a separate channel for signals. Fallback to the main chat ID if not set.
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)


if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120

# [FINAL PERSISTENCE FIX] Hardcode the data file paths to the application's root directory.
APP_ROOT = '.' # Use current directory for easier local testing
DB_FILE = os.path.join(APP_ROOT, 'trading_bot_v13.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'settings.json')


EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) --- #
LOG_FILE = os.path.join(APP_ROOT, 'bot_v13.log')
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
        df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        bbu_col, macd_col, macds_col, rsi_col = f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"RSI_{params['rsi_period']}"
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and last[rsi_col] < params['rsi_max_level']):
             return {"reason": "Momentum Breakout", "type": "long"}
    except Exception: return None
    return None

def analyze_mean_reversion(df, params):
    try:
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        bbl_col, rsi_col = f"BBL_{params['bbands_period']}_{params['bbands_stddev']}", f"RSI_{params['rsi_period']}"
        last = df.iloc[-2]
        if (last['close'] < last[bbl_col] and last[rsi_col] < params['rsi_oversold_level']):
            return {"reason": "Mean Reversion (Oversold Bounce)", "type": "long"}
    except Exception: return None
    return None

def analyze_breakout_squeeze(df, params):
    try:
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
        bbu_col, bbl_col = f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"
        df['bb_width'] = (df[bbu_col] - df[bbl_col]) / df['close'] * 100
        last, prev = df.iloc[-2], df.iloc[-3]
        if prev['bb_width'] < params['squeeze_threshold_percent'] and last['close'] > last[bbu_col]:
            return {"reason": f"Breakout Squeeze (BBW < {params['squeeze_threshold_percent']}%)", "type": "long"}
    except Exception: return None
    return None

def find_divergence_points(series, lookback):
    if not SCIPY_AVAILABLE:
        return [], []
    peaks, _ = find_peaks(series, distance=lookback)
    troughs, _ = find_peaks(-series, distance=lookback)
    return peaks, troughs

def analyze_rsi_divergence(df, params):
    if not SCIPY_AVAILABLE:
        return None
    try:
        rsi_col = f"RSI_{params['rsi_period']}"
        df.ta.rsi(length=params['rsi_period'], append=True, col_names=(rsi_col,))
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
        logging.warning(f"RSI Divergence analysis failed for {df.iloc[-1].name}: {e}")
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
        # [SPOT MARKET FIX] Force CCXT to only load spot markets.
        exchange = getattr(ccxt, ex_id)({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            },
        })
        try:
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logging.info(f"Connected to {ex_id} (spot markets only).")
        except Exception as e:
            logging.error(f"Failed for {ex_id}: {e}")
            await exchange.close()
    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    all_tickers = []
    async def fetch(ex_id, ex):
        try: return [dict(t, exchange=ex_id) for t in (await ex.fetch_tickers()).values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()])
    for res in results: all_tickers.extend(res)

    usdt_tickers = [
        t for t in all_tickers
        if t.get('symbol')
        and t['symbol'].upper().endswith('/USDT')
        and not any(k in t['symbol'].upper() for k in ['UP','DOWN','3L','3S','BEAR','BULL'])
    ]

    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)

    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols:
            unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}

    final_list = list(unique_symbols.values())[:bot_data["settings"]['top_n_symbols_by_volume']]

    logging.info(f"Aggregated markets. Found {len(all_tickers)} total tickers, filtered down to {len(usdt_tickers)} USDT pairs, selected top {len(final_list)} unique pairs by volume.")
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    return final_list

async def worker(queue, results_list, settings):
    while not queue.empty():
        try:
            market_info = await queue.get()
            exchange = bot_data["exchanges"].get(market_info['exchange'])
            if not exchange or not settings.get('active_scanners'): continue

            ohlcv = await exchange.fetch_ohlcv(market_info['symbol'], TIMEFRAME, limit=150)
            if len(ohlcv) < 50: queue.task_done(); continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)
            df.ta.atr(length=settings['atr_period'], append=True)

            for scanner_name in settings['active_scanners']:
                analysis_result = SCANNERS.get(scanner_name, lambda d, p: None)(df, settings.get(scanner_name, {}))
                if analysis_result and analysis_result.get("type") == "long":
                    entry_price = df.iloc[-2]['close']
                    current_atr = df.iloc[-2].get(f"ATRr_{settings['atr_period']}", 0)

                    if settings.get("use_dynamic_risk_management", False) and current_atr > 0 and not pd.isna(current_atr):
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
        logging.error(f"DB Error in perform_scan: {e}")
        active_trades_count = settings.get("max_concurrent_trades", 5)

    if settings.get('market_regime_filter_enabled', True) and not await check_market_regime():
        logging.info("Skipping scan: Bearish market regime detected."); status['scan_in_progress'] = False; return

    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("Scan complete: No markets to scan.")
        status['scan_in_progress'] = False; return

    queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
    signals = []; worker_tasks = [asyncio.create_task(worker(queue, signals, settings)) for _ in range(settings['concurrent_workers'])]
    await queue.join(); [task.cancel() for task in worker_tasks]

    total_signals_found_this_run = 0
    new_trades_entered = 0
    opportunities_identified = 0

    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        symbol = signal['symbol']; current_time = time.time()

        if symbol in last_signal_time and (current_time - last_signal_time.get(symbol, 0)) <= (SCAN_INTERVAL_SECONDS * 4):
            continue

        total_signals_found_this_run += 1
        trade_amount_usdt = settings["virtual_portfolio_balance_usdt"] * (settings["virtual_trade_size_percentage"] / 100)
        signal.update({'quantity': trade_amount_usdt / signal['entry_price'], 'entry_value_usdt': trade_amount_usdt})

        if active_trades_count < settings.get("max_concurrent_trades", 5):
            trade_id = log_recommendation_to_db(signal)
            if trade_id:
                signal['trade_id'] = trade_id
                await send_telegram_message(context.bot, signal, is_new=True)
                active_trades_count += 1
                new_trades_entered += 1
        else:
            await send_telegram_message(context.bot, signal, is_opportunity=True)
            opportunities_identified += 1

        await asyncio.sleep(0.5)
        last_signal_time[symbol] = current_time

    summary_log = f"Scan complete. Found: {total_signals_found_this_run}, Entered: {new_trades_entered}, Opportunities: {opportunities_identified}."
    logging.info(summary_log)

    if total_signals_found_this_run > 0:
        summary_message = (f"🔹 *ملخص الفحص* 🔹\n\n"
                           f"▫️ إجمالي الإشارات التي تم العثور عليها: *{total_signals_found_this_run}*\n"
                           f"✅ صفقات جديدة تم الدخول بها: *{new_trades_entered}*\n"
                           f"💡 فرص إضافية تم تحديدها: *{opportunities_identified}*")
        await send_telegram_message(context.bot, {'custom_message': summary_message, 'target_chat': TELEGRAM_CHAT_ID})


    status['signals_found'] = new_trades_entered + opportunities_identified
    status['last_scan_end_time'] = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False

# [REFACTOR] A single, robust function to send all messages with retry logic.
async def send_telegram_message(bot, signal_data, is_new=False, is_opportunity=False, status_update=None, update_type=None):
    message = ""; keyboard = None; target_chat = TELEGRAM_CHAT_ID

    # دالة مساعدة لتنسيق السعر بشكل ديناميكي
    def format_price(price):
        if price < 0.01:
            return f"{price:,.8f}"
        return f"{price:,.4f}"

    if 'custom_message' in signal_data:
        message = signal_data['custom_message']
        target_chat = signal_data['target_chat']
    
    # [MODIFIED] New message formats for new trades and opportunities
    elif is_new or is_opportunity:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        entry_price = signal_data['entry_price']
        take_profit = signal_data['take_profit']
        stop_loss = signal_data['stop_loss']
        
        tp_percent = ((take_profit - entry_price) / entry_price * 100)
        sl_percent = ((stop_loss - entry_price) / entry_price * 100)

        title = "✅ توصية صفقة جديدة ✅" if is_new else "💡 فرصة تداول محتملة 💡"
        trade_value_line = f"▫️ قيمة الصفقة: *${signal_data['entry_value_usdt']:,.2f}*" if is_new else ""
        trade_id_line = f"\n*للمتابعة، استخدم الأمر `/check {signal_data['trade_id']}` في البوت*" if is_new else ""

        message = (
            f"{title}\n\n"
            f"▫️ العملة: `{signal_data['symbol']}`\n"
            f"▫️ المنصة: *{signal_data['exchange']}*\n"
            f"▫️ الاستراتيجية: `{signal_data['reason']}`\n"
            f"{trade_value_line}\n"
            f"━━━━━━━━━━━━━━\n"
            f"📈 سعر الدخول: *{format_price(entry_price)} $*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎯 الهدف: *{format_price(take_profit)} $* `({tp_percent:+.2f}%)`\n"
            f"🛑 الوقف: *{format_price(stop_loss)} $* `({sl_percent:+.2f}%)`"
            f"{trade_id_line}"
        )

    elif status_update in ['ناجحة', 'فاشلة']:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100) if signal_data.get('entry_value_usdt', 0) > 0 else 0
        icon, title, pnl_label = ("🎯", "هدف محقق!", "الربح") if status_update == 'ناجحة' else ("🛑", "تم تفعيل وقف الخسارة", "الخسارة")
        message = f"{icon} *{title}* {icon}\n\n*العملة:* `{signal_data['symbol']}` | *المنصة:* `{signal_data['exchange']}`\n*{pnl_label}:* `~${abs(signal_data.get('pnl_usdt', 0)):.2f} ({pnl_percent:+.2f}%)`"
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى `${format_price(signal_data['stop_loss'])}`."

    if not message: return

    for attempt in range(3):
        try:
            await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            return
        except RetryAfter as e:
            logging.warning(f"Flood control exceeded. Waiting for {e.retry_after} seconds (Attempt {attempt+1}/3).")
            await asyncio.sleep(e.retry_after)
        except TimedOut:
            logging.warning(f"Request timed out. Waiting for 5 seconds before retrying (Attempt {attempt+1}/3).")
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Failed to send message to {target_chat} on attempt {attempt+1}: {e}")
            await asyncio.sleep(2)
    logging.error(f"Failed to send message to {target_chat} after 3 attempts.")


async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'")
        active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e:
        logging.error(f"DB error in track_open_trades: {e}")
        return

    bot_data['status_snapshot']['active_trades_count'] = len(active_trades)
    if not active_trades: return

    async def check_trade(trade):
        exchange = bot_data["exchanges"].get(trade['exchange'].lower())
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None
            if current_price >= trade['take_profit']: return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']: return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price}

            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade.get('highest_price', current_price), current_price)
                if not trade.get('trailing_sl_active') and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price'] * (1 + (settings.get('trailing_sl_activate_percent', 2.0) - settings.get('trailing_sl_percent', 1.5)) / 100)
                    if new_sl > trade['stop_loss']:
                        return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade.get('trailing_sl_active'):
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade.get('highest_price', 0): return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db = []; portfolio_pnl = 0.0
    for result in filter(None, results):
        original_trade = next((t for t in active_trades if t['id'] == result['id']), None)
        if not original_trade: continue

        status = result['status']
        if status in ['ناجحة', 'فاشلة']:
            pnl = (result['exit_price'] - original_trade['entry_price']) * original_trade['quantity']; portfolio_pnl += pnl
            closed_at_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')
            updates_to_db.append(("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?", (status, result['exit_price'], closed_at_str, result['exit_price'] * original_trade['quantity'], pnl, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result, 'pnl_usdt': pnl}, status_update=status)
        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl': updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], result['id'])))
        elif status == 'update_peak': updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], result['id'])))

    if updates_to_db:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            cursor = conn.cursor()
            [cursor.execute(q, p) for q, p in updates_to_db]
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"DB update failed in track_open_trades: {e}")

    if portfolio_pnl != 0.0: bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl; save_settings(); logging.info(f"Portfolio balance updated by ${portfolio_pnl:.2f}.")

async def check_market_regime():
    try:
        binance = bot_data["exchanges"].get('binance')
        if not binance: return True
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma50'] = ta.sma(df['close'], length=50)
        return df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception: return True


# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [
    ["📊 الإحصائيات", "📈 الصفقات النشطة"],
    ["⚙️ الإعدادات", "👀 ماذا يجري في الخلفية؟"],
    ["ℹ️ مساعدة", "🔬 فحص يدوي الآن"]
]
settings_menu_keyboard = [["🎭 تفعيل/تعطيل الماسحات"], ["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في محاكي التداول المتقدم! (v13)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))

async def scan_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot'].get('scan_in_progress', False):
        await update.message.reply_text("⚠️ لا يمكن بدء فحص جديد، هناك فحص آخر قيد التنفيذ حالياً. يرجى الانتظار.")
        return

    await update.message.reply_text("⏳ جاري بدء الفحص اليدوي... سأرسل لك ملخصاً عند الانتهاء.")
    context.job_queue.run_once(perform_scan, 0, name='manual_scan')


async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))

def get_scanners_keyboard():
    keyboard = []
    active_scanners = bot_data["settings"].get("active_scanners", [])
    for name in SCANNERS.keys():
        status_icon = "✅" if name in active_scanners else "❌"
        button = InlineKeyboardButton(f"{status_icon} {name}", callback_data=f"toggle_{name}")
        keyboard.append([button])
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keyboard)

async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر الماسحات التي تريد تفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())

async def toggle_scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    scanner_name = "_".join(query.data.split("_")[1:])
    active_scanners = bot_data["settings"].get("active_scanners", []).copy()

    if scanner_name in active_scanners:
        active_scanners.remove(scanner_name)
    else:
        active_scanners.append(scanner_name)

    bot_data["settings"]["active_scanners"] = active_scanners
    save_settings()

    try:
        await query.edit_message_text(text="اختر الماسحات التي تريد تفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
    except BadRequest as e:
        if "Message is not modified" in str(e): pass
        else: raise

async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params_list = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, (dict, list))])
    message = (f"لتعديل معيار، أرسل:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير القابلة للتعديل:*\n{params_list}")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*مساعدة البوت*\n"
        "`/start` - بدء\n"
        "`/scan` - إجراء فحص يدوي فوري\n"
        "`/report` - إرسال التقرير اليومي يدوياً\n"
        "`/check <ID>` - متابعة صفقة\n"
        "`/debug` - فحص حالة البوت التشخيصية",
        parse_mode=ParseMode.MARKDOWN
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status")
        stats_data = cursor.fetchall()
        conn.close()
        counts = {s: c for s, c, p in stats_data}; pnl = {s: (p if p is not None else 0) for s, c, p in stats_data}
        total = sum(counts.values()); active = counts.get('نشطة', 0); successful = counts.get('ناجحة', 0); failed = counts.get('فاشلة', 0)
        closed_trades = successful + failed; win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0; total_pnl = sum(pnl.values())
        stats_message = (f"*📊 إحصائيات المحفظة*\n\n📈 *الرصيد الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n💰 *إجمالي الربح/الخسارة:* `${total_pnl:+.2f}`\n\n- *إجمالي الصفقات:* `{total}` (`{active}` نشطة)\n- *الناجحة:* `{successful}` | *الربح:* `${pnl.get('ناجحة', 0):.2f}`\n- *الفاشلة:* `{failed}` | *الخسارة:* `${abs(pnl.get('فاشلة', 0)):.2f}`\n- *معدل النجاح:* `{win_rate:.2f}%`")
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Error in stats_command: {e}", exc_info=True)
        await update.message.reply_text("حدث خطأ أثناء جلب الإحصائيات.")

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d')
    logging.info(f"Generating daily report for {today_str}...")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT status, pnl_usdt FROM trades WHERE DATE(closed_at) = ?", (today_str,))
        closed_today = cursor.fetchall(); conn.close()
        if not closed_today:
            report_message = f"🗓️ *التقرير اليومي ليوم {today_str}*\n\nلم يتم إغلاق أي صفقات اليوم."
        else:
            wins, losses, total_pnl = 0, 0, 0.0
            for status, pnl in closed_today:
                if status == 'ناجحة': wins += 1
                else: losses += 1
                if pnl is not None: total_pnl += pnl
            total_trades = wins + losses
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
            report_message = (
                f"🗓️ *التقرير اليومي ليوم {today_str}*\n\n"
                f"▫️ *إجمالي الصفقات المغلقة:* `{total_trades}`\n"
                f"✅ *الرابحة:* `{wins}`\n"
                f"❌ *الخاسرة:* `{losses}`\n\n"
                f"📈 *معدل النجاح اليومي:* `{win_rate:.2f}%`\n"
                f"💰 *إجمالي الربح/الخسارة اليومي:* `${total_pnl:+.2f}`"
            )
        await send_telegram_message(context.bot, {'custom_message': report_message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})
        return True
    except Exception as e:
        logging.error(f"Failed to generate daily report: {e}", exc_info=True)
        return False

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إعداد وإرسال التقرير اليومي إلى القناة...")
    if await send_daily_report(context):
        await update.message.reply_text("✅ تم إرسال التقرير بنجاح إلى القناة.")


async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']; next_scan_time = "N/A"
    if not status['scan_in_progress'] and context.job_queue:
        next_scan_job = context.job_queue.get_jobs_by_name('perform_scan')
        if next_scan_job and next_scan_job[0].next_t: next_scan_time = next_scan_job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S')
    message = (f"🤖 *حالة البوت في الخلفية*\n\n*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n- *آخر فحص بدأ:* `{status['last_scan_start_time']}`\n- *آخر فحص انتهى:* `{status['last_scan_end_time']}`\n- *العملات المفحوصة:* `{status['markets_found']}`\n- *الإشارات الجديدة:* `{status['signals_found']}`\n- *الصفقات النشطة:* `{status['active_trades_count']}`\n- *الفحص التالي:* `{next_scan_time}`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report_parts = ["*🔍 تقرير التشخيص والحالة*"]
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor(); cursor.execute("SELECT COUNT(*) FROM trades"); count = cursor.fetchone()[0]; conn.close()
        report_parts.append(f"✅ *قاعدة البيانات:* متصلة وسليمة ({count} trades recorded).")
    except Exception as e: report_parts.append(f"❌ *قاعدة البيانات:* فشل الاتصال! ({e})")
    exchanges_status = [f"  - `{ex_id}`: {'✅ متصل' if ex_id in bot_data.get('exchanges', {}) else '❌ غير متصل'}" for ex_id in EXCHANGES_TO_SCAN]
    report_parts.append("\n*📡 حالة الاتصال بالمنصات:*"); report_parts.extend(exchanges_status)
    report_parts.append("\n*⚙️ حالة المهام الخلفية:*")
    if context.job_queue:
        for job_name in ['perform_scan', 'track_open_trades', 'daily_report']:
            job = context.job_queue.get_jobs_by_name(job_name)
            if job: report_parts.append(f"  - `{job_name}`: ✅ نشطة (التالي: {job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S')})")
            else: report_parts.append(f"  - `{job_name}`: ❌ غير نشطة!")
    else: report_parts.append("  - ❌ لم يتم العثور على مدير المهام!")
    report_parts.append("\n*📊 حالة الماسحات (الاستراتيجيات):*")
    active_scanners = bot_data.get("settings", {}).get("active_scanners", [])
    report_parts.extend([f"  - `{s}`: {'✅' if s in active_scanners else '❌'}" for s in SCANNERS.keys()])
    await update.message.reply_text("\n".join(report_parts), parse_mode=ParseMode.MARKDOWN)

async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    target_message = update.callback_query.message if trade_id_from_callback else update.message
    # دالة مساعدة لتنسيق السعر بشكل ديناميكي
    def format_price(price):
        if price < 0.01: return f"{price:,.8f}"
        return f"{price:,.4f}"
    try:
        trade_id = trade_id_from_callback if trade_id_from_callback else int(context.args[0])
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor(); cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,));
        trade_row = cursor.fetchone(); conn.close()
        if not trade_row:
            await target_message.reply_text(f"لم يتم العثور على صفقة بالرقم `{trade_id}`.", parse_mode=ParseMode.MARKDOWN); return
        trade = dict(trade_row)

        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            closed_at_dt = datetime.strptime(trade['closed_at'], '%Y-%m-%d %H:%M:%S')
            message = f"📋 *ملخص الصفقة المغلقة #{trade_id}*\n\n*العملة:* `{trade['symbol']}`\n*الحالة:* `{trade['status']}`\n*تاريخ الإغلاق:* `{closed_at_dt.strftime('%Y-%m-%d %I:%M %p')}`\n*الربح/الخسارة:* `${trade.get('pnl_usdt', 0):+.2f} ({pnl_percent:+.2f}%)`"
        else:
            exchange = bot_data["exchanges"].get(trade['exchange'].lower())
            if not exchange: await target_message.reply_text("المنصة غير متصلة حالياً."); return
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            if current_price is None:
                await target_message.reply_text(f"لم أتمكن من جلب السعر الحالي لـ `{trade['symbol']}`.", parse_mode=ParseMode.MARKDOWN); return

            live_pnl = (current_price - trade['entry_price']) * trade['quantity']
            # [FIX] Ensured robust calculation and display for PNL percentage
            live_pnl_percent = (live_pnl / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            message = (f"📈 *متابعة حية للصفقة #{trade_id}*\n\n"
                       f"▫️ *العملة:* `{trade['symbol']}` | *الحالة:* `نشطة`\n"
                       f"▫️ *سعر الدخول:* `${format_price(trade['entry_price'])}`\n"
                       f"▫️ *السعر الحالي:* `${format_price(current_price)}`\n\n"
                       f"💰 *الربح/الخسارة الحالية:*\n`${live_pnl:+.2f} ({live_pnl_percent:+.2f}%)`")

        await target_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await target_message.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e:
        logging.error(f"Error in check_trade_command: {e}", exc_info=True)
        await target_message.reply_text("حدث خطأ أثناء فحص الصفقة.")


async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, entry_value_usdt, exchange FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
        active_trades = cursor.fetchall(); conn.close()
        if not active_trades:
            await update.message.reply_text("لا توجد صفقات نشطة حالياً."); return

        keyboard = [[InlineKeyboardButton(f"#{t['id']} | {t['symbol']} | ${t['entry_value_usdt']:.2f} | {t['exchange']}", callback_data=f"check_{t['id']}")] for t in active_trades]
        await update.message.reply_text("اختر صفقة لمتابعتها مباشرة:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logging.error(f"Error in show_active_trades_command: {e}")
        await update.message.reply_text("حدث خطأ أثناء جلب الصفقات النشطة.")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data.startswith("toggle_"): await toggle_scanner_callback(update, context)
    elif data == "back_to_settings":
        await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text="اختر الإعداد الذي تريد تعديله:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif data.startswith("check_"): await check_trade_command(update, context, trade_id_from_callback=int(data.split("_")[1]))

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handlers = {
        "📊 الإحصائيات": stats_command, "📈 الصفقات النشطة": show_active_trades_command,
        "ℹ️ مساعدة": help_command,
        "⚙️ الإعدادات": show_settings_menu, "👀 ماذا يجري في الخلفية؟": background_status_command,
        "🔬 فحص يدوي الآن": scan_now_command,
        "🔧 تعديل المعايير": show_set_parameter_instructions, "🔙 القائمة الرئيسية": start_command,
        "🔙 قائمة الإعدادات": show_settings_menu, "🎭 تفعيل/تعطيل الماسحات": show_scanners_menu
    }
    text = update.message.text
    if text in handlers: await handlers[text](update, context)
    elif re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text):
        match = re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text); param, value_str = match.groups()
        settings = bot_data["settings"]
        if param in settings and not isinstance(settings[param], (dict, list)):
            try:
                current_value = settings[param]
                if isinstance(current_value, bool): new_value = value_str.lower() in ['true', '1', 'yes', 'on']
                elif isinstance(current_value, int): new_value = int(value_str)
                else: new_value = float(value_str)
                settings[param] = new_value; save_settings()
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_value}`.")
            except ValueError: await update.message.reply_text(f"❌ قيمة غير صالحة.")
        else: await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود أو لا يمكن تعديله بهذه الطريقة.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def post_init(application: Application):
    logging.info("Post-init started. Initializing exchanges...")
    await initialize_exchanges()
    if not bot_data["exchanges"]: logging.critical("CRITICAL: Failed to connect during post_init."); return
    logging.info("Exchanges initialized. Setting up job queue...")
    if application.job_queue:
        application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
        application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
        report_time = dt_time(hour=23, minute=55, tzinfo=EGYPT_TZ)
        application.job_queue.run_daily(send_daily_report, time=report_time, name='daily_report')
        logging.info(f"Daily report scheduled for {report_time.strftime('%H:%M:%S')} {EGYPT_TZ}.")
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *محاكي التداول المتقدم (v13) جاهز للعمل!*", parse_mode=ParseMode.MARKDOWN)
    logging.info("Post-init finished.")

async def post_shutdown(application: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logging.info("Connections closed.")

def main():
    print("🚀 Starting Pro Trading Simulator Bot (v13)...")
    load_settings(); init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("scan", scan_now_command))
    application.add_handler(CommandHandler("report", daily_report_command))
    application.add_handler(CommandHandler("check", check_trade_command))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    application.add_error_handler(error_handler)
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error in __main__: {e}", exc_info=True)
