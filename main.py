# -*- coding: utf-8 -*-

import ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os
import logging
from datetime import datetime
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

## --- الإعدادات --- ##

# 1. مفاتيح المنصة من متغيرات البيئة
MEXC_API_KEY = os.getenv('MEXC_API_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_API_SECRET')

# 2. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([MEXC_API_KEY, MEXC_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("خطأ فادح: متغير واحد أو أكثر من متغيرات البيئة غير موجود.")
    exit()

# 3. إعدادات استراتيجية التداول ومسح السوق
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 900  # 15 دقيقة
EXCLUDED_SYMBOLS = ['BTC/USDT', 'ETH/USDT']
STABLECOINS = ['USDC', 'DAI', 'BUSD', 'TUSD', 'USDP']
PERFORMANCE_FILE = 'recommendations_log.csv'

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

# --- تهيئة ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {
    "exchange": None,
    "symbols_to_watch": [],
    "last_signal_time": {}
}

## --- الدوال الأساسية --- ##

def get_exchange_client():
    """تهيئة الاتصال بمنصة MEXC."""
    try:
        exchange = ccxt.mexc({
            'apiKey': MEXC_API_KEY,
            'secret': MEXC_SECRET_KEY,
            'options': {'defaultType': 'spot'},
        })
        exchange.load_markets()
        logging.info("تم الاتصال بنجاح بمنصة MEXC.")
        return exchange
    except Exception as e:
        logging.error(f"فشل الاتصال بمنصة MEXC: {e}")
        return None

async def fetch_dynamic_symbols(exchange):
    """جلب كل أزواج USDT من المنصة واستثناء العملات المحددة والمستقرة."""
    logging.info("جاري جلب قائمة العملات الديناميكية من MEXC...")
    try:
        all_symbols = [s for s in exchange.symbols if s.endswith('/USDT')]
        filtered_symbols = [s for s in all_symbols if not any(stable in s for stable in STABLECOINS)]
        final_symbols = [s for s in filtered_symbols if s not in EXCLUDED_SYMBOLS]
        logging.info(f"تم العثور على {len(final_symbols)} عملة للمراقبة.")
        return final_symbols
    except Exception as e:
        logging.error(f"خطأ في جلب العملات: {e}")
        return []

def fetch_data(exchange, symbol, timeframe):
    """جلب بيانات الشموع التاريخية لعملة معينة."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        # **الحل لمشكلة التحليل**: تعيين timestamp كفهرس
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        logging.warning(f"لم يتمكن من جلب البيانات لـ {symbol}: {e}")
        return None

def analyze_market_data(df, symbol):
    """تطبيق الاستراتيجية المتقدمة على البيانات."""
    if df is None or len(df) < BBANDS_PERIOD: return None
    try:
        # حساب المؤشرات
        df.ta.vwap(length=VWAP_PERIOD, append=True)
        df.ta.bbands(length=BBANDS_PERIOD, std=BBANDS_STDDEV, append=True)
        df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)

        last, prev = df.iloc[-2], df.iloc[-3]

        # --- شروط الاستراتيجية ---
        macd_crossover = prev[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] <= prev[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and \
                         last[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] > last[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
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
    except KeyError as ke:
        logging.error(f"خطأ في تحليل {symbol}: لم يتم العثور على المؤشر {ke}. قد تكون البيانات غير كافية.")
    except Exception as e:
        logging.error(f"خطأ عام في تحليل {symbol}: {e}")
    return None

async def send_telegram_message(bot: Bot, signal):
    """تنسيق وإرسال إشارة التداول إلى تليجرام."""
    message = f"""
✅ *توصية تداول جديدة* ✅

*العملة:* `{signal['symbol']}`
*الاستراتيجية:* `{signal['reason']}`
*الإجراء:* `شراء (BUY)`

*سعر الدخول:* `${signal['entry_price']:,.4f}`

🎯 *جني الأرباح ({TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.4f}`
🛑 *وقف الخسارة ({STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.4f}`

*إخلاء مسؤولية: التداول عالي المخاطر.*
"""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"تم إرسال التوصية بنجاح لعملة {signal['symbol']}.")
        log_recommendation(signal)
    except Exception as e:
        logging.error(f"فشل إرسال الرسالة إلى تليجرام: {e}")

def log_recommendation(signal):
    """حفظ التوصية في ملف CSV لتتبع الأداء."""
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    df = pd.DataFrame([{'timestamp': signal['timestamp'], 'symbol': signal['symbol'], 'entry_price': signal['entry_price'],
                        'take_profit': signal['take_profit'], 'stop_loss': signal['stop_loss'],
                        'status': 'نشطة', 'exit_price': None, 'closed_at': None}])
    with open(PERFORMANCE_FILE, 'a') as f:
        df.to_csv(f, header=not file_exists, index=False, encoding='utf-8-sig')
    logging.info(f"تم تسجيل التوصية لعملة {signal['symbol']}")

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """تنفيذ جولة فحص واحدة للسوق."""
    exchange, symbols, last_signal_time = bot_data['exchange'], bot_data['symbols_to_watch'], bot_data['last_signal_time']
    found_signals = 0
    logging.info(f"بدء جولة فحص جديدة لـ {len(symbols)} عملة...")
    for symbol in symbols:
        df = fetch_data(exchange, symbol, TIMEFRAME)
        if df is not None:
            signal = analyze_market_data(df, symbol)
            if signal:
                current_time = time.time()
                if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 4):
                    await send_telegram_message(context.bot, signal)
                    last_signal_time[symbol] = current_time
                    found_signals += 1
                else:
                    logging.info(f"تم تجاهل إشارة مكررة لعملة {symbol}.")
    logging.info(f"اكتمل الفحص. تم العثور على {found_signals} إشارة جديدة.")

## --- أوامر ومعالجات تليجرام (مُعرّبة) --- ##

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """التعامل مع أمر /start وعرض القائمة الرئيسية."""
    keyboard = [
        ["📊 الإحصائيات", "ℹ️ مساعدة"],
        ["🔍 فحص يدوي"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "أهلاً بك في بوت التداول المتقدم! أنا الآن أراقب السوق. استخدم الأزرار في الأسفل للتفاعل.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض رسالة المساعدة."""
    help_text = """
*مساعدة بوت التداول المتقدم*

`🔍 فحص يدوي` - يقوم بتشغيل فحص فوري للسوق.
`📊 الإحصائيات` - يعرض إحصائيات أداء التوصيات السابقة.
`ℹ️ مساعدة` - يعرض رسالة المساعدة هذه.

يقوم البوت تلقائياً بمسح جميع أزواج USDT على منصة MEXC (باستثناء BTC و ETH) كل 15 دقيقة.
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """التعامل مع طلب الفحص اليدوي."""
    await update.message.reply_text("👍 حسناً! جاري بدء فحص يدوي للسوق الآن...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص اليدوي.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض إحصائيات الأداء."""
    if not os.path.exists(PERFORMANCE_FILE):
        await update.message.reply_text("لم يتم تسجيل أي توصيات بعد. الإحصائيات غير متاحة.")
        return

    df = pd.read_csv(PERFORMANCE_FILE)
    total_recs = len(df)
    tp_hits = len(df[df['status'] == 'tp_hit']) # ملاحظة: يتطلب تحديثاً خارجياً
    sl_hits = len(df[df['status'] == 'sl_hit']) # ملاحظة: يتطلب تحديثاً خارجياً
    active_trades = len(df[df['status'] == 'نشطة'])
    win_rate = (tp_hits / (tp_hits + sl_hits) * 100) if (tp_hits + sl_hits) > 0 else 0
    
    stats_message = f"""
*إحصائيات الأداء*

- *إجمالي التوصيات المرسلة:* {total_recs}
- *الصفقات النشطة:* {active_trades}
- *صفقات حققت الهدف:* {tp_hits}
- *صفقات ضربت وقف الخسارة:* {sl_hits}
- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`

*ملاحظة: هذا نظام تتبع أداء مبسط.*
"""
    await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """التعامل مع ضغطات الأزرار من لوحة المفاتيح المخصصة."""
    text = update.message.text
    if text == "📊 الإحصائيات":
        await stats_command(update, context)
    elif text == "ℹ️ مساعدة":
        await help_command(update, context)
    elif text == "🔍 فحص يدوي":
        await manual_scan_command(update, context)

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت مباشرة."""
    bot_data['exchange'] = get_exchange_client()
    if not bot_data['exchange']:
        logging.error("لم يتم الاتصال بالمنصة. لن يتمكن البوت من الفحص.")
        return
    bot_data['symbols_to_watch'] = await fetch_dynamic_symbols(bot_data['exchange'])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *البوت الآن متصل وجاهز للعمل!*\n- *الاستراتيجية:* `متقدمة (MACD, BB, VWAP)`\n- *عدد العملات المراقبة:* `{len(bot_data['symbols_to_watch'])}`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=LOOP_INTERVAL_SECONDS, first=10)

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 جاري بدء تشغيل البوت...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print("✅ البوت يعمل الآن ويستمع للتحديثات...")
    application.run_polling()

