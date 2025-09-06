# -*- coding: utf-8 -*-

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

## --- الإعدادات --- ##

# 1. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("FATAL ERROR: Missing Telegram environment variables.")
    exit()

# 2. إعدادات استراتيجية التداول ومسح السوق
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900  # 15 minutes for finding new trades
TRACK_INTERVAL_SECONDS = 120 # (جديد) 2 minutes for tracking open trades
TOP_N_SYMBOLS_BY_VOLUME = 150
PERFORMANCE_FILE = 'recommendations_log.csv'

# 3. معايير الاستراتيجية المتقدمة
VWAP_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STDDEV = 2.0
RSI_PERIOD = 14
RSI_MAX_LEVEL = 68

# 4. إدارة المخاطر
TAKE_PROFIT_PERCENTAGE = 4.0
STOP_LOSS_PERCENTAGE = 2.0

# --- تهيئة ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {"exchanges": {}, "last_signal_time": {}}

## --- الدوال الأساسية --- ##

async def initialize_exchanges():
    """تهيئة الاتصال بكل المنصات بشكل متزامن."""
    exchange_ids = EXCHANGES_TO_SCAN
    exchange_instances = {ex_id: getattr(ccxt, ex_id)({'enableRateLimit': True}) for ex_id in exchange_ids}
    
    async def load_markets_safe(ex_id, exchange):
        try:
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logging.info(f"Connected to {ex_id}")
            return exchange
        except Exception as e:
            logging.error(f"Failed to connect to {ex_id}: {e}")
            await exchange.close()
            return None

    tasks = [load_markets_safe(ex_id, ex) for ex_id, ex in exchange_instances.items()]
    await asyncio.gather(*tasks)

async def aggregate_top_movers():
    """تجميع أفضل العملات من كل المنصات المتاحة."""
    all_tickers = []
    # (الكود الداخلي لهذه الدالة لم يتغير)
    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            for symbol, ticker_data in tickers.items(): ticker_data['exchange'] = ex_id
            return list(tickers.values())
        except Exception as e:
            logging.warning(f"Could not fetch tickers from {ex_id}: {e}")
            return []
    tasks = [fetch_for_exchange(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()]
    results = await asyncio.gather(*tasks)
    for res in results: all_tickers.extend(res)
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].endswith('/USDT')]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols: unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}
    final_list = list(unique_symbols.values())[:TOP_N_SYMBOLS_BY_VOLUME]
    logging.info(f"Aggregated top {len(final_list)} unique markets.")
    return final_list

def analyze_market_data(df, symbol):
    """تحليل البيانات."""
    # (الكود الداخلي لهذه الدالة لم يتغير)
    if df is None or len(df) < BBANDS_PERIOD: return None
    try:
        df.ta.vwap(append=True); df.ta.bbands(append=True); df.ta.macd(append=True); df.ta.rsi(append=True)
        required = [f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}', f'VWAP_D', f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'RSI_{RSI_PERIOD}']
        if not all(col in df.columns for col in required): return None
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] <= prev[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and 
            last[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] > last[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and
            last['close'] > last[f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}'] and
            last['close'] > last[f'VWAP_D'] and
            last[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL):
            entry = last['close']
            return {"symbol": symbol, "entry_price": entry, "take_profit": entry*(1+TAKE_PROFIT_PERCENTAGE/100), "stop_loss": entry*(1-STOP_LOSS_PERCENTAGE/100), "timestamp": df.index[-2], "reason": "MACD Crossover & Bollinger Breakout"}
    except Exception: return None
    return None

async def fetch_and_analyze(market_info):
    """جلب وتحليل لعملة واحدة."""
    # (الكود الداخلي لهذه الدالة لم يتغير)
    exchange_id, symbol = market_info['exchange'], market_info['symbol']
    exchange = bot_data["exchanges"].get(exchange_id)
    if not exchange: return None
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=150)
        if len(ohlcv) < BBANDS_PERIOD: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        signal = analyze_market_data(df, symbol)
        if signal:
            signal['exchange'] = exchange_id.capitalize()
            return signal
    except Exception: return None
    return None

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ فحص متوازي للبحث عن فرص جديدة."""
    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("No markets to scan.")
        return
    logging.info(f"Starting parallel scan for {len(top_markets)} active markets...")
    tasks = [fetch_and_analyze(market) for market in top_markets]
    results = await asyncio.gather(*tasks)
    signals = [res for res in results if res is not None]
    found_signals = 0
    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        symbol = signal['symbol']
        current_time = time.time()
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (SCAN_INTERVAL_SECONDS * 4):
            await send_telegram_message(context.bot, signal, is_new=True)
            last_signal_time[symbol] = current_time
            found_signals += 1
    logging.info(f"Parallel scan complete. Found {found_signals} new signals.")

def log_recommendation(signal):
    """تسجيل التوصية في ملف CSV."""
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    log_entry = {'timestamp': signal['timestamp'], 'exchange': signal['exchange'], 'symbol': signal['symbol'], 'entry_price': signal['entry_price'], 'take_profit': signal['take_profit'], 'stop_loss': signal['stop_loss'], 'status': 'نشطة', 'exit_price': 'N/A', 'closed_at': 'N/A'}
    df = pd.DataFrame([log_entry])
    with open(PERFORMANCE_FILE, 'a', newline='', encoding='utf-8-sig') as f:
        df.to_csv(f, header=not file_exists, index=False)

async def send_telegram_message(bot, signal_data, is_new=False, status=None):
    """إرسال كل أنواع الرسائل (توصية جديدة أو تحديث حالة)."""
    if is_new:
        message = f"✅ *توصية تداول جديدة* ✅\n\n*المنصة:* `{signal_data['exchange']}`\n*العملة:* `{signal_data['symbol']}`\n\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`"
        log_recommendation(signal_data)
    elif status == 'ناجحة':
        profit_percent = (signal_data['exit_price'] / signal_data['entry_price'] - 1) * 100
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n*سعر الإغلاق:* `${signal_data['exit_price']:,.4f}`\n*الربح:* `~{profit_percent:.2f}%`"
    elif status == 'فاشلة':
        loss_percent = (signal_data['entry_price'] / signal_data['exit_price'] - 1) * 100
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n*سعر الإغلاق:* `${signal_data['exit_price']:,.4f}`\n*الخسارة:* `~{loss_percent:.2f}%`"
    else: return
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"Message sent for {signal_data['symbol']}. New Status: {status or 'new'}")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    """(جديد) المهمة الخلفية لمتابعة الصفقات النشطة."""
    if not os.path.exists(PERFORMANCE_FILE): return
    
    df = pd.read_csv(PERFORMANCE_FILE)
    active_trades = df[df['status'] == 'نشطة'].copy()
    if active_trades.empty: return

    logging.info(f"Tracking {len(active_trades)} active trade(s)...")
    file_was_updated = False

    for index, trade in active_trades.iterrows():
        exchange_id = trade['exchange'].lower()
        exchange = bot_data["exchanges"].get(exchange_id)
        if not exchange: continue

        try:
            ticker = await exchange.fetch_ticker(trade['symbol'])
            current_price = ticker.get('last') or ticker.get('close')
            if not current_price: continue

            new_status = None
            if current_price >= trade['take_profit']:
                new_status = 'ناجحة'
            elif current_price <= trade['stop_loss']:
                new_status = 'فاشلة'

            if new_status:
                df.loc[index, 'status'] = new_status
                df.loc[index, 'exit_price'] = current_price
                df.loc[index, 'closed_at'] = pd.to_datetime('now', utc=True)
                file_was_updated = True
                
                # إرسال إشعار بالتحديث
                updated_trade_details = df.loc[index].to_dict()
                await send_telegram_message(context.bot, updated_trade_details, is_new=False, status=new_status)
        
        except Exception as e:
            logging.warning(f"Could not track {trade['symbol']} on {exchange_id}: {e}")
            continue

    if file_was_updated:
        df.to_csv(PERFORMANCE_FILE, index=False, encoding='utf-8-sig')
        logging.info("Performance file updated with closed trades.")


## --- أوامر ومعالجات تليجرام --- ##

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك! أنا بوت التداول الاحترافي. جاهز للعمل.", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*مساعدة البوت*\n`🔍 فحص يدوي` - يفحص أفضل 150 عملة.\n`📊 الإحصائيات` - يعرض أداء التوصيات.\n`ℹ️ مساعدة` - يعرض هذه الرسالة.", parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👍 حسناً! جاري بدء فحص متوازي للسوق...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(مُحدّثة) عرض إحصائيات الأداء الكاملة."""
    if not os.path.exists(PERFORMANCE_FILE):
        await update.message.reply_text("لم يتم تسجيل أي توصيات بعد.")
        return
    try:
        df = pd.read_csv(PERFORMANCE_FILE)
        if df.empty:
            await update.message.reply_text("ملف الإحصائيات فارغ.")
            return
        
        total = len(df)
        active = len(df[df['status'] == 'نشطة'])
        successful = len(df[df['status'] == 'ناجحة'])
        failed = len(df[df['status'] == 'فاشلة'])
        closed_trades = successful + failed
        win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0
        
        stats_message = f"""
*📊 إحصائيات أداء التوصيات*

- *إجمالي التوصيات:* `{total}`
- *الصفقات النشطة:* `{active}`
- *الصفقات الناجحة:* `{successful}` ✅
- *الصفقات الفاشلة:* `{failed}` ❌
- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`
"""
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ: {e}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت مباشرة."""
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.critical("CRITICAL: Failed to connect to any exchange. Bot cannot run.")
        return
    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *بوت التداول الاحترافي جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية:* `الفحص المتوازي + متابعة الصفقات`", parse_mode=ParseMode.MARKDOWN)
    
    # جدولة المهام
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=15) # إضافة مهمة المتابعة

async def post_shutdown(application: Application):
    """إغلاق الاتصالات بأمان."""
    logging.info("Closing all exchange connections...")
    for exchange in bot_data["exchanges"].values():
        await exchange.close()
    logging.info("Connections closed successfully.")

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 Starting Professional Trading Bot...")
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

