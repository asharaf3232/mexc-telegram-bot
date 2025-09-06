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
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- الإعدادات الأساسية --- #
# تأكد من تعيين هذه المتغيرات في بيئة التشغيل الخاصة بك
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE') # استبدل إذا لم تكن تستخدم متغيرات البيئة
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')   # استبدل إذا لم تكن تستخدم متغيرات البيئة

if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900  # 15 دقيقة
TRACK_INTERVAL_SECONDS = 120 # دقيقتين
SETTINGS_FILE = 'settings.json'
DB_FILE = 'trading_bot.db'   # ملف قاعدة البيانات الجديد

# --- إعداد مسجل الأحداث (Logger) --- #
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
# تجاهل رسائل DEBUG المزعجة من بعض المكتبات
logging.getLogger('httpx').setLevel(logging.WARNING)

# --- متغيرات الحالة العامة للبوت --- #
bot_data = {
    "exchanges": {},
    "last_signal_time": {},
    "settings": {},
    "status_snapshot": { # لتتبع ما يحدث في الخلفية
        "last_scan_start_time": "N/A",
        "last_scan_end_time": "N/A",
        "markets_found": 0,
        "signals_found": 0,
        "active_trades_count": 0,
        "scan_in_progress": False
    }
}

# --- إدارة الإعدادات --- #
DEFAULT_SETTINGS = {
    "active_strategy": "momentum_breakout",
    "top_n_symbols_by_volume": 150,
    "concurrent_workers": 10,
    "market_regime_filter_enabled": True,
    "take_profit_percentage": 4.0,
    "stop_loss_percentage": 2.0,
    "trailing_sl_enabled": True,
    "trailing_sl_activate_percent": 2.0,
    "trailing_sl_percent": 1.5,
    "momentum_breakout": {
        "vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68
    },
    "mean_reversion": {
        "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_oversold_level": 30
    }
}

def load_settings():
    """تحميل الإعدادات من ملف JSON."""
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            bot_data["settings"] = json.load(f)
    else:
        bot_data["settings"] = DEFAULT_SETTINGS
        save_settings()
    logging.info("Settings loaded successfully.")

def save_settings():
    """حفظ الإعدادات في ملف JSON."""
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(bot_data["settings"], f, indent=4)
    logging.info("Settings saved successfully.")

# --- إدارة قاعدة البيانات (SQLite) --- #
def init_database():
    """إنشاء جدول الصفقات في قاعدة البيانات إذا لم يكن موجودًا."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            exchange TEXT,
            symbol TEXT,
            entry_price REAL,
            take_profit REAL,
            stop_loss REAL,
            status TEXT,
            exit_price REAL,
            closed_at TEXT,
            trailing_sl_active BOOLEAN,
            highest_price REAL,
            reason TEXT
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully.")

def log_recommendation_to_db(signal):
    """تسجيل توصية جديدة في قاعدة البيانات."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO trades (timestamp, exchange, symbol, entry_price, take_profit, stop_loss, status, exit_price, closed_at, trailing_sl_active, highest_price, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'], signal['entry_price'],
        signal['take_profit'], signal['stop_loss'], 'نشطة', None, None, False, signal['entry_price'], signal['reason']
    ))
    conn.commit()
    conn.close()

# --- دوال التحليل والاستراتيجيات --- #
# (لا تغييرات هنا، تبقى كما هي)
def analyze_momentum_breakout(df, params):
    try:
        df.ta.vwap(append=True)
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
        df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True)
        df.ta.rsi(length=params['rsi_period'], append=True)
        
        required = [f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"VWAP_D", f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] <= prev[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and
            last[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] > last[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and
            last['close'] > last[f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"] and
            last['close'] > last[f"VWAP_D"] and
            last[f"RSI_{params['rsi_period']}"] < params['rsi_max_level']):
            return {"reason": "Momentum Breakout"}
    except Exception as e:
        logging.error(f"Error in analyze_momentum_breakout: {e}", exc_info=True)
        return None
    return None

def analyze_mean_reversion(df, params):
    try:
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
        df.ta.rsi(length=params['rsi_period'], append=True)
        
        required = [f"BBL_{params['bbands_period']}_{params['bbands_stddev']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        
        last = df.iloc[-2]
        if (last['close'] < last[f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"] and
            last[f"RSI_{params['rsi_period']}"] < params['rsi_oversold_level']):
            return {"reason": "Mean Reversion (Oversold Bounce)"}
    except Exception as e:
        logging.error(f"Error in analyze_mean_reversion: {e}", exc_info=True)
        return None
    return None

STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}


# --- الدوال الأساسية للبوت --- #
async def initialize_exchanges():
    """الاتصال بالمنصات عند بدء التشغيل."""
    async def connect_with_retries(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True})
        for attempt in range(3):
            try:
                await exchange.load_markets()
                bot_data["exchanges"][ex_id] = exchange
                logging.info(f"Successfully connected to {ex_id} on attempt {attempt + 1}")
                return
            except Exception as e:
                logging.error(f"Attempt {attempt + 1} failed to connect to {ex_id}: {type(e).__name__} - {e}")
                if attempt < 2: await asyncio.sleep(10)
        await exchange.close()
    
    tasks = [connect_with_retries(ex_id) for ex_id in EXCHANGES_TO_SCAN]
    await asyncio.gather(*tasks)

async def aggregate_top_movers():
    """تجميع أفضل العملات من حيث حجم التداول من جميع المنصات."""
    all_tickers = []
    logging.info("Aggregating top movers...")
    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            for symbol, data in tickers.items(): data['exchange'] = ex_id
            return list(tickers.values())
        except Exception as e:
            logging.error(f"Could not fetch tickers from {ex_id}: {e}")
            return []

    tasks = [fetch_for_exchange(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()]
    results = await asyncio.gather(*tasks)
    for res in results: all_tickers.extend(res)
    
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and 'USDT' in t['symbol'] and not any(k in t['symbol'] for k in ['UP', 'DOWN', '3L', '3S'])]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    
    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols: unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}
            
    final_list = list(unique_symbols.values())[:bot_data["settings"]['top_n_symbols_by_volume']]
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    logging.info(f"Aggregated top {len(final_list)} markets.")
    return final_list

async def worker(queue, results_list, settings):
    """العامل الذي يقوم بتحليل كل عملة."""
    while not queue.empty():
        try:
            market_info = await queue.get()
            active_strategy_func = STRATEGIES.get(settings['active_strategy'])
            if not active_strategy_func: continue
            
            exchange_id, symbol = market_info['exchange'], market_info['symbol']
            exchange = bot_data["exchanges"].get(exchange_id)
            if not exchange: continue

            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=150)
            if len(ohlcv) >= 50: # Ensure enough data for indicators
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                analysis_result = active_strategy_func(df, settings[settings['active_strategy']])
                if analysis_result:
                    entry_price = df.iloc[-2]['close']
                    signal = {
                        "symbol": symbol, "exchange": exchange_id.capitalize(), "entry_price": entry_price,
                        "take_profit": entry_price * (1 + settings['take_profit_percentage'] / 100),
                        "stop_loss": entry_price * (1 - settings['stop_loss_percentage'] / 100),
                        "timestamp": df.index[-2], "reason": analysis_result['reason']
                    }
                    results_list.append(signal)
            queue.task_done()
        except Exception as e:
            logging.error(f"Error in worker for market {market_info.get('symbol', 'N/A')}: {e}", exc_info=True)
            queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """الدالة الرئيسية التي تقوم ببدء فحص السوق."""
    status = bot_data['status_snapshot']
    status['scan_in_progress'] = True
    status['last_scan_start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status['signals_found'] = 0
    
    settings = bot_data["settings"]
    if settings.get('market_regime_filter_enabled', True) and not await check_market_regime():
        logging.info("Skipping scan due to bearish market conditions.")
        status['scan_in_progress'] = False
        return

    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("No markets to scan.")
        status['scan_in_progress'] = False
        return
        
    logging.info(f"Starting concurrent scan for {len(top_markets)} markets with {settings['concurrent_workers']} workers...")
    queue = asyncio.Queue()
    for market in top_markets: await queue.put(market)
    
    signals = []
    worker_tasks = [asyncio.create_task(worker(queue, signals, settings)) for _ in range(settings['concurrent_workers'])]
    await queue.join()
    for task in worker_tasks: task.cancel()
    
    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        symbol = signal['symbol']
        current_time = time.time()
        # منع إرسال نفس الإشارة بشكل متكرر
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (SCAN_INTERVAL_SECONDS * 4):
            await send_telegram_message(context.bot, signal, is_new=True)
            last_signal_time[symbol] = current_time
            status['signals_found'] += 1
            
    logging.info(f"Concurrent scan complete. Found {status['signals_found']} new signals.")
    status['last_scan_end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status['scan_in_progress'] = False

async def send_telegram_message(bot, signal_data, is_new=False, status_update=None, update_type=None):
    """إرسال الرسائل إلى تليجرام."""
    message = ""
    if is_new:
        message = (
            f"✅ *توصية تداول جديدة* ✅\n\n"
            f"*المنصة:* `{signal_data['exchange']}`\n"
            f"*العملة:* `{signal_data['symbol']}`\n\n"
            f"*سبب الدخول:* `{signal_data['reason']}`\n"
            f"*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n"
            f"🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n"
            f"🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`"
        )
        log_recommendation_to_db(signal_data)
    elif status_update == 'ناجحة':
        profit_percent = (signal_data['exit_price'] / signal_data['entry_price'] - 1) * 100
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*الربح:* `~{profit_percent:.2f}%`"
    elif status_update == 'فاشلة':
        loss_percent = (1 - signal_data['exit_price'] / signal_data['entry_price']) * 100
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*الخسارة:* `~{loss_percent:.2f}%`"
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم تفعيل وقف الخسارة المتحرك عند سعر `${signal_data['stop_loss']:,.4f}`."

    if message:
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logging.error(f"Failed to send Telegram message: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    """تتبع الصفقات المفتوحة وتحديث حالتها."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row # للوصول إلى الأعمدة بالاسم
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'")
    active_trades = [dict(row) for row in cursor.fetchall()]
    conn.close()

    bot_data['status_snapshot']['active_trades_count'] = len(active_trades)
    if not active_trades:
        return
        
    logging.info(f"Tracking {len(active_trades)} active trade(s)...")

    async def check_trade(trade):
        exchange_id = trade['exchange'].lower()
        exchange = bot_data["exchanges"].get(exchange_id)
        if not exchange: return None
        
        try:
            ticker = await exchange.fetch_ticker(trade['symbol'])
            current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None

            # تحقق من جني الأرباح ووقف الخسارة
            if current_price >= trade['take_profit']:
                return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']:
                return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price}
            
            # منطق وقف الخسارة المتحرك
            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade['highest_price'], current_price)
                trailing_sl_active = trade['trailing_sl_active']

                # تفعيل الوقف المتحرك لأول مرة
                if not trailing_sl_active and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price'] # نقل الوقف لنقطة الدخول
                    if new_sl > trade['stop_loss']:
                        return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price, 'tsl_active': True}
                
                # تحديث الوقف المتحرك بعد تفعيله
                elif trailing_sl_active:
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']:
                        return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade['highest_price']: # فقط تحديث أعلى سعر
                         return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}

        except ccxt.NetworkError as e:
            logging.warning(f"Network error checking {trade['symbol']}: {e}")
        except Exception as e:
            logging.error(f"Error checking trade {trade['symbol']}: {e}", exc_info=True)
        return None

    tasks = [check_trade(trade) for trade in active_trades]
    results = await asyncio.gather(*tasks)

    updates_to_db = []
    for result in filter(None, results):
        trade_id, status = result['id'], result['status']
        now_utc = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        
        original_trade = next((t for t in active_trades if t['id'] == trade_id), None)
        if not original_trade: continue
        
        if status in ['ناجحة', 'فاشلة']:
            updates_to_db.append(("UPDATE trades SET status = ?, exit_price = ?, closed_at = ? WHERE id = ?", (status, result['exit_price'], now_utc, trade_id)))
            await send_telegram_message(context.bot, {**original_trade, **result}, status_update=status)
        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss = ?, highest_price = ?, trailing_sl_active = ? WHERE id = ?", (result['new_sl'], result['highest_price'], True, trade_id)))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl':
            updates_to_db.append(("UPDATE trades SET stop_loss = ?, highest_price = ? WHERE id = ?", (result['new_sl'], result['highest_price'], trade_id)))
        elif status == 'update_peak':
            updates_to_db.append(("UPDATE trades SET highest_price = ? WHERE id = ?", (result['highest_price'], trade_id)))

    if updates_to_db:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        for query, params in updates_to_db:
            cursor.execute(query, params)
        conn.commit()
        conn.close()
        logging.info(f"Updated {len(updates_to_db)} trade(s) in the database.")

async def check_market_regime():
    """التحقق من حالة السوق العامة (صاعد/هابط) باستخدام BTC."""
    try:
        binance = bot_data["exchanges"].get('binance')
        if not binance: return True # إذا فشل الاتصال بباينانس، اسمح بالعمل
        
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma50'] = df['close'].rolling(window=50).mean()
        
        is_bullish = df['close'].iloc[-1] > df['sma50'].iloc[-1]
        logging.info(f"Market Regime is {'BULLISH' if is_bullish else 'BEARISH'}.")
        return is_bullish
    except Exception as e:
        logging.error(f"Error in market regime check: {e}", exc_info=True)
        return True # اسمح بالعمل كإجراء وقائي عند حدوث خطأ

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [
    ["📊 الإحصائيات", "ℹ️ مساعدة"],
    ["🔍 فحص يدوي", "⚙️ الإعدادات"],
    ["👀 ماذا يجري في الخلفية؟"]
]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك! استخدم الأزرار للتفاعل.", reply_markup=reply_markup)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)

async def show_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(strategy_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر استراتيجية التداول الجديدة:", reply_markup=reply_markup)

async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params_list = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, dict)])
    await update.message.reply_text(
        f"لتعديل معيار، أرسل رسالة بالصيغة:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير القابلة للتعديل:*\n{params_list}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*مساعدة البوت*\n`🔍 فحص يدوي` - يفحص السوق فوراً.\n`📊 الإحصائيات` - يعرض أداء التوصيات.\n`👀 ماذا يجري في الخلفية؟` - يعرض حالة البوت الحالية.",
        parse_mode=ParseMode.MARKDOWN
    )

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot']['scan_in_progress']:
        await update.message.reply_text("⏳ الفحص الدوري قيد التنفيذ حالياً. يرجى الانتظار حتى ينتهي.")
        return
    await update.message.reply_text("👍 حسناً! سأبدأ الفحص اليدوي للسوق الآن...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص اليدوي.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM trades GROUP BY status")
        stats_data = dict(cursor.fetchall())
        conn.close()

        total = sum(stats_data.values())
        active = stats_data.get('نشطة', 0)
        successful = stats_data.get('ناجحة', 0)
        failed = stats_data.get('فاشلة', 0)
        
        closed_trades = successful + failed
        win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0
        
        stats_message = (
            f"*📊 إحصائيات أداء التوصيات*\n\n"
            f"- *إجمالي التوصيات:* `{total}`\n"
            f"- *النشطة حالياً:* `{active}`\n"
            f"- *الناجحة:* `{successful}` ✅\n"
            f"- *الفاشلة:* `{failed}` ❌\n"
            f"- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`"
        )
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ في عرض الإحصائيات: {e}")
        logging.error(f"Error in stats_command: {e}", exc_info=True)

async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض حالة البوت الحالية."""
    status = bot_data['status_snapshot']
    next_scan_time = "قيد التنفيذ"
    if not status['scan_in_progress'] and context.job_queue:
        next_scan_job = context.job_queue.get_jobs_by_name('perform_scan')
        if next_scan_job:
            run_time = next_scan_job[0].next_t
            if run_time:
                 # تحويل الوقت إلى توقيت محلي (بافتراض أن الخادم يعمل بتوقيت UTC)
                local_time = run_time.astimezone()
                next_scan_time = local_time.strftime('%H:%M:%S')

    message = (
        f"🤖 *حالة البوت في الخلفية*\n\n"
        f"*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n"
        f"- *آخر فحص بدأ في:* `{status['last_scan_start_time']}`\n"
        f"- *آخر فحص انتهى في:* `{status['last_scan_end_time']}`\n"
        f"- *العملات التي تم فحصها:* `{status['markets_found']}`\n"
        f"- *الإشارات الجديدة في آخر فحص:* `{status['signals_found']}`\n"
        f"- *الصفقات النشطة حالياً:* `{status['active_trades_count']}`\n"
        f"- *الفحص التلقائي التالي في:* `{next_scan_time}`"
    )
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الرسائل النصية الرئيسي."""
    text = update.message.text
    # ... (بقية المعالج كما هو)
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)
    elif text == "⚙️ الإعدادات": await show_settings_menu(update, context)
    elif text == "👀 ماذا يجري في الخلفية؟": await background_status_command(update, context)
    elif text == "📈 تغيير الاستراتيجية": await show_strategy_menu(update, context)
    elif text == "🔧 تعديل المعايير": await show_set_parameter_instructions(update, context)
    elif text == "🔙 القائمة الرئيسية": await start_command(update, context)
    elif text == "🚀 الزخم والاندفاع":
        bot_data["settings"]["active_strategy"] = "momentum_breakout"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الزخم والاندفاع`.")
        await show_settings_menu(update, context)
    elif text == "🔄 الارتداد من الدعم":
        bot_data["settings"]["active_strategy"] = "mean_reversion"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الارتداد من الدعم`.")
        await show_settings_menu(update, context)
    elif text == "🔙 قائمة الإعدادات":
        await show_settings_menu(update, context)
    elif re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text):
        match = re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text)
        param, value_str = match.groups()
        settings = bot_data["settings"]
        if param in settings and not isinstance(settings[param], dict):
            try:
                current_value = settings[param]
                if isinstance(current_value, bool): new_value = value_str.lower() in ['true', '1', 'yes', 'on']
                elif isinstance(current_value, int): new_value = int(value_str)
                elif isinstance(current_value, float): new_value = float(value_str)
                else: new_value = value_str
                settings[param] = new_value
                save_settings()
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_value}`.")
            except ValueError:
                await update.message.reply_text(f"❌ قيمة غير صالحة.")
        else:
            await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود.")

# --- دوال دورة حياة البوت --- #
async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت وقبل بدء التشغيل."""
    await asyncio.sleep(2) # انتظر قليلاً
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.critical("CRITICAL: Failed to connect to any exchange. Bot cannot run.")
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="❌ فشل البوت في الاتصال بأي منصة. سيتم إيقاف التشغيل.")
        # لا نستخدم application.stop() هنا لأنها قد لا تعمل بشكل جيد في كل البيئات
        return

    # جدولة المهام المتكررة
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')

    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت التداول الاحترافي جاهز للعمل! (نسخة مطورة)*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية:* `{bot_data['settings']['active_strategy']}`",
        parse_mode=ParseMode.MARKDOWN
    )
    logging.info("Bot initialization complete and jobs scheduled.")

async def post_shutdown(application: Application):
    """دالة تعمل عند إيقاف البوت لضمان الإغلاق النظيف."""
    logging.info("Closing all exchange connections...")
    tasks = [ex.close() for ex in bot_data["exchanges"].values()]
    await asyncio.gather(*tasks)
    logging.info("Connections closed successfully.")

def main():
    """الدالة الرئيسية المنظمة للتشغيل."""
    print("🚀 Starting Professional Trading Bot (Upgraded Version)...")
    load_settings()
    init_database()
    
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.critical(f"Bot stopped due to a critical error in __main__: {e}", exc_info=True)

