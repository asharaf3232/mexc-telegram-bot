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
PERFORMANCE_FILE = 'recommendations_log.csv'
SETTINGS_FILE = 'settings.json'

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
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
    else:
        bot_data["settings"] = DEFAULT_SETTINGS
        save_settings()
    logging.info("Settings loaded successfully.")

def save_settings():
    with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
    logging.info("Settings saved successfully.")

## --- دوال التحليل والاستراتيجيات --- ##
def analyze_momentum_breakout(df, params):
    try:
        df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        required = [f"BBU_{params['bbands_period']}_{params['bbands_stddev']}", f"VWAP_D", f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        last, prev = df.iloc[-2], df.iloc[-3]
        if (prev[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] <= prev[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and last[f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] > last[f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"] and last['close'] > last[f"BBU_{params['bbands_period']}_{params['bbands_stddev']}"] and last['close'] > last[f"VWAP_D"] and last[f"RSI_{params['rsi_period']}"] < params['rsi_max_level']):
            return {"reason": "Momentum Breakout"}
    except Exception: return None
    return None

def analyze_mean_reversion(df, params):
    try:
        df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
        required = [f"BBL_{params['bbands_period']}_{params['bbands_stddev']}", f"RSI_{params['rsi_period']}"]
        if not all(col in df.columns for col in required): return None
        last = df.iloc[-2]
        if (last['close'] < last[f"BBL_{params['bbands_period']}_{params['bbands_stddev']}"] and last[f"RSI_{params['rsi_period']}"] < params['rsi_oversold_level']):
            return {"reason": "Mean Reversion (Oversold Bounce)"}
    except Exception: return None
    return None

STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}

## --- الدوال الأساسية --- ##
async def initialize_exchanges():
    # ... الكود الداخلي لم يتغير ...
    pass
async def aggregate_top_movers():
    # ... الكود الداخلي لم يتغير ...
    pass
async def worker(queue, results_list, settings):
    # ... الكود الداخلي لم يتغير ...
    pass
async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
def log_recommendation(signal):
    # ... الكود الداخلي لم يتغير ...
    pass
async def send_telegram_message(bot, signal_data, is_new=False, status=None, update_type=None):
    # ... الكود الداخلي لم يتغير ...
    pass
async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def check_market_regime():
    # ... الكود الداخلي لم يتغير ...
    pass
# --- لصق الدوال الكاملة من الإصدار السابق هنا ---

## --- لوحات المفاتيح والأوامر --- ##
main_menu_keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي", "⚙️ الإعدادات"]]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def show_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def show_set_parameter_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass
# --- لصق الدوال الكاملة من الإصدار السابق هنا ---

## --- الموجه الذكي للرسائل النصية --- ##
async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... الكود الداخلي لم يتغير ...
    pass

## --- (جديد) التشغيل الرئيسي المنظم --- ##
async def main():
    """الدالة الرئيسية التي تنظم عملية بدء التشغيل بالكامل."""
    
    # 1. تحميل الإعدادات أولاً
    load_settings()

    # 2. بناء التطبيق
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).build())
    
    # 3. إضافة الأوامر والمعالج الذكي
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))

    # 4. تشغيل كل شيء بالتوازي (التهيئة والتشغيل)
    async with application:
        # تهيئة الاتصالات بالمنصات
        await initialize_exchanges()
        if not bot_data["exchanges"]:
            logging.critical("CRITICAL: Failed to connect to any exchange. Bot cannot run.")
            return

        # جدولة المهام الخلفية
        application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
        application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20)
        
        # إرسال رسالة البدء
        exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🚀 *بوت التداول التفاعلي جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية:* `{bot_data['settings']['active_strategy']}`",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # بدء الاستماع للتحديثات
        logging.info("Bot is now running and polling for updates...")
        await application.updater.start_polling()
        
        # حلقة لا نهائية لإبقاء البوت يعمل
        while True:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    print("🚀 Starting Interactive Trading Bot...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually.")
    except Exception as e:
        logging.critical(f"Bot stopped due to a critical error: {e}")

