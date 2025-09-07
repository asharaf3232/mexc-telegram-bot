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
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# --- الإعدادات الأساسية --- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')

if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120
SETTINGS_FILE = 'settings.json'
DB_FILE = 'trading_bot_v6.db'

# --- إعداد مسجل الأحداث (Logger) --- #
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler("bot_v6.log"), logging.StreamHandler()])
logging.getLogger('httpx').setLevel(logging.WARNING)

# --- متغيرات الحالة العامة للبوت --- #
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}, "status_snapshot": {"last_scan_start_time": "N/A", "last_scan_end_time": "N/A", "markets_found": 0, "signals_found": 0, "active_trades_count": 0, "scan_in_progress": False}}

# --- إدارة الإعدادات مع إدارة المخاطر الديناميكية --- #
DEFAULT_SETTINGS = {
    # --- Portfolio & General ---
    "virtual_portfolio_balance_usdt": 1000.0,
    "virtual_trade_size_percentage": 5.0,
    "max_concurrent_trades": 5,
    "top_n_symbols_by_volume": 250,
    "concurrent_workers": 10,
    "market_regime_filter_enabled": True,

    # --- Active Scanners (Phase 2) ---
    "active_scanners": ["momentum_breakout", "breakout_squeeze"], # قائمة الماسحات النشطة

    # --- Dynamic Risk Management (Phase 3) ---
    "use_dynamic_risk_management": True,
    "atr_period": 14,
    "atr_sl_multiplier": 2.0,
    "risk_reward_ratio": 1.5,

    # --- Static Risk Management (Fallback) ---
    "take_profit_percentage": 4.0,
    "stop_loss_percentage": 2.0,
    "trailing_sl_enabled": True,
    "trailing_sl_activate_percent": 2.0,
    "trailing_sl_percent": 1.5,

    # --- Strategy Parameters ---
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68},
    "mean_reversion": {"bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_oversold_level": 30},
    "breakout_squeeze": {"bbands_period": 20, "bbands_stddev": 2.0, "squeeze_threshold_percent": 3.5},
    "rsi_divergence": {"rsi_period": 14, "lookback_period": 35, "peak_trough_lookback": 5} # فترة البحث عن الانفراج
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
        updated = False
        for key, value in DEFAULT_SETTINGS.items():
            if key not in bot_data["settings"]:
                bot_data["settings"][key] = value; updated = True
            elif isinstance(value, dict):
                 for sub_key, sub_value in value.items():
                     if sub_key not in bot_data["settings"][key]:
                         bot_data["settings"][key][sub_key] = sub_value; updated = True
        if updated: save_settings()
    else:
        bot_data["settings"] = DEFAULT_SETTINGS
        save_settings()
    logging.info("Settings loaded successfully.")

def save_settings():
    with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
    logging.info("Settings saved successfully.")

# --- إدارة قاعدة البيانات --- #
def init_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, exchange TEXT, symbol TEXT, entry_price REAL, take_profit REAL, stop_loss REAL, quantity REAL, entry_value_usdt REAL, status TEXT, exit_price REAL, closed_at TEXT, exit_value_usdt REAL, pnl_usdt REAL, trailing_sl_active BOOLEAN, highest_price REAL, reason TEXT)
    ''')
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully.")

def log_recommendation_to_db(signal):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO trades (timestamp, exchange, symbol, entry_price, take_profit, stop_loss, quantity, entry_value_usdt, status, trailing_sl_active, highest_price, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'], signal['entry_price'], signal['take_profit'], signal['stop_loss'], signal['quantity'], signal['entry_value_usdt'], 'نشطة', False, signal['entry_price'], signal['reason']))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id

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
    """Helper to find peaks and troughs in a series."""
    from scipy.signal import find_peaks
    peaks, _ = find_peaks(series, distance=lookback)
    troughs, _ = find_peaks(-series, distance=lookback)
    return peaks, troughs

def analyze_rsi_divergence(df, params):
    """
    [نسخة احترافية] ماسح الانفراج الإيجابي والسلبي.
    المنطق: يستخدم مكتبة Scipy للعثور على القمم والقيعان بشكل دقيق، ثم يقارن آخر قمتين/قاعين بين السعر و RSI.
    """
    try:
        rsi_col = f"RSI_{params['rsi_period']}"
        df.ta.rsi(length=params['rsi_period'], append=True, col_names=(rsi_col,))
        
        subset = df.iloc[-params['lookback_period']:]
        
        price_peaks_idx, price_troughs_idx = find_divergence_points(subset['high'], params['peak_trough_lookback']), find_divergence_points(subset['low'], params['peak_trough_lookback'])
        rsi_peaks_idx, rsi_troughs_idx = find_divergence_points(subset[rsi_col], params['peak_trough_lookback']), find_divergence_points(subset[rsi_col], params['peak_trough_lookback'])
        
        # 1. Check for Bullish Divergence (Lower Low in Price, Higher Low in RSI)
        if len(price_troughs_idx[0]) >= 2 and len(rsi_troughs_idx[0]) >= 2:
            p_low1, p_low2 = subset.iloc[price_troughs_idx[0][-2]], subset.iloc[price_troughs_idx[0][-1]]
            r_low1, r_low2 = subset.iloc[rsi_troughs_idx[0][-2]], subset.iloc[rsi_troughs_idx[0][-1]]
            if p_low2['low'] < p_low1['low'] and r_low2[rsi_col] > r_low1[rsi_col]:
                return {"reason": "Bullish RSI Divergence", "type": "long"}

        # 2. Check for Bearish Divergence (Higher High in Price, Lower High in RSI)
        if len(price_peaks_idx[0]) >= 2 and len(rsi_peaks_idx[0]) >= 2:
            p_high1, p_high2 = subset.iloc[price_peaks_idx[0][-2]], subset.iloc[price_peaks_idx[0][-1]]
            r_high1, r_high2 = subset.iloc[rsi_peaks_idx[0][-2]], subset.iloc[rsi_peaks_idx[0][-1]]
            if p_high2['high'] > p_high1['high'] and r_high2[rsi_col] < r_high1[rsi_col]:
                # إشارة البيع يمكن استخدامها في المستقبل لإغلاق صفقات الشراء
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
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True})
        try: await exchange.load_markets(); bot_data["exchanges"][ex_id] = exchange; logging.info(f"Connected to {ex_id}")
        except Exception as e: logging.error(f"Failed for {ex_id}: {e}"); await exchange.close()
    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    # ... Same as before
    all_tickers = []
    async def fetch_for_exchange(ex_id, exchange):
        try: tickers = await exchange.fetch_tickers(); return [dict(t, exchange=ex_id) for t in tickers.values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch_for_exchange(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()])
    for res in results: all_tickers.extend(res)
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and 'USDT' in t['symbol'] and not any(k in t['symbol'] for k in ['UP','DOWN','3L','3S'])]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    unique_symbols = {t['symbol']: {'exchange': t['exchange'], 'symbol': t['symbol']} for t in sorted_tickers}
    final_list = list(unique_symbols.values())[:bot_data["settings"]['top_n_symbols_by_volume']]
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    return final_list


async def worker(queue, results_list, settings):
    while not queue.empty():
        try:
            market_info = await queue.get()
            active_scanners = settings.get('active_scanners', [])
            if not active_scanners: continue
            exchange = bot_data["exchanges"].get(market_info['exchange'])
            if not exchange: continue

            ohlcv = await exchange.fetch_ohlcv(market_info['symbol'], TIMEFRAME, limit=150)
            if len(ohlcv) < 50: queue.task_done(); continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)
            df.ta.atr(length=settings['atr_period'], append=True)

            for scanner_name in active_scanners:
                scanner_func = SCANNERS.get(scanner_name)
                if not scanner_func: continue
                analysis_result = scanner_func(df, settings.get(scanner_name, {}))
                
                # تجاهل إشارات البيع في المنطق الرئيسي، لكنها مفيدة للاختبار التاريخي
                if analysis_result and analysis_result.get("type") == "long":
                    entry_price = df.iloc[-2]['close']
                    current_atr = df.iloc[-2].get(f"ATRr_{settings['atr_period']}", 0)
                    
                    if settings.get("use_dynamic_risk_management", False) and current_atr > 0:
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
    # ... Same as before, no changes to this logic
    status = bot_data['status_snapshot']; status['scan_in_progress'] = True; status['last_scan_start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); status['signals_found'] = 0
    settings = bot_data["settings"]
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'"); active_trades_count = cursor.fetchone()[0]; conn.close()
    max_trades = settings.get("max_concurrent_trades", 3)
    if active_trades_count >= max_trades:
        logging.info(f"Skipping scan: Max concurrent trades limit reached ({active_trades_count}/{max_trades}).")
        status['scan_in_progress'] = False; return

    if settings.get('market_regime_filter_enabled', True) and not await check_market_regime(): logging.info("Skipping scan: Bearish market."); status['scan_in_progress'] = False; return
    
    top_markets = await aggregate_top_movers()
    if not top_markets: logging.info("No markets to scan."); status['scan_in_progress'] = False; return
    
    queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
    signals = []; worker_tasks = [asyncio.create_task(worker(queue, signals, settings)) for _ in range(settings['concurrent_workers'])]
    await queue.join(); [task.cancel() for task in worker_tasks]

    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        if active_trades_count >= max_trades:
            logging.info(f"Max trades limit reached while processing signals. Halting new entries."); break
            
        symbol = signal['symbol']; current_time = time.time()
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (SCAN_INTERVAL_SECONDS * 4):
            portfolio_balance = settings.get("virtual_portfolio_balance_usdt", 1000.0)
            trade_size_percent = settings.get("virtual_trade_size_percentage", 5.0)
            trade_amount_usdt = portfolio_balance * (trade_size_percent / 100)
            quantity = trade_amount_usdt / signal['entry_price']
            
            signal.update({'quantity': quantity, 'entry_value_usdt': trade_amount_usdt})
            
            trade_id = log_recommendation_to_db(signal)
            signal['trade_id'] = trade_id
            await send_telegram_message(context.bot, signal, is_new=True)
            
            last_signal_time[symbol] = current_time
            status['signals_found'] += 1
            active_trades_count += 1
            
    status['last_scan_end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False

async def send_telegram_message(bot, signal_data, is_new=False, status_update=None, update_type=None):
    # ... Same as before, no changes
    message = ""; keyboard = None
    if is_new:
        message = (f"✅ *محاكاة صفقة جديدة* ✅\n\n*العملة:* `{signal_data['symbol']}` | *المنصة:* `{signal_data['exchange']}`\n*رقم الصفقة:* `{signal_data['trade_id']}`\n\n*سبب الدخول:* `{signal_data['reason']}`\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n*قيمة الصفقة:* `${signal_data['entry_value_usdt']:,.2f}`\n\n🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 متابعة حية", callback_data=f"check_{signal_data['trade_id']}")]])
    elif status_update == 'ناجحة':
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100) if signal_data['entry_value_usdt'] else 0
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*الربح:* `~${signal_data['pnl_usdt']:.2f} ({pnl_percent:+.2f}%)`"
    elif status_update == 'فاشلة':
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100) if signal_data['entry_value_usdt'] else 0
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*الخسارة:* `~${abs(signal_data['pnl_usdt']):.2f} ({pnl_percent:+.2f}%)`"
    elif update_type == 'tsl_activation': message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى `${signal_data['stop_loss']:,.4f}`."
    if message: await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    # ... Same as before, no changes
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'")
    active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
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
                highest_price = max(trade['highest_price'], current_price)
                if not trade['trailing_sl_active'] and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price'] * (1 + (settings['trailing_sl_activate_percent'] - settings['trailing_sl_percent']) / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade['trailing_sl_active']:
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade['highest_price']: return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db = []; portfolio_pnl = 0.0
    for result in filter(None, results):
        trade_id, status = result['id'], result['status']
        original_trade = next((t for t in active_trades if t['id'] == trade_id), None)
        if not original_trade: continue
        
        if status in ['ناجحة', 'فاشلة']:
            exit_value = result['exit_price'] * original_trade['quantity']; pnl = exit_value - original_trade['entry_value_usdt']; portfolio_pnl += pnl
            updates_to_db.append(("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?", (status, result['exit_price'], datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), exit_value, pnl, trade_id)))
            await send_telegram_message(context.bot, {**original_trade, **result, 'pnl_usdt': pnl, 'entry_value_usdt': original_trade['entry_value_usdt']}, status_update=status)
        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, trade_id)))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl': updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], trade_id)))
        elif status == 'update_peak': updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], trade_id)))
    
    if updates_to_db: conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); [cursor.execute(q, p) for q, p in updates_to_db]; conn.commit(); conn.close()
    if portfolio_pnl != 0.0: bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl; save_settings(); logging.info(f"Portfolio balance updated by ${portfolio_pnl:.2f}.")

async def check_market_regime():
    try:
        binance = bot_data["exchanges"].get('binance');
        if not binance: return True
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55); df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['sma50'] = df['close'].rolling(window=50).mean()
        return df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception: return True

# --- [المرحلة 4] محرك الاختبار التاريخي المدمج --- #

async def fetch_historical_data(symbol, timeframe, limit):
    logging.info(f"Fetching historical data for {symbol}...")
    exchange = ccxt.binance() # Use a new instance for thread safety in backtesting
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        logging.info(f"Successfully fetched {len(df)} candles for {symbol}.")
        await exchange.close()
        return df
    except Exception as e:
        logging.error(f"Error fetching data for {symbol}: {e}")
        await exchange.close()
        return None

def analyze_backtest_results(trades, symbol, timeframe, limit):
    if not trades: return "\nلم يتم تنفيذ أي صفقات."
    df_trades = pd.DataFrame(trades)
    total_trades = len(df_trades)
    wins = df_trades[df_trades['status'] == 'Take Profit']
    losses = df_trades[df_trades['status'] == 'Stop Loss']
    win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
    total_pnl = df_trades['pnl'].sum()
    avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
    risk_reward_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    df_trades['cumulative_pnl'] = df_trades['pnl'].cumsum()
    df_trades['peak'] = df_trades['cumulative_pnl'].cummax()
    df_trades['drawdown'] = df_trades['peak'] - df_trades['cumulative_pnl']
    max_drawdown = df_trades['drawdown'].max()

    report = (
        f"--- 📜 *تقرير الاختبار التاريخي* ---\n\n"
        f"*العملة:* `{symbol}` | *الإطار الزمني:* `{timeframe}` | *الشموع:* `{limit}`\n\n"
        f"▫️ *إجمالي الصفقات:* `{total_trades}`\n"
        f"✅ *الصفقات الرابحة:* `{len(wins)}`\n"
        f"❌ *الصفقات الخاسرة:* `{len(losses)}`\n"
        f"📈 *معدل النجاح:* `{win_rate:.2f}%`\n\n"
        f"💰 *إجمالي الربح/الخسارة:* `{total_pnl:+.4f}` (لحجم 1 وحدة)\n"
        f"👍 *متوسط الربح:* `{avg_win:.4f}`\n"
        f"👎 *متوسط الخسارة:* `{avg_loss:.4f}`\n"
        f"⚖️ *متوسط المخاطرة/العائد:* `1:{risk_reward_ratio:.2f}`\n"
        f"📉 *أقصى تراجع في الرصيد:* `-{max_drawdown:.4f}`\n"
        f"------------------------"
    )
    return report

async def run_backtest_logic(update: Update, symbol: str, timeframe: str, limit: int):
    try:
        df = await fetch_historical_data(symbol, timeframe, limit)
        if df is None or len(df) < 50:
            await update.message.reply_text(f"لم أتمكن من جلب بيانات كافية لـ `{symbol}`.", parse_mode=ParseMode.MARKDOWN)
            return

        trades = []
        active_trade = None
        settings = bot_data["settings"]

        for i in range(50, len(df)):
            current_candle = df.iloc[i]
            historical_df = df.iloc[0:i].copy()
            
            if active_trade:
                if current_candle['low'] <= active_trade['stop_loss']:
                    active_trade.update({'exit_price': active_trade['stop_loss'], 'status': 'Stop Loss'})
                elif current_candle['high'] >= active_trade['take_profit']:
                    active_trade.update({'exit_price': active_trade['take_profit'], 'status': 'Take Profit'})
                if 'status' in active_trade:
                    active_trade['pnl'] = (active_trade['exit_price'] - active_trade['entry_price']) * active_trade['size']
                    trades.append(active_trade); active_trade = None; continue

            if not active_trade:
                historical_df.ta.atr(length=settings['atr_period'], append=True)
                for scanner_name in settings['active_scanners']:
                    scanner_func = SCANNERS.get(scanner_name)
                    if not scanner_func: continue
                    result = scanner_func(historical_df, settings.get(scanner_name, {}))
                    if result and result.get('type') == 'long':
                        entry_price = historical_df.iloc[-1]['close']
                        current_atr = historical_df.iloc[-1].get(f"ATRr_{settings['atr_period']}", 0)
                        if current_atr == 0: continue
                        risk_per_unit = current_atr * settings['atr_sl_multiplier']
                        active_trade = {
                            'entry_price': entry_price, 'stop_loss': entry_price - risk_per_unit,
                            'take_profit': entry_price + (risk_per_unit * settings['risk_reward_ratio']),
                            'size': 1, 'reason': result['reason']
                        }
                        break
        
        report = analyze_backtest_results(trades, symbol, timeframe, limit)
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Error during backtest execution: {e}", exc_info=True)
        await update.message.reply_text(f"حدث خطأ أثناء تشغيل الاختبار التاريخي: {e}")

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [["📊 الإحصائيات", "📈 الصفقات النشطة"], ["🧪 اختبار تاريخي", "⚙️ الإعدادات"], ["👀 ماذا يجري في الخلفية؟", "ℹ️ مساعدة"]]
settings_menu_keyboard = [["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في محاكي التداول المتقدم! (v6)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))

async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Same as before
    params_list = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, dict)])
    active_scanners = bot_data["settings"].get("active_scanners", [])
    scanners_status = "\n".join([f"- `{s}` ({'✅' if s in active_scanners else '❌'})" for s in SCANNERS.keys()])
    
    message = (f"لتعديل معيار، أرسل:\n`اسم_المعيار = قيمة_جديدة`\n\n"
               f"*المعايير العامة:*\n{params_list}\n\n"
               f"*الماسحات الحالية:*\n{scanners_status}\n\n"
               f"لتفعيل أو تعطيل الماسحات، استخدم:\n`active_scanners = [\"scanner1\", \"scanner2\"]`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("*مساعدة البوت*\n`/start` - بدء\n`/check <ID>` - متابعة صفقة\n`/backtest <S> <T> <C>` - إجراء اختبار تاريخي.", parse_mode=ParseMode.MARKDOWN)

async def backtest_instructions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "لإجراء اختبار تاريخي، استخدم الأمر `/backtest` مع تحديد المعلمات المطلوبة.\n\n"
        "🔹 *الصيغة:* `/backtest SYMBOL TIMEFRAME CANDLES`\n"
        "🔹 *مثال:* `/backtest BTC/USDT 15m 2000`\n\n"
        "سيقوم البوت بتحليل أداء الاستراتيجيات النشطة على البيانات التاريخية وإرسال تقرير بالنتائج.",
        parse_mode=ParseMode.MARKDOWN
    )

async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args or len(context.args) != 3:
            await backtest_instructions_command(update, context); return
        symbol, timeframe, limit_str = context.args; limit = int(limit_str)
        await update.message.reply_text(f"⏳ جاري بدء الاختبار التاريخي لـ `{symbol}`... قد يستغرق هذا بعض الوقت.", parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(run_backtest_logic(update, symbol, timeframe, limit))
    except (ValueError, IndexError):
        await update.message.reply_text("❌ خطأ في المدخلات. تأكد من أن عدد الشموع هو رقم صحيح.")
    except Exception as e:
        logging.error(f"Error in backtest_command: {e}", exc_info=True)
        await update.message.reply_text(f"حدث خطأ غير متوقع: {e}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Same as before
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status"); stats_data = cursor.fetchall(); conn.close()
        counts = {s: c for s, c, p in stats_data}; pnl = {s: (p if p is not None else 0) for s, c, p in stats_data}
        total = sum(counts.values()); active = counts.get('نشطة', 0); successful = counts.get('ناجحة', 0); failed = counts.get('فاشلة', 0)
        closed_trades = successful + failed; win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0; total_pnl = sum(pnl.values())
        stats_message = (f"*📊 إحصائيات المحفظة*\n\n📈 *الرصيد الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n💰 *إجمالي الربح/الخسارة:* `${total_pnl:+.2f}`\n\n- *إجمالي الصفقات:* `{total}` (`{active}` نشطة)\n- *الناجحة:* `{successful}` | *الربح:* `${pnl.get('ناجحة', 0):.2f}`\n- *الفاشلة:* `{failed}` | *الخسارة:* `${abs(pnl.get('فاشلة', 0)):.2f}`\n- *معدل النجاح:* `{win_rate:.2f}%`")
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Error in stats_command: {e}", exc_info=True)


async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Same as before
    status = bot_data['status_snapshot']; next_scan_time = "قيد التنفيذ"
    if not status['scan_in_progress'] and context.job_queue:
        next_scan_job = context.job_queue.get_jobs_by_name('perform_scan')
        if next_scan_job and next_scan_job[0].next_t: next_scan_time = next_scan_job[0].next_t.astimezone().strftime('%H:%M:%S')
    message = (f"🤖 *حالة البوت في الخلفية*\n\n*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n- *آخر فحص بدأ:* `{status['last_scan_start_time']}`\n- *آخر فحص انتهى:* `{status['last_scan_end_time']}`\n- *العملات المفحوصة:* `{status['markets_found']}`\n- *الإشارات الجديدة:* `{status['signals_found']}`\n- *الصفقات النشطة:* `{status['active_trades_count']}`\n- *الفحص التالي:* `{next_scan_time}`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    # ... Same as before
    try:
        trade_id = trade_id_from_callback if trade_id_from_callback else int(context.args[0])
        conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor(); cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)); trade = dict(cursor.fetchone()) if cursor.rowcount > 0 else None; conn.close()
        if not trade: await update.message.reply_text(f"لم يتم العثور على صفقة بالرقم `{trade_id}`."); return
        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade['entry_value_usdt'] != 0 else 0
            message = f"📋 *ملخص الصفقة المغلقة #{trade_id}*\n\n*العملة:* `{trade['symbol']}`\n*الحالة:* `{trade['status']}`\n*تاريخ الإغلاق:* `{trade['closed_at']}`\n*الربح/الخسارة:* `${trade['pnl_usdt']:+.2f} ({pnl_percent:+.2f}%)`"
        else:
            exchange = bot_data["exchanges"].get(trade['exchange'].lower()); ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            current_value = current_price * trade['quantity']; live_pnl = current_value - trade['entry_value_usdt']; live_pnl_percent = (live_pnl / trade['entry_value_usdt'] * 100) if trade['entry_value_usdt'] != 0 else 0
            message = f"📈 *متابعة حية للصفقة #{trade_id}*\n\n*العملة:* `{trade['symbol']}`\n*الحالة:* `نشطة`\n\n*سعر الدخول:* `${trade['entry_price']:,.4f}`\n*السعر الحالي:* `${current_price:,.4f}`\n\n💰 *الربح/الخسارة الحالية:*\n`${live_pnl:+.2f} ({live_pnl_percent:+.2f}%)`"
        
        target = update if not trade_id_from_callback else update.effective_message
        await target.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await update.message.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e: logging.error(f"Error in check_trade_command: {e}", exc_info=True)


async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Same as before
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT id, symbol FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
    active_trades = cursor.fetchall(); conn.close()
    if not active_trades:
        await update.message.reply_text("لا توجد صفقات نشطة حالياً."); return
    keyboard = [[InlineKeyboardButton(f"ID: {t['id']} | {t['symbol']}", callback_data=f"check_{t['id']}")] for t in active_trades]
    await update.message.reply_text("اختر صفقة لمتابعتها مباشرة:", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.startswith("check_"):
        await check_trade_command(query, context, trade_id_from_callback=int(query.data.split("_")[1]))

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handlers = {
        "📊 الإحصائيات": stats_command, "📈 الصفقات النشطة": show_active_trades_command,
        "ℹ️ مساعدة": help_command, "🧪 اختبار تاريخي": backtest_instructions_command,
        "⚙️ الإعدادات": show_settings_menu, "👀 ماذا يجري في الخلفية؟": background_status_command,
        "🔧 تعديل المعايير": show_set_parameter_instructions, "🔙 القائمة الرئيسية": start_command,
        "🔙 قائمة الإعدادات": show_settings_menu
    }
    text = update.message.text
    if text in handlers:
        await handlers[text](update, context)
    elif re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text):
        match = re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text); param, value_str = match.groups()
        settings = bot_data["settings"]
        if param in settings and not isinstance(settings[param], dict):
            try:
                if param == 'active_scanners':
                    new_value = [s.strip().replace('"',"") for s in value_str[1:-1].split(',')]
                else:
                    current_value = settings[param]
                    if isinstance(current_value, bool): new_value = value_str.lower() in ['true', '1', 'yes', 'on']
                    elif isinstance(current_value, int): new_value = int(value_str)
                    elif isinstance(current_value, float): new_value = float(value_str)
                    else: new_value = str(value_str)
                settings[param] = new_value; save_settings()
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_value}`.")
            except (ValueError, json.JSONDecodeError): await update.message.reply_text(f"❌ قيمة غير صالحة.")
        else: await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود.")

async def post_init(application: Application):
    await asyncio.sleep(2); await initialize_exchanges()
    if not bot_data["exchanges"]: logging.critical("CRITICAL: Failed to connect."); return
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
    exchange_names = ", ".join(bot_data["exchanges"].keys())
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *محاكي التداول المتقدم (v6) جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الماسحات النشطة:* `{', '.join(bot_data['settings']['active_scanners'])}`\n- *إدارة المخاطر:* `{'ديناميكية (ATR)' if bot_data['settings']['use_dynamic_risk_management'] else 'ثابتة (%)'}`", parse_mode=ParseMode.MARKDOWN)

async def post_shutdown(application: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logging.info("Connections closed.")

def main():
    print("🚀 Starting Pro Trading Simulator Bot (v6 - Integrated Backtester)...")
    load_settings(); init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    
    # Add all handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_trade_command))
    application.add_handler(CommandHandler("backtest", backtest_command)) # New command handler
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error in __main__: {e}", exc_info=True)

