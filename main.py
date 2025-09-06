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
TOP_N_SYMBOLS_BY_VOLUME = 150 # زيادة العدد قليلاً لتغطية كل المنصات
PERFORMANCE_FILE = 'recommendations_log.csv'

# 3. معايير الاستراتيجية المتقدمة (لا تغيير)
VWAP_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STDDEV = 2.0
RSI_PERIOD = 14
RSI_MAX_LEVEL = 68

# 4. إدارة المخاطر (لا تغيير)
TAKE_PROFIT_PERCENTAGE = 4.0
STOP_LOSS_PERCENTAGE = 2.0

# --- تهيئة ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {"exchanges": {}, "last_signal_time": {}}

## --- الدوال الأساسية (مع تعديلات هامة) --- ##

async def initialize_exchanges():
    """(مُحسّنة) تهيئة الاتصال بكل المنصات بشكل متزامن."""
    exchange_ids = EXCHANGES_TO_SCAN
    exchange_tasks = [getattr(ccxt, ex_id)({'enableRateLimit': True}) for ex_id in exchange_ids]
    
    for i, exchange in enumerate(exchange_tasks):
        try:
            await exchange.load_markets()
            bot_data["exchanges"][exchange.id] = exchange
            logging.info(f"تم الاتصال بنجاح بمنصة {exchange.id}")
        except Exception as e:
            logging.error(f"فشل الاتصال بمنصة {exchange.id}: {e}")
    
    # التأكد من إغلاق الجلسات التي لم يتم استخدامها لتجنب التحذيرات
    closable_exchanges = [ex for ex in exchange_tasks if ex.id not in bot_data["exchanges"]]
    for exchange in closable_exchanges:
        await exchange.close()

async def aggregate_top_movers():
    """(جديد ومُحصّن) تجميع أفضل العملات من كل المنصات المتاحة."""
    all_tickers = []
    logging.info("بدء تجميع العملات النشطة من كل المنصات...")

    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            # إضافة اسم المنصة لكل عملة
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
    
    # إزالة التكرارات مع الاحتفاظ بالعملة من المنصة الأعلى سيولة
    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols:
            unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}

    final_list = list(unique_symbols.values())[:TOP_N_SYMBOLS_BY_VOLUME]
    logging.info(f"تم تجميع أفضل {len(final_list)} عملة فريدة من كل المنصات.")
    return final_list

def fetch_data(exchange, symbol, timeframe):
    """جلب بيانات الشموع (لا تغيير)."""
    # هذه الدالة لا تحتاج للتعديل
    pass # (الكود موجود في الملف السابق، لا حاجة لتكراره)

def analyze_market_data(df, symbol):
    """تحليل البيانات (لا تغيير)."""
    # هذه الدالة لا تحتاج للتعديل
    pass # (الكود موجود في الملف السابق، لا حاجة لتكراره)

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """(مُحسّنة) تنفيذ جولة فحص واحدة للسوق على العملات المجمعة."""
    top_markets = await aggregate_top_movers()
    if not top_markets:
        logging.info("لا توجد أسواق لفحصها في هذه الجولة.")
        return

    found_signals = 0
    logging.info(f"بدء جولة فحص جديدة لـ {len(top_markets)} عملة نشطة...")
    
    last_signal_time = bot_data['last_signal_time']

    for market in top_markets:
        exchange_id = market['exchange']
        symbol = market['symbol']
        
        exchange = bot_data["exchanges"].get(exchange_id)
        if not exchange: continue

        df = await fetch_data_async(exchange, symbol, TIMEFRAME) # استخدام دالة async
        
        if df is not None:
            signal = analyze_market_data(df, symbol)
            if signal:
                # إضافة المنصة إلى بيانات الإشارة
                signal['exchange'] = exchange_id.capitalize()
                current_time = time.time()
                if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 4):
                    await send_telegram_message(context.bot, signal)
                    last_signal_time[symbol] = current_time
                    found_signals += 1
        await asyncio.sleep(0.5)
        
    logging.info(f"اكتمل الفحص. تم العثور على {found_signals} إشارة جديدة.")

# دالة مساعد async لجلب البيانات
async def fetch_data_async(exchange, symbol, timeframe):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        if len(ohlcv) < BBANDS_PERIOD: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception:
        return None

async def send_telegram_message(bot, signal):
    """(مُحسّنة) إرسال التوصية مع ذكر اسم المنصة."""
    message = f"""
✅ *توصية تداول جديدة* ✅

*المنصة:* `{signal['exchange']}`
*العملة:* `{signal['symbol']}`
*الاستراتيجية:* `{signal['reason']}`

*الإجراء:* `شراء (BUY)`
*سعر الدخول:* `${signal['entry_price']:,.4f}`

🎯 *جني الأرباح ({TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.4f}`
🛑 *وقف الخسارة ({STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.4f}`

*إخلاء مسؤولية: التداول عالي المخاطر.*
"""
    # (باقي كود الإرسال والتسجيل كما هو)
    pass # (الكود موجود في الملف السابق، لا حاجة لتكراره)


## --- أوامر ومعالجات تليجرام (مُعرّبة) --- ##
# (كل أوامر تليجرام: start, help, manual_scan, stats, text_handler لا تحتاج لتعديل)
pass # (الكود موجود في الملف السابق، لا حاجة لتكراره)

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت مباشرة."""
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.error("فشل الاتصال بجميع المنصات. سيتم إيقاف البوت.")
        return

    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت المنصات المتعددة جاهز للعمل!*\n- *المنصات المتصلة:* `{exchange_names}`\n- *الاستراتيجية:* `القنص الذكي`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=LOOP_INTERVAL_SECONDS, first=10)

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    # (كود التشغيل الرئيسي لا يحتاج تعديل)
    pass # (الكود موجود في الملف السابق، لا حاجة لتكراره)

# ملاحظة: تم إخفاء الأجزاء غير المتغيرة من الكود للاختصار.
# يجب عليك دمج الأجزاء الجديدة والمعدلة مع الكود الكامل الموجود لديك.

