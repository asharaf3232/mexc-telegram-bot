# -*- coding: utf-8 -*-

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import re
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

## --- الإعدادات --- ##
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("FATAL ERROR: Missing Telegram environment variables.")
    exit()

EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120
SETTINGS_FILE = 'settings.json'
PERFORMANCE_FILE = 'recommendations_log.csv'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}}

## --- إدارة الإعدادات --- ##
DEFAULT_SETTINGS = {
    "active_strategy": "momentum_breakout", "top_n_symbols_by_volume": 150, "concurrent_workers": 10,
    "market_regime_filter_enabled": True, "take_profit_percentage": 4.0, "stop_loss_percentage": 2.0,
    "trailing_sl_enabled": True, "trailing_sl_activate_percent": 2.0, "trailing_sl_percent": 1.5,
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68},
    "mean_reversion": {"bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_oversold_level": 30}
}

def load_settings():
    # ... الكود الداخلي لم يتغير ...
    pass
def save_settings():
    # ... الكود الداخلي لم يتغير ...
    pass

## --- دوال التحليل والاستراتيجيات --- ##
def analyze_momentum_breakout(df, params):
    # ... الكود الداخلي لم يتغير ...
    pass
def analyze_mean_reversion(df, params):
    # ... الكود الداخلي لم يتغير ...
    pass
STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}

## --- الدوال الأساسية (مع تعديلات هامة) --- ##
async def initialize_exchanges():
    """(مُحصّنة) تهيئة الاتصال بكل المنصات مع محاولات إعادة اتصال."""
    exchange_ids = EXCHANGES_TO_SCAN
    
    async def connect_with_retries(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True})
        for attempt in range(3): # 3 محاولات
            try:
                await exchange.load_markets()
                bot_data["exchanges"][ex_id] = exchange
                logging.info(f"Successfully connected to {ex_id} on attempt {attempt + 1}")
                return
            except Exception as e:
                # (تشخيص دقيق) تسجيل الخطأ المحدد
                logging.error(f"Attempt {attempt + 1} failed to connect to {ex_id}: {type(e).__name__} - {e}")
                if attempt < 2:
                    await asyncio.sleep(10) # انتظار 10 ثوانٍ قبل المحاولة التالية
        await exchange.close() # إغلاق الاتصال الفاشل نهائياً

    tasks = [connect_with_retries(ex_id) for ex_id in exchange_ids]
    await asyncio.gather(*tasks)

# ... باقي الدوال الأساسية (aggregate_top_movers, worker, perform_scan, etc.) لم تتغير ...

## --- لوحات المفاتيح والأوامر --- ##
# ... كل دوال الأوامر والواجهة التفاعلية لم تتغير ...

## --- التشغيل الرئيسي المنظم (مع التحسينات) --- ##
async def main():
    """الدالة الرئيسية التي تنظم عملية بدء التشغيل بالكامل."""
    
    logging.info("Bot starting up... Giving network 5 seconds to initialize.")
    await asyncio.sleep(5) # (جديد) فترة إحماء للشبكة

    load_settings()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # إضافة الأوامر والمعالج الذكي
    # ... (الكود لم يتغير)
    
    async with application:
        await initialize_exchanges()
        if not bot_data["exchanges"]:
            logging.critical("CRITICAL: Failed to connect to any exchange after all retries. Bot cannot run.")
            await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="❌ فشل البوت في الاتصال بأي منصة. يرجى التحقق من السجل.")
            return

        # جدولة المهام الخلفية
        # ... (الكود لم يتغير)
        
        # إرسال رسالة البدء
        # ... (الكود لم يتغير)
        
        logging.info("Bot is now running and polling for updates...")
        await application.updater.start_polling()
        
        while True:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    print("🚀 Starting Resilient Trading Bot...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually.")
    except Exception as e:
        logging.critical(f"Bot stopped due to a critical error: {e}")

# ملاحظة: تم إخفاء الكود المتكرر. يجب عليك لصق الكود الكامل من الإصدار السابق وملء الفراغات.
# سأقوم بتوفير الكود الكامل أدناه لسهولة الاستخدام.

