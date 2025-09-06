# -*- coding: utf-8 -*-

import ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os
import logging
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

## --- CONFIGURATION --- ##

# 1. مفاتيح منصة MEXC
MEXC_API_KEY = os.getenv('MEXC_API_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_API_SECRET') # <-- تم تعديلها في الدرس السابق

# 2. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# التأكد من وجود كل المتغيرات الضرورية
if not all([MEXC_API_KEY, MEXC_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة غير موجود.")
    exit()

# 3. إعدادات استراتيجية التحليل
SYMBOLS_TO_WATCH = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 300  # الفاصل الزمني للفحص التلقائي

# 4. معايير الاستراتيجية الكمية
VOLUME_SPIKE_FACTOR = 3.0
EMA_FAST_PERIOD = 10
EMA_SLOW_PERIOD = 25
RSI_PERIOD = 14
RSI_MAX_LEVEL = 65

# 5. إعدادات إدارة المخاطر
TAKE_PROFIT_PERCENTAGE = 3.0
STOP_LOSS_PERCENTAGE = 1.5

# --- إعدادات إضافية ---
last_signal_time = {}
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

## --- FUNCTIONS --- ##

def get_exchange_client():
    """يقوم بإعداد وتجهيز الاتصال بمنصة MEXC."""
    try:
        exchange = ccxt.mexc({
            'apiKey': MEXC_API_KEY,
            'secret': MEXC_SECRET_KEY,
            'options': {'defaultType': 'spot'},
        })
        logging.info("✅ تم الاتصال بنجاح بمنصة MEXC.")
        return exchange
    except Exception as e:
        logging.error(f"❌ فشل الاتصال بمنصة MEXC: {e}")
        return None

def fetch_data(exchange, symbol, timeframe):
    """يجلب بيانات الشموع التاريخية لعملة معينة."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.warning(f"⚠️ حدث خطأ أثناء جلب البيانات لعملة {symbol}: {e}")
        return None

def analyze_market_data(df, symbol):
    """يطبق الاستراتيجية على البيانات ويقرر ما إذا كانت هناك فرصة للشراء."""
    if df is None or len(df) < EMA_SLOW_PERIOD:
        return None
    try:
        df.ta.ema(length=EMA_FAST_PERIOD, append=True)
        df.ta.ema(length=EMA_SLOW_PERIOD, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)
        df['volume_sma'] = df['volume'].rolling(window=EMA_SLOW_PERIOD).mean()
        last_row = df.iloc[-2]
        previous_row = df.iloc[-3]
        volume_condition = last_row['volume'] > last_row['volume_sma'] * VOLUME_SPIKE_FACTOR
        crossover_condition = previous_row[f'EMA_{EMA_FAST_PERIOD}'] <= previous_row[f'EMA_{EMA_SLOW_PERIOD}'] and last_row[f'EMA_{EMA_FAST_PERIOD}'] > last_row[f'EMA_{EMA_SLOW_PERIOD}']
        rsi_condition = last_row[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL
        price_condition = last_row['close'] > last_row[f'EMA_{EMA_FAST_PERIOD}']
        if volume_condition and crossover_condition and rsi_condition and price_condition:
            entry_price = last_row['close']
            signal = {
                "symbol": symbol,
                "entry_price": entry_price,
                "take_profit": entry_price * (1 + TAKE_PROFIT_PERCENTAGE / 100),
                "stop_loss": entry_price * (1 - STOP_LOSS_PERCENTAGE / 100),
                "timestamp": last_row['timestamp']
            }
            logging.info(f"💡 تم العثور على إشارة شراء محتملة لعملة {symbol}!")
            return signal
    except Exception as e:
        logging.error(f"❌ خطأ أثناء تحليل بيانات {symbol}: {e}")
    return None

async def send_telegram_message(bot: Bot, signal):
    """يقوم بتنسيق رسالة التوصية وإرسالها إلى تليجرام."""
    message = f"""
🔔 *توصية جديدة* 🔔

*العملة:* `{signal['symbol']}`
*نوع العملية:* `شراء (BUY)`

*سعر الدخول المقترح:* `${signal['entry_price']:,.4f}`

🎯 *الهدف (ربح {TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.4f}`
🛑 *وقف الخسارة (خسارة {STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.4f}`

*تحذير: التداول ينطوي على مخاطر عالية.*
"""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"✅ تم إرسال التوصية بنجاح إلى تليجرام.")
    except Exception as e:
        logging.error(f"❌ فشل إرسال الرسالة إلى تليجرام: {e}")

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """الدالة الأساسية التي تقوم بجولة فحص واحدة للسوق."""
    exchange = context.bot_data['exchange']
    found_signals = 0
    
    logging.info("▶️  بدء جولة فحص للسوق...")
    
    for symbol in SYMBOLS_TO_WATCH:
        df = fetch_data(exchange, symbol, TIMEFRAME)
        if df is not None:
            signal = analyze_market_data(df, symbol)
            if signal:
                current_time = time.time()
                if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 2):
                    await send_telegram_message(context.bot, signal)
                    last_signal_time[symbol] = current_time
                    found_signals += 1
                else:
                    logging.info(f"ℹ️ تم تجاهل إشارة مكررة لعملة {symbol}.")
    
    logging.info("⏹️  انتهاء جولة الفحص.")
    return found_signals

# --- NEW: Command Handlers & Jobs ---

async def start_command(update, context):
    """إرسال رسالة ترحيبية عند بدء المحادثة مع البوت."""
    await update.message.reply_text("أهلاً بك! أنا بوت تحليل السوق. أعمل تلقائياً في الخلفية. يمكنك استخدام الأمر /scan لطلب فحص يدوي فوري.")

async def manual_scan_command(update, context):
    """المشغل الخاص بالفحص اليدوي عند طلب المستخدم."""
    await update.message.reply_text("👍 حسناً، جاري الفحص اليدوي للسوق الآن...")
    found_signals_count = await perform_scan(context)
    if found_signals_count == 0:
        await update.message.reply_text("✅ اكتمل الفحص اليدوي. لم يتم العثور على فرص مطابقة للاستراتيجية حالياً.")

async def timed_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """الوظيفة المجدولة التي تعمل بشكل متكرر."""
    await perform_scan(context)

async def send_startup_message(bot: Bot):
    """(جديد) إرسال رسالة عند بدء تشغيل البوت."""
    symbols_list_str = ", ".join(SYMBOLS_TO_WATCH)
    message = f"""
🚀 *تم تشغيل البوت بنجاح* 🚀

*الحالة:* متصل وجاهز للعمل.
*العملات قيد المراقبة:* `{symbols_list_str}`
*الفاصل الزمني للفحص:* `{LOOP_INTERVAL_SECONDS // 60} دقائق`

سأقوم بإعلامك فور العثور على فرصة مناسبة.
"""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info("✅ تم إرسال رسالة بدء التشغيل.")
    except Exception as e:
        logging.error(f"❌ فشل في إرسال رسالة بدء التشغيل: {e}")

# --- MAIN EXECUTION ---

if __name__ == '__main__':
    print("🚀 جارٍ بدء تشغيل البوت...")
    
    # تهيئة الاتصال بالمنصة
    exchange_client = get_exchange_client()
    if not exchange_client:
        print("⏹️ لا يمكن بدء تشغيل البوت بدون اتصال ناجح بالمنصة.")
        exit()

    # إعداد تطبيق البوت
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # تخزين كائن الاتصال بالمنصة للوصول إليه من أي مكان
    application.bot_data['exchange'] = exchange_client

    # إضافة الأوامر
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("scan", manual_scan_command))

    # جدولة الفحص التلقائي المتكرر
    job_queue = application.job_queue
    job_queue.run_repeating(timed_scan_job, interval=LOOP_INTERVAL_SECONDS, first=10) # يبدأ أول فحص بعد 10 ثوان

    # إرسال رسالة بدء التشغيل (مرة واحدة)
    # نستخدم asyncio.run لتشغيل هذه الدالة غير المتزامنة بشكل منفصل
    asyncio.run(send_startup_message(application.bot))

    # تشغيل البوت
    print("✅ البوت يعمل الآن ويستمع للأوامر...")
    application.run_polling()
