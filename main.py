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
DB_FILE = 'trading_bot_v4.db'

# --- إعداد مسجل الأحداث (Logger) --- #
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler("bot_v4.log"), logging.StreamHandler()])
logging.getLogger('httpx').setLevel(logging.WARNING)

# --- متغيرات الحالة العامة للبوت --- #
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}, "status_snapshot": {"last_scan_start_time": "N/A", "last_scan_end_time": "N/A", "markets_found": 0, "signals_found": 0, "active_trades_count": 0, "scan_in_progress": False}}

# --- إدارة الإعدادات مع إدارة المخاطر --- #
DEFAULT_SETTINGS = {
    # --- Portfolio & Risk Management ---
    "virtual_portfolio_balance_usdt": 1000.0,
    "virtual_trade_size_percentage": 5.0,
    "max_concurrent_trades": 3, # *** الميزة الجديدة: الحد الأقصى للصفقات النشطة ***
    # --- General Settings ---
    "active_strategy": "momentum_breakout", "top_n_symbols_by_volume": 250, "concurrent_workers": 10, "market_regime_filter_enabled": True,
    # --- Trade Parameters ---
    "take_profit_percentage": 4.0, "stop_loss_percentage": 2.0, "trailing_sl_enabled": True, "trailing_sl_activate_percent": 2.0, "trailing_sl_percent": 1.5,
    # --- Strategy Parameters ---
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68},
    "mean_reversion": {"bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_oversold_level": 30}
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
        # التأكد من وجود الإعدادات الجديدة وتحديث الملف إذا لزم الأمر
        updated = False
        for key, value in DEFAULT_SETTINGS.items():
            if key not in bot_data["settings"]:
                bot_data["settings"][key] = value
                updated = True
        if updated: save_settings()
    else:
        bot_data["settings"] = DEFAULT_SETTINGS
        save_settings()
    logging.info("Settings loaded successfully.")

def save_settings():
    with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
    logging.info("Settings saved successfully.")

# --- إدارة قاعدة البيانات (بدون تغيير) --- #
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

# --- دوال التحليل والاستراتيجيات (بدون تغيير) --- #
def analyze_momentum_breakout(df, params):
    try:
        df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        required = [f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"VWAP_D", f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] <= prev[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and last[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] > last[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and last['close'] > last[f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"] and last['close'] > last[f"VWAP_D"] and last[f"RSI_{params['rsi_period']}"] < params['rsi_max_level']): return {"reason": "Momentum Breakout"}
    except Exception: return None
    return None

def analyze_mean_reversion(df, params):
    try:
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        required = [f"BBL_{params['bbands_period']}_{params['bbands_stddev']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        last = df.iloc[-2]
        if (last['close'] < last[f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"] and last[f"RSI_{params['rsi_period']}"] < params['rsi_oversold_level']): return {"reason": "Mean Reversion (Oversold Bounce)"}
    except Exception: return None
    return None

STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}


# --- الدوال الأساسية للبوت --- #
async def initialize_exchanges():
    async def connect_with_retries(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True})
        for attempt in range(3):
            try: await exchange.load_markets(); bot_data["exchanges"][ex_id] = exchange; logging.info(f"Successfully connected to {ex_id}"); return
            except Exception as e: logging.error(f"Attempt {attempt + 1} failed for {ex_id}: {e}"); await asyncio.sleep(10)
        await exchange.close()
    await asyncio.gather(*[connect_with_retries(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
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
            active_strategy_func = STRATEGIES.get(settings['active_strategy'])
            if not active_strategy_func: continue
            exchange = bot_data["exchanges"].get(market_info['exchange'])
            if not exchange: continue
            ohlcv = await exchange.fetch_ohlcv(market_info['symbol'], TIMEFRAME, limit=150)
            if len(ohlcv) >= 50:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)
                analysis_result = active_strategy_func(df, settings[settings['active_strategy']])
                if analysis_result:
                    entry_price = df.iloc[-2]['close']
                    signal = {"symbol": market_info['symbol'], "exchange": market_info['exchange'].capitalize(), "entry_price": entry_price, "take_profit": entry_price * (1 + settings['take_profit_percentage'] / 100), "stop_loss": entry_price * (1 - settings['stop_loss_percentage'] / 100), "timestamp": df.index[-2], "reason": analysis_result['reason']}
                    results_list.append(signal)
            queue.task_done()
        except Exception: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']; status['scan_in_progress'] = True; status['last_scan_start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); status['signals_found'] = 0
    settings = bot_data["settings"]
    
    # *** [تحديث إدارة المخاطر] التحقق من عدد الصفقات النشطة أولاً ***
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'")
    active_trades_count = cursor.fetchone()[0]
    conn.close()
    
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
        if active_trades_count >= max_trades: # تحقق مرة أخرى بعد العثور على الإشارات
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
            active_trades_count += 1 # زيادة العداد يدوياً للصفقات الجديدة في نفس الدورة
            
    status['last_scan_end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False

async def send_telegram_message(bot, signal_data, is_new=False, status_update=None, update_type=None):
    message = ""; keyboard = None
    if is_new:
        message = (f"✅ *محاكاة صفقة جديدة* ✅\n\n*العملة:* `{signal_data['symbol']}` | *المنصة:* `{signal_data['exchange']}`\n*رقم الصفقة:* `{signal_data['trade_id']}`\n\n*سبب الدخول:* `{signal_data['reason']}`\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n*الكمية الافتراضية:* `{signal_data['quantity']:,.4f}`\n*قيمة الصفقة:* `${signal_data['entry_value_usdt']:,.2f}`\n\n🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 متابعة حية", callback_data=f"check_{signal_data['trade_id']}")]])
    elif status_update == 'ناجحة':
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100)
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*الربح:* `~${signal_data['pnl_usdt']:.2f} ({pnl_percent:+.2f}%)`"
    elif status_update == 'فاشلة':
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100)
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*الخسارة:* `~${abs(signal_data['pnl_usdt']):.2f} ({pnl_percent:+.2f}%)`"
    elif update_type == 'tsl_activation': message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى `${signal_data['stop_loss']:,.4f}`."
    if message: await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
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

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [["📊 الإحصائيات", "📈 الصفقات النشطة"], ["🔍 فحص يدوي", "⚙️ الإعدادات"], ["👀 ماذا يجري في الخلفية؟", "ℹ️ مساعدة"]]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في محاكي التداول الاحترافي! (v4)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
async def show_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("اختر استراتيجية التداول الجديدة:", reply_markup=ReplyKeyboardMarkup(strategy_menu_keyboard, resize_keyboard=True))

async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params_list = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, dict)])
    await update.message.reply_text(f"لتعديل معيار، أرسل:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير:*\n{params_list}", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("*مساعدة البوت*\n`/start` - بدء\n`/check <ID>` - متابعة صفقة مغلقة\n`📈 الصفقات النشطة` - عرض الصفقات المفتوحة ومتابعتها.", parse_mode=ParseMode.MARKDOWN)
async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot']['scan_in_progress']: await update.message.reply_text("⏳ الفحص الدوري قيد التنفيذ حالياً."); return
    await update.message.reply_text("👍 حسناً! سأبدأ الفحص اليدوي للسوق الآن..."); await perform_scan(context); await update.message.reply_text("✅ اكتمل الفحص اليدوي.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status"); stats_data = cursor.fetchall(); conn.close()
        counts = {s: c for s, c, p in stats_data}; pnl = {s: (p if p is not None else 0) for s, c, p in stats_data}
        total = sum(counts.values()); active = counts.get('نشطة', 0); successful = counts.get('ناجحة', 0); failed = counts.get('فاشلة', 0)
        closed_trades = successful + failed; win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0; total_pnl = sum(pnl.values())
        stats_message = (f"*📊 إحصائيات محفظة التداول الافتراضية*\n\n📈 *رصيد المحفظة الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n💰 *إجمالي الربح/الخسارة:* `${total_pnl:+.2f}`\n\n- *إجمالي الصفقات:* `{total}`\n- *النشطة حالياً:* `{active}`\n- *الناجحة:* `{successful}` | *الربح:* `${pnl.get('ناجحة', 0):.2f}`\n- *الفاشلة:* `{failed}` | *الخسارة:* `${abs(pnl.get('فاشلة', 0)):.2f}`\n- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`")
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Error in stats_command: {e}", exc_info=True)

async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']; next_scan_time = "قيد التنفيذ"
    if not status['scan_in_progress'] and context.job_queue:
        next_scan_job = context.job_queue.get_jobs_by_name('perform_scan')
        if next_scan_job and next_scan_job[0].next_t: next_scan_time = next_scan_job[0].next_t.astimezone().strftime('%H:%M:%S')
    message = (f"🤖 *حالة البوت في الخلفية*\n\n*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n- *آخر فحص بدأ في:* `{status['last_scan_start_time']}`\n- *آخر فحص انتهى في:* `{status['last_scan_end_time']}`\n- *العملات المفحوصة:* `{status['markets_found']}`\n- *الإشارات الجديدة:* `{status['signals_found']}`\n- *الصفقات النشطة:* `{status['active_trades_count']}`\n- *الفحص التالي في:* `{next_scan_time}`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
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
    # *** [زر جديد] لعرض الصفقات النشطة ***
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT id, symbol, entry_price FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
    active_trades = cursor.fetchall(); conn.close()
    if not active_trades:
        await update.message.reply_text("لا توجد صفقات نشطة حالياً."); return
    
    keyboard = []
    for trade in active_trades:
        button_text = f"ID: {trade['id']} | {trade['symbol']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"check_{trade['id']}")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("اختر صفقة لمتابعتها مباشرة:", reply_markup=reply_markup)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.startswith("check_"):
        trade_id = int(query.data.split("_")[1])
        # تعديل صغير لتجنب إرسال رسالتين
        await check_trade_command(query, context, trade_id_from_callback=trade_id)

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "📈 الصفقات النشطة": await show_active_trades_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)
    elif text == "⚙️ الإعدادات": await show_settings_menu(update, context)
    elif text == "👀 ماذا يجري في الخلفية؟": await background_status_command(update, context)
    elif text == "📈 تغيير الاستراتيجية": await show_strategy_menu(update, context)
    elif text == "🔧 تعديل المعايير": await show_set_parameter_instructions(update, context)
    elif text == "🔙 القائمة الرئيسية": await start_command(update, context)
    elif text in ["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"]:
        strategy_name = "momentum_breakout" if text == "🚀 الزخم والاندفاع" else "mean_reversion"; bot_data["settings"]["active_strategy"] = strategy_name; save_settings()
        await update.message.reply_text(f"✅ تم تفعيل استراتيجية `{text.split(' ')[0]}`."); await show_settings_menu(update, context)
    elif text == "🔙 قائمة الإعدادات": await show_settings_menu(update, context)
    elif re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text):
        match = re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text); param, value_str = match.groups()
        settings = bot_data["settings"]
        if param in settings and not isinstance(settings[param], dict):
            try:
                current_value = settings[param]
                if isinstance(current_value, bool): new_value = value_str.lower() in ['true', '1', 'yes', 'on']
                elif isinstance(current_value, int): new_value = int(value_str)
                elif isinstance(current_value, float): new_value = float(value_str)
                else: new_value = str(value_str)
                settings[param] = new_value; save_settings()
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_value}`.")
            except ValueError: await update.message.reply_text(f"❌ قيمة غير صالحة.")
        else: await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود.")

async def post_init(application: Application):
    await asyncio.sleep(2); await initialize_exchanges()
    if not bot_data["exchanges"]: logging.critical("CRITICAL: Failed to connect."); return
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
    exchange_names = ", ".join(bot_data["exchanges"].keys())
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *محاكي التداول الاحترافي (v4) جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *رصيد المحفظة:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n- *الحد الأقصى للصفقات:* `{bot_data['settings']['max_concurrent_trades']}`", parse_mode=ParseMode.MARKDOWN)

async def post_shutdown(application: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logging.info("Connections closed.")

def main():
    print("🚀 Starting Pro Trading Simulator Bot (v4 - Risk Management)...")
    load_settings(); init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_command)); application.add_handler(CommandHandler("check", check_trade_command)); application.add_handler(CallbackQueryHandler(button_callback_handler)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error in __main__: {e}", exc_info=True)

