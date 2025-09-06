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

# 1. إعدادات بوت التليجرام
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("FATAL ERROR: Missing Telegram environment variables.")
    exit()

# 2. إعدادات أساسية
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120
PERFORMANCE_FILE = 'recommendations_log.csv'
SETTINGS_FILE = 'settings.json'

# --- تهيئة ---
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

# --- دوال التحليل والاستراتيجيات (لا تغيير) ---
def analyze_momentum_breakout(df, params):
    # الكود الكامل هنا
    pass
def analyze_mean_reversion(df, params):
    # الكود الكامل هنا
    pass
STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}

# --- باقي الدوال الأساسية (لا تغيير) ---
async def initialize_exchanges(): pass
async def aggregate_top_movers(): pass
async def worker(queue, results_list): pass
async def perform_scan(context: ContextTypes.DEFAULT_TYPE): pass
def log_recommendation(signal): pass
async def send_telegram_message(bot, signal_data, is_new=False, status=None, update_type=None): pass
async def track_open_trades(context: ContextTypes.DEFAULT_TYPE): pass
async def check_market_regime(): pass

# --- إعادة تعريف الدوال المخفية بالكود الكامل ---
# (لصق الدوال الكاملة من الإصدار السابق هنا)

## --- لوحات المفاتيح التفاعلية (لا تغيير) --- ##
main_menu_keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي", "⚙️ الإعدادات"]]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

## --- دوال عرض القوائم والأوامر --- ##

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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # (الكود لم يتغير)
async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # (الكود لم يتغير)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # (الكود لم يتغير)


## --- (جديد) الموجه الذكي للرسائل النصية --- ##

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هذا المعالج هو المسؤول الوحيد عن توجيه كل الرسائل النصية."""
    text = update.message.text
    
    # القائمة الرئيسية
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)
    elif text == "⚙️ الإعدادات": await show_settings_menu(update, context)
    
    # قائمة الإعدادات
    elif text == "📈 تغيير الاستراتيجية": await show_strategy_menu(update, context)
    elif text == "🔧 تعديل المعايير": await show_set_parameter_instructions(update, context)
    elif text == "🔙 القائمة الرئيسية": await start_command(update, context)

    # قائمة الاستراتيجيات
    elif text == "🚀 الزخم والاندفاع":
        bot_data["settings"]["active_strategy"] = "momentum_breakout"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الزخم والاندفاع`.")
        await show_settings_menu(update, context) # العودة لقائمة الإعدادات
    elif text == "🔄 الارتداد من الدعم":
        bot_data["settings"]["active_strategy"] = "mean_reversion"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الارتداد من الدعم`.")
        await show_settings_menu(update, context) # العودة لقائمة الإعدادات
    elif text == "🔙 قائمة الإعدادات":
        await show_settings_menu(update, context)
        
    # معالجة تعديل المعايير
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
    # else:
    #     await update.message.reply_text(" أمر غير معروف. يرجى استخدام الأزرار.")


async def post_init(application: Application): pass # (الكود لم يتغير)
async def post_shutdown(application: Application): pass # (الكود لم يتغير)

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 Starting Interactive Trading Bot...")
    load_settings()
    
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    
    # إضافة الأوامر والمعالج الذكي الوحيد
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

