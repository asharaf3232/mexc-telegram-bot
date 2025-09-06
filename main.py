# -*- coding: utf-8 -*-

import ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os # <-- تم استيراد مكتبة os للتعامل مع متغيرات البيئة
from telegram import Bot
from telegram.constants import ParseMode

## --- CONFIGURATION - الآن تتم القراءة من متغيرات البيئة --- ##

# 1. مفاتيح منصة MEXC
# سيتم الآن قراءة هذه المفاتيح من متغيرات البيئة في الاستضافة
MEXC_API_KEY = os.getenv('MEXC_API_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_SECRET_KEY')

# 2. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# التأكد من وجود كل المتغيرات الضرورية
if not all([MEXC_API_KEY, MEXC_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة غير موجود.")
    print("يرجى التأكد من تعيين: MEXC_API_KEY, MEXC_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
    exit() # إيقاف البرنامج إذا كانت المعلومات ناقصة

# 3. إعدادات استراتيجية التحليل (يمكن تركها هنا أو نقلها لمتغيرات البيئة)
SYMBOLS_TO_WATCH = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 300

# 4. معايير الاستراتيجية الكمية
VOLUME_SPIKE_FACTOR = 3.0
EMA_FAST_PERIOD = 10
EMA_SLOW_PERIOD = 25
RSI_PERIOD = 14
RSI_MAX_LEVEL = 65

# 5. إعدادات إدارة المخاطر
TAKE_PROFIT_PERCENTAGE = 3.0
STOP_LOSS_PERCENTAGE = 1.5

## --- END OF CONFIGURATION --- ##


# متغير لتخزين وقت آخر إشارة تم إرسالها لكل عملة لمنع التكرار
last_signal_time = {}

# --- 1. الاتصال بالمنصة وجلب البيانات ---
def get_exchange_client():
    """يقوم بإعداد وتجهيز الاتصال بمنصة MEXC."""
    try:
        exchange = ccxt.mexc({
            'apiKey': MEXC_API_KEY,
            'secret': MEXC_SECRET_KEY,
            'options': {
                'defaultType': 'spot',
            },
        })
        print("✅ تم الاتصال بنجاح بمنصة MEXC.")
        return exchange
    except Exception as e:
        print(f"❌ فشل الاتصال بمنصة MEXC: {e}")
        return None

def fetch_data(exchange, symbol, timeframe):
    """يجلب بيانات الشموع التاريخية لعملة معينة."""
    try:
        # نحتاج 100 شمعة على الأقل لحساب المتوسطات بشكل دقيق
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"⚠️ حدث خطأ أثناء جلب البيانات لعملة {symbol}: {e}")
        return None

# --- 2. محرك التحليل الكمي ---
def analyze_market_data(df, symbol):
    """
    يطبق الاستراتيجية على البيانات ويقرر ما إذا كانت هناك فرصة للشراء.
    """
    if df is None or len(df) < EMA_SLOW_PERIOD:
        return None

    try:
        # حساب المؤشرات الفنية باستخدام مكتبة pandas-ta
        df.ta.ema(length=EMA_FAST_PERIOD, append=True)
        df.ta.ema(length=EMA_SLOW_PERIOD, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)
        df['volume_sma'] = df['volume'].rolling(window=EMA_SLOW_PERIOD).mean()

        # الحصول على بيانات آخر شمعة مكتملة (الشمعة قبل الأخيرة)
        last_row = df.iloc[-2]
        previous_row = df.iloc[-3]

        # --- شروط توليد الإشارة ---
        volume_condition = last_row['volume'] > last_row['volume_sma'] * VOLUME_SPIKE_FACTOR
        crossover_condition = previous_row[f'EMA_{EMA_FAST_PERIOD}'] <= previous_row[f'EMA_{EMA_SLOW_PERIOD}'] and last_row[f'EMA_{EMA_FAST_PERIOD}'] > last_row[f'EMA_{EMA_SLOW_PERIOD}']
        rsi_condition = last_row[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL
        price_condition = last_row['close'] > last_row[f'EMA_{EMA_FAST_PERIOD}']

        if volume_condition and crossover_condition and rsi_condition and price_condition:
            entry_price = last_row['close']
            take_profit = entry_price * (1 + TAKE_PROFIT_PERCENTAGE / 100)
            stop_loss = entry_price * (1 - STOP_LOSS_PERCENTAGE / 100)

            signal = {
                "symbol": symbol,
                "entry_price": entry_price,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "timestamp": last_row['timestamp']
            }
            print(f"💡 تم العثور على إشارة شراء محتملة لعملة {symbol}!")
            return signal

    except Exception as e:
        print(f"❌ خطأ أثناء تحليل بيانات {symbol}: {e}")

    return None

# --- 3. إرسال الإشعارات عبر تليجرام ---
async def send_telegram_message(signal):
    """يقوم بتنسيق رسالة التوصية وإرسالها إلى تليجرام."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    
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
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode=ParseMode.MARKDOWN
        )
        print(f"✅ تم إرسال التوصية بنجاح إلى تليجرام.")
    except Exception as e:
        print(f"❌ فشل إرسال الرسالة إلى تليجرام: {e}")

# --- 4. الحلقة الرئيسية لتشغيل البوت ---
async def main_loop():
    """الحلقة الرئيسية التي تقوم بتشغيل البوت بشكل مستمر."""
    exchange = get_exchange_client()
    if not exchange:
        print("⏹️ لا يمكن بدء تشغيل البوت بدون اتصال ناجح بالمنصة.")
        return

    while True:
        print("\n" + "="*40)
        print(f"▶️  بدء جولة فحص جديدة للسوق... ({time.strftime('%Y-%m-%d %H:%M:%S')})")
        
        # استخدام asyncio.gather لتشغيل المهام بشكل متزامن
        tasks = [check_symbol(exchange, symbol) for symbol in SYMBOLS_TO_WATCH]
        await asyncio.gather(*tasks)

        print(f"⏹️  انتهاء جولة الفحص. الانتظار لمدة {LOOP_INTERVAL_SECONDS} ثانية...")
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)

async def check_symbol(exchange, symbol):
    """دالة منفصلة لتحليل كل عملة على حدة."""
    print(f"--- تحليل عملة {symbol} ---")
    df = fetch_data(exchange, symbol, TIMEFRAME)
    
    if df is not None:
        signal = analyze_market_data(df, symbol)
        
        if signal:
            current_time = time.time()
            # منع تكرار الإشارة لنفس العملة خلال فترة زمنية قصيرة (ضعف فترة الفحص)
            if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 2):
                await send_telegram_message(signal)
                last_signal_time[symbol] = current_time
            else:
                print(f"ℹ️ تم تجاهل إشارة مكررة لعملة {symbol}.")

if __name__ == '__main__':
    try:
        print("🚀 جارٍ بدء تشغيل البوت...")
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n🛑 تم إيقاف البوت يدوياً.")
    except Exception as e:
        print(f"\n❌ حدث خطأ غير متوقع في الحلقة الرئيسية: {e}")
