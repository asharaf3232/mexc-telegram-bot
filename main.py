# -*- coding: utf-8 -*-

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

## --- الإعدادات --- ##

# 1. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("خطأ فادح: متغيرات بيئة تليجرام غير موجودة.")
    exit()

# 2. إعدادات استراتيجية التداول ومسح السوق
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 900  # 15 دقيقة
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

## --- الدوال الأساسية (مع تعديلات هامة) --- ##

async def initialize_exchanges():
    """(مُحسّنة) تهيئة الاتصال بكل المنصات بشكل متزامن."""
    exchange_ids = EXCHANGES_TO_SCAN
    # Create exchange instances
    exchange_instances = {ex_id: getattr(ccxt, ex_id)({'enableRateLimit': True}) for ex_id in exchange_ids}
    
    async def load_markets_safe(ex_id, exchange):
        try:
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logging.info(f"تم الاتصال بنجاح بمنصة {ex_id}")
            return exchange
        except Exception as e:
            logging.error(f"فشل الاتصال بمنصة {ex_id}: {e}")
            await exchange.close() # Close failed connections
            return None

    tasks = [load_markets_safe(ex_id, ex) for ex_id, ex in exchange_instances.items()]
    await asyncio.gather(*tasks)


async def aggregate_top_movers():
    """(جديد ومُحصّن) تجميع أفضل العملات من كل المنصات المتاحة."""
    all_tickers = []
    logging.info("بدء تجميع العملات النشطة من كل المنصات...")

    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            for symbol, ticker_data in tickers.items():
                ticker_data['exchange'] = ex_id
            return list(tickers.values())
        except Exception as e:
            logging.warning(f"لم يتمكن من جلب البيانات من {ex_id}: {e}")
            return []

    tasks = [fetch_for_exchange(ex_id, ex_instance) for ex_id, ex_instance in bot_data["exchanges"].items()]
    results = await asyncio.gather(*tasks)

    for ticker_list in results:
        all_tickers.extend(ticker_list)

    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].endswith('/USDT')]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    
    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols:
            unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}

    final_list = list(unique_symbols.values())[:TOP_N_SYMBOLS_BY_VOLUME]
    logging.info(f"تم تجميع أفضل {len(final_list)} عملة فريدة من كل المنصات.")
    return final_list

def analyze_market_data(df, symbol):
    """تحليل البيانات."""
    if df is None or len(df) < BBANDS_PERIOD: return None
    try:
        df.ta.vwap(length=VWAP_PERIOD, append=True)
        df.ta.bbands(length=BBANDS_PERIOD, std=BBANDS_STDDEV, append=True)
        df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)
        
        required_columns = [f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}', f'VWAP_{VWAP_PERIOD}', f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}', f'RSI_{RSI_PERIOD}']
        if not all(col in df.columns for col in required_columns): return None

        last, prev = df.iloc[-2], df.iloc[-3]

        macd_crossover = prev[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] <= prev[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and last[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] > last[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
        bollinger_breakout = last['close'] > last[f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}']
        vwap_confirmation = last['close'] > last[f'VWAP_{VWAP_PERIOD}']
        rsi_condition = last[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL

        if macd_crossover and bollinger_breakout and vwap_confirmation and rsi_condition:
            entry_price = last['close']
            return {
                "symbol": symbol, "entry_price": entry_price,
                "take_profit": entry_price * (1 + TAKE_PROFIT_PERCENTAGE / 100),
                "stop_loss": entry_price * (1 - STOP_LOSS_PERCENTAGE / 100),
                "timestamp": df.index[-2], "reason": "تقاطع MACD واختراق Bollinger"
            }
    except Exception as e:
        logging.error(f"خطأ عام في تحليل {symbol}: {e}")
    return None

async def fetch_and_analyze(market_info):
    """(جديد) دالة تجمع بين جلب البيانات وتحليلها لعملة واحدة."""
    exchange_id = market_info['exchange']
    symbol = market_info['symbol']
    exchange = bot_data["exchanges"].get(exchange_id)

    if not exchange:
        return None

    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=150)
        if len(ohlcv) < BBANDS_PERIOD:
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        signal = analyze_market_data(df, symbol)
        if signal:
            signal['exchange'] = exchange_id.capitalize()
            return signal
    except Exception as e:
        logging.warning(f"حدث خطأ أثناء معالجة {symbol} على {exchange_id}: {e}")
        return None
    return None

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """(مُطورة) تنفيذ فحص متوازي كامل للسوق."""
    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("لا توجد أسواق لفحصها في هذه الجولة.")
        return

    logging.info(f"بدء فحص متوازي لـ {len(top_markets)} عملة نشطة...")
    
    # إنشاء وتشغيل كل مهام الفحص والتحليل في نفس الوقت
    tasks = [fetch_and_analyze(market) for market in top_markets]
    results = await asyncio.gather(*tasks)

    # فلترة النتائج للحصول على الإشارات فقط
    signals = [res for res in results if res is not None]
    
    found_signals = 0
    last_signal_time = bot_data['last_signal_time']
    
    for signal in signals:
        symbol = signal['symbol']
        current_time = time.time()
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 4):
            await send_telegram_message(context.bot, signal)
            last_signal_time[symbol] = current_time
            found_signals += 1
            
    logging.info(f"اكتمل الفحص المتوازي. تم العثور على {found_signals} إشارة جديدة.")


async def send_telegram_message(bot, signal):
    """إرسال التوصية مع ذكر اسم المنصة."""
    message = f"✅ *توصية تداول جديدة* ✅\n\n*المنصة:* `{signal['exchange']}`\n*العملة:* `{signal['symbol']}`\n*الاستراتيجية:* `{signal['reason']}`\n\n*الإجراء:* `شراء (BUY)`\n*سعر الدخول:* `${signal['entry_price']:,.4f}`\n\n🎯 *جني الأرباح ({TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.4f}`\n🛑 *وقف الخسارة ({STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.4f}`\n\n*إخلاء مسؤولية: التداول عالي المخاطر.*"
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"تم إرسال التوصية بنجاح لعملة {signal['symbol']}.")
        # log_recommendation(signal) # You need to define this function if you want to log
    except Exception as e:
        logging.error(f"فشل إرسال الرسالة إلى تليجرام: {e}")

## --- أوامر ومعالجات تليجرام (لا تغيير) --- ##
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك! أنا بوت التداول فائق السرعة. استخدم الأزرار للتفاعل.", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "*مساعدة البوت*\n`🔍 فحص يدوي` - يفحص أفضل 150 عملة فوراً.\n`📊 الإحصائيات` - يعرض أداء التوصيات.\n`ℹ️ مساعدة` - يعرض هذه الرسالة."
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👍 حسناً! جاري بدء فحص متوازي فائق السرعة للسوق...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ميزة الإحصائيات قيد التطوير.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت مباشرة."""
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.error("فشل الاتصال بجميع المنصات. سيتم إيقاف البوت.")
        # Cleanly close any remaining exchange connections
        for ex in bot_data["exchanges"].values():
            await ex.close()
        return

    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت المنصات المتعددة جاهز للعمل!*\n- *المنصات المتصلة:* `{exchange_names}`\n- *الاستراتيجية:* `الفحص المتوازي فائق السرعة`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=LOOP_INTERVAL_SECONDS, first=10)

async def post_shutdown(application: Application):
    """(جديد) دالة لإغلاق الاتصالات بأمان عند إيقاف البوت."""
    logging.info("يتم إغلاق جميع اتصالات المنصات...")
    for exchange in bot_data["exchanges"].values():
        await exchange.close()
    logging.info("تم إغلاق الاتصالات بنجاح.")


## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 جاري بدء تشغيل البوت...")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown) # إضافة دالة الإغلاق
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print("✅ البوت يعمل الآن ويستمع للتحديثات...")
    application.run_polling()

