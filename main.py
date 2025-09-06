# -*- coding: utf-8 -*-

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import time
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
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120
TOP_N_SYMBOLS_BY_VOLUME = 150
PERFORMANCE_FILE = 'recommendations_log.csv'
CONCURRENT_WORKERS = 10

# 3. (جديد) إعدادات فلتر حالة السوق
MARKET_REGIME_SYMBOL = 'BTC/USDT'
MARKET_REGIME_TIMEFRAME = '4h'
MARKET_REGIME_SMA_PERIOD = 50

# 4. معايير الاستراتيجية المتقدمة
VWAP_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STDDEV = 2.0
RSI_PERIOD = 14
RSI_MAX_LEVEL = 68

# 5. إدارة المخاطر
TAKE_PROFIT_PERCENTAGE = 4.0
STOP_LOSS_PERCENTAGE = 2.0
# (جديد) إعدادات وقف الخسارة المتحرك
TRAILING_STOP_LOSS_ACTIVATE_PERCENT = 2.0 # تفعيل الوقف المتحرك عند ربح
TRAILING_STOP_LOSS_PERCENT = 1.5 # المسافة التي يتبعها السعر

# --- تهيئة ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {"exchanges": {}, "last_signal_time": {}}

## --- الدوال الأساسية --- ##

async def initialize_exchanges():
    """تهيئة الاتصال بكل المنصات."""
    # (الكود الداخلي لهذه الدالة لم يتغير)
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
            await exchange.close(); return None
    tasks = [load_markets_safe(ex_id, ex) for ex_id, ex in exchange_instances.items()]
    await asyncio.gather(*tasks)

async def check_market_regime():
    """(جديد) التحقق من حالة السوق العامة باستخدام البيتكوين."""
    try:
        binance = bot_data["exchanges"].get('binance')
        if not binance:
            logging.warning("Binance not available for market regime check. Assuming market is bullish.")
            return True # الافتراض الإيجابي في حالة عدم توفر بينانس
        
        ohlcv = await binance.fetch_ohlcv(MARKET_REGIME_SYMBOL, MARKET_REGIME_TIMEFRAME, limit=MARKET_REGIME_SMA_PERIOD + 5)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # حساب SMA
        df[f'SMA_{MARKET_REGIME_SMA_PERIOD}'] = df['close'].rolling(window=MARKET_REGIME_SMA_PERIOD).mean()
        
        last_close = df['close'].iloc[-1]
        last_sma = df[f'SMA_{MARKET_REGIME_SMA_PERIOD}'].iloc[-1]
        
        is_bullish = last_close > last_sma
        logging.info(f"Market Regime Check (BTC 4h): Close={last_close:.2f}, SMA({MARKET_REGIME_SMA_PERIOD})={last_sma:.2f}. Market is {'BULLISH' if is_bullish else 'BEARISH'}.")
        return is_bullish
    except Exception as e:
        logging.error(f"Error in market regime check: {e}")
        return True # السماح بالمرور في حالة حدوث خطأ

async def aggregate_top_movers():
    """تجميع أفضل العملات من كل المنصات."""
    # (الكود الداخلي لهذه الدالة لم يتغير)
    all_tickers = []
    logging.info("Aggregating top movers from all exchanges...")
    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            for symbol, data in tickers.items(): data['exchange'] = ex_id
            return list(tickers.values())
        except Exception: return []
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
        required_cols = [f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}', f'VWAP_D', f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'RSI_{RSI_PERIOD}']
        if not all(col in df.columns for col in required_cols): return None
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] <= prev[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and last[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] > last[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and last['close'] > last[f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}'] and last['close'] > last[f'VWAP_D'] and last[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL):
            entry_price = last['close']
            return {"symbol": symbol, "entry_price": entry_price, "take_profit": entry_price*(1+TAKE_PROFIT_PERCENTAGE/100), "stop_loss": entry_price*(1-STOP_LOSS_PERCENTAGE/100), "timestamp": df.index[-2], "reason": "MACD Crossover & Bollinger Breakout"}
    except Exception: return None
    return None

async def worker(queue, results_list):
    """العامل الذي يأخذ المهام من الطابور وينفذها."""
    # (الكود الداخلي لهذه الدالة لم يتغير)
    while not queue.empty():
        try:
            market_info = await queue.get()
            exchange_id, symbol = market_info['exchange'], market_info['symbol']
            exchange = bot_data["exchanges"].get(exchange_id)
            if not exchange: continue
            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=150)
            if len(ohlcv) >= BBANDS_PERIOD:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                signal = analyze_market_data(df, symbol)
                if signal:
                    signal['exchange'] = exchange_id.capitalize()
                    results_list.append(signal)
            queue.task_done()
        except Exception: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """(مُطورة) تنفيذ فحص منظم بعد التحقق من حالة السوق."""
    is_market_bullish = await check_market_regime()
    if not is_market_bullish:
        logging.info("Skipping scan due to bearish market conditions.")
        return

    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("No markets to scan.")
        return

    logging.info(f"Starting concurrent scan for {len(top_markets)} markets with {CONCURRENT_WORKERS} workers...")
    
    queue = asyncio.Queue()
    for market in top_markets: await queue.put(market)
        
    signals = []
    worker_tasks = [asyncio.create_task(worker(queue, signals)) for _ in range(CONCURRENT_WORKERS)]
    await queue.join()
    for task in worker_tasks: task.cancel()

    found_signals = 0
    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        symbol = signal['symbol']
        current_time = time.time()
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (SCAN_INTERVAL_SECONDS * 4):
            await send_telegram_message(context.bot, signal, is_new=True)
            last_signal_time[symbol] = current_time
            found_signals += 1
            
    logging.info(f"Concurrent scan complete. Found {found_signals} new signals.")

def log_recommendation(signal):
    """تسجيل التوصية في ملف CSV مع بيانات وقف الخسارة المتحرك."""
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    log_entry = {
        'timestamp': signal['timestamp'], 'exchange': signal['exchange'], 'symbol': signal['symbol'],
        'entry_price': signal['entry_price'], 'take_profit': signal['take_profit'],
        'stop_loss': signal['stop_loss'], 'status': 'نشطة', 'exit_price': 'N/A', 'closed_at': 'N/A',
        'trailing_sl_active': False, 'highest_price': signal['entry_price']
    }
    df = pd.DataFrame([log_entry])
    with open(PERFORMANCE_FILE, 'a', newline='', encoding='utf-8-sig') as f:
        df.to_csv(f, header=not file_exists, index=False)

async def send_telegram_message(bot, signal_data, is_new=False, status=None, update_type=None):
    """إرسال كل أنواع الرسائل."""
    message = ""
    if is_new:
        message = f"✅ *توصية تداول جديدة* ✅\n\n*المنصة:* `{signal_data['exchange']}`\n*العملة:* `{signal_data['symbol']}`\n\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`"
        log_recommendation(signal_data)
    elif status == 'ناجحة':
        profit_percent = (signal_data['exit_price'] / signal_data['entry_price'] - 1) * 100
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n*الربح:* `~{profit_percent:.2f}%`"
    elif status == 'فاشلة':
        loss_percent = (1 - signal_data['exit_price'] / signal_data['entry_price']) * 100
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n*الخسارة:* `~{loss_percent:.2f}%`"
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم تفعيل وقف الخسارة المتحرك عند سعر `${signal_data['stop_loss']:,.4f}`."
    
    if message:
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logging.error(f"Failed to send Telegram message: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    """(مُطورة) متابعة الصفقات النشطة مع وقف خسارة متحرك."""
    if not os.path.exists(PERFORMANCE_FILE): return
    
    df = pd.read_csv(PERFORMANCE_FILE)
    active_trades_df = df[df['status'] == 'نشطة'].copy()
    if active_trades_df.empty: return

    logging.info(f"Tracking {len(active_trades_df)} active trade(s)...")
    
    async def check_trade(index, trade):
        exchange_id = trade['exchange'].lower()
        exchange = bot_data["exchanges"].get(exchange_id)
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol'])
            current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None

            # --- منطق تحديد نتيجة الصفقة ---
            if current_price >= trade['take_profit']:
                return {'index': index, 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']:
                return {'index': index, 'status': 'فاشلة', 'exit_price': current_price}
            
            # --- (جديد) منطق وقف الخسارة المتحرك ---
            highest_price = max(trade.get('highest_price', trade['entry_price']), current_price)
            trailing_sl_active = trade.get('trailing_sl_active', False)
            
            # تفعيل الوقف المتحرك لأول مرة
            if not trailing_sl_active and current_price >= trade['entry_price'] * (1 + TRAILING_STOP_LOSS_ACTIVATE_PERCENT / 100):
                new_sl = trade['entry_price'] * (1 + (TRAILING_STOP_LOSS_ACTIVATE_PERCENT - TRAILING_STOP_LOSS_PERCENT) / 100)
                if new_sl > trade['stop_loss']:
                    return {'index': index, 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price, 'tsl_active': True}

            # تحديث الوقف المتحرك للصفقات المفعلة
            elif trailing_sl_active:
                new_sl = highest_price * (1 - TRAILING_STOP_LOSS_PERCENT / 100)
                if new_sl > trade['stop_loss']:
                    return {'index': index, 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                # تحديث أعلى سعر فقط إذا لم يتم تحديث الوقف
                elif highest_price > trade['highest_price']:
                    return {'index': index, 'status': 'update_peak', 'highest_price': highest_price}

        except Exception: pass
        return None

    tasks = [check_trade(index, trade) for index, trade in active_trades_df.iterrows()]
    results = await asyncio.gather(*tasks)

    file_was_updated = False
    for result in filter(None, results):
        index = result['index']
        status = result['status']
        
        if status in ['ناجحة', 'فاشلة']:
            df.loc[index, 'status'] = status
            df.loc[index, 'exit_price'] = result['exit_price']
            df.loc[index, 'closed_at'] = pd.to_datetime('now', utc=True)
            await send_telegram_message(context.bot, df.loc[index].to_dict(), status=status)
            file_was_updated = True
        elif status == 'update_tsl':
            df.loc[index, 'stop_loss'] = result['new_sl']
            df.loc[index, 'highest_price'] = result['highest_price']
            df.loc[index, 'trailing_sl_active'] = True
            await send_telegram_message(context.bot, df.loc[index].to_dict(), update_type='tsl_activation')
            file_was_updated = True
        elif status == 'update_sl':
            df.loc[index, 'stop_loss'] = result['new_sl']
            df.loc[index, 'highest_price'] = result['highest_price']
            file_was_updated = True
        elif status == 'update_peak':
            df.loc[index, 'highest_price'] = result['highest_price']
            file_was_updated = True

    if file_was_updated:
        df.to_csv(PERFORMANCE_FILE, index=False, encoding='utf-8-sig')
        logging.info("Performance file updated.")

## --- أوامر ومعالجات تليجرام --- ##

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك! أنا بوت التداول العبقري. جاهز للعمل.", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*مساعدة البوت*\n`🔍 فحص يدوي` - يفحص السوق إذا كانت الظروف مواتية.\n`📊 الإحصائيات` - يعرض أداء التوصيات.\n`ℹ️ مساعدة` - يعرض هذه الرسالة.", parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👍 حسناً! جاري التحقق من حالة السوق أولاً...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض إحصائيات الأداء الكاملة."""
    if not os.path.exists(PERFORMANCE_FILE):
        await update.message.reply_text("لم يتم تسجيل أي توصيات بعد."); return
    try:
        df = pd.read_csv(PERFORMANCE_FILE)
        if df.empty:
            await update.message.reply_text("ملف الإحصائيات فارغ."); return
        
        total, active, successful, failed = len(df), len(df[df['status'] == 'نشطة']), len(df[df['status'] == 'ناجحة']), len(df[df['status'] == 'فاشلة'])
        closed_trades = successful + failed
        win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0
        
        stats_message = f"""*📊 إحصائيات أداء التوصيات*\n\n- *إجمالي التوصيات:* `{total}`\n- *النشطة حالياً:* `{active}`\n- *الناجحة:* `{successful}` ✅\n- *الفاشلة:* `{failed}` ❌\n- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`"""
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
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت التداول العبقري جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية:* `فلتر السوق + وقف متحرك`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20)

async def post_shutdown(application: Application):
    """إغلاق الاتصالات بأمان."""
    logging.info("Closing all exchange connections...")
    for exchange in bot_data["exchanges"].values():
        await exchange.close()
    logging.info("Connections closed successfully.")

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 Starting Genius Trading Bot...")
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

