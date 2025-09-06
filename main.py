# -*- coding: utf-8 -*-

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import re
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
    """تحميل الإعدادات من ملف JSON أو إنشاء ملف افتراضي."""
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
    else:
        bot_data["settings"] = DEFAULT_SETTINGS
        save_settings()
    logging.info("Settings loaded successfully.")

def save_settings():
    """حفظ الإعدادات الحالية في ملف JSON."""
    with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
    logging.info("Settings saved successfully.")

## --- دوال التحليل والاستراتيجيات --- ##
def analyze_momentum_breakout(df, params):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
def analyze_mean_reversion(df, params):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
STRATEGIES = {"momentum_breakout": analyze_momentum_breakout, "mean_reversion": analyze_mean_reversion}

async def fetch_and_analyze(market_info):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def worker(queue, results_list, settings):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
def log_recommendation(signal):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def send_telegram_message(bot, signal_data, is_new=False, status=None, update_type=None):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def initialize_exchanges():
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def check_market_regime():
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass
async def aggregate_top_movers():
    # (الكود الداخلي لهذه الدالة لم يتغير)
    pass

# --- إعادة تعريف الدوال المخفية بالكود الكامل ---
# ... (لصق الدوال الكاملة من الإصدار السابق هنا)

## --- (جديد) لوحات المفاتيح التفاعلية --- ##
main_menu_keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي", "⚙️ الإعدادات"]]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

## --- أوامر ومعالجات تليجرام التفاعلية --- ##

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض القائمة الرئيسية عند البدء."""
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك! أنا بوت التداول القابل للتخصيص. استخدم الأزرار للتفاعل.", reply_markup=reply_markup)

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أزرار القائمة الرئيسية."""
    text = update.message.text
    if text == "📊 الإحصائيات":
        await stats_command(update, context)
    elif text == "ℹ️ مساعدة":
        await help_command(update, context)
    elif text == "🔍 فحص يدوي":
        await manual_scan_command(update, context)
    elif text == "⚙️ الإعدادات":
        reply_markup = ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True)
        await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)

async def handle_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أزرار قائمة الإعدادات."""
    text = update.message.text
    if text == "📈 تغيير الاستراتيجية":
        reply_markup = ReplyKeyboardMarkup(strategy_menu_keyboard, resize_keyboard=True)
        await update.message.reply_text("اختر استراتيجية التداول الجديدة:", reply_markup=reply_markup)
    elif text == "🔧 تعديل المعايير":
        params_list = "\n".join([f"`{k}`" for k, v in bot_data["settings"].items() if not isinstance(v, dict)])
        await update.message.reply_text(
            f"لتعديل معيار، أرسل رسالة بالصيغة:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير القابلة للتعديل:*\n{params_list}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True)
        )
    elif text == "🔙 القائمة الرئيسية":
        await start_command(update, context)

async def handle_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أزرار قائمة الاستراتيجيات."""
    text = update.message.text
    strategy_map = {"🚀 الزخم والاندفاع": "momentum_breakout", "🔄 الارتداد من الدعم": "mean_reversion"}
    
    if text in strategy_map:
        strategy_name = strategy_map[text]
        bot_data["settings"]["active_strategy"] = strategy_name
        save_settings()
        await update.message.reply_text(f"✅ تم تفعيل استراتيجية `{strategy_name}` بنجاح.")
    
    # العودة لقائمة الإعدادات في كل الحالات
    reply_markup = ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر إعداداً آخر أو عد للقائمة الرئيسية.", reply_markup=reply_markup)

async def handle_set_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل النصية لتعديل المعايير."""
    text = update.message.text
    match = re.match(r"^\s*(\w+)\s*=\s*(.+)\s*$", text)
    if not match:
        await update.message.reply_text("الصيغة غير صحيحة. يرجى استخدام: `اسم_المعيار = قيمة_جديدة`")
        return

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
            await update.message.reply_text(f"❌ قيمة غير صالحة. لا يمكن تحويل '{value_str}' إلى النوع المطلوب.")
    else:
        await update.message.reply_text(f"❌ خطأ: المعيار `{param}` غير موجود أو لا يمكن تعديله.")


# (باقي دوال الأوامر مثل help_command, manual_scan_command, stats_command لم تتغير)

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت مباشرة."""
    load_settings() # تحميل الإعدادات عند البدء
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.critical("CRITICAL: Failed to connect to any exchange. Bot cannot run.")
        return
    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت التداول التفاعلي جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية النشطة:* `{bot_data['settings']['active_strategy']}`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20)

async def post_shutdown(application: Application):
    """إغلاق الاتصالات بأمان."""
    logging.info("Closing all exchange connections...")
    for exchange in bot_data["exchanges"].values(): await exchange.close()
    logging.info("Connections closed successfully.")

## --- التشغيل الرئيسي --- ##

if __name__ == '__main__':
    print("🚀 Starting Interactive Trading Bot...")
    
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    
    # نظام معالجة الرسائل الجديد المعتمد على الأزرار
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))
    
    # استخدام فلاتر مخصصة لتوجيه الرسائل إلى المعالج الصحيح بناءً على لوحة المفاتيح
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^(📈 تغيير الاستراتيجية|🔧 تعديل المعايير|🔙 القائمة الرئيسية)$')), handle_settings_menu))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^(🚀 الزخم والاندفاع|🔄 الارتداد من الدعم|🔙 قائمة الإعدادات)$')), handle_strategy_menu))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^\s*\w+\s*=\s*.+$')), handle_set_parameter))

    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

