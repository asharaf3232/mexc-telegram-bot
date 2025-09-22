# -*- coding: utf-8 -*-
# =======================================================================================
# --- 🚀 العقل الخارق للنظام التجاري | v1.2 (الإصدار الهجين) 🚀 ---
# =======================================================================================
#
# هذا الإصدار يحول "العقل" إلى نظام هجين يدعم كلاً من التداول الآلي واليدوي.
#
# --- سجل التغييرات v1.2 ---
#   ✅ [ميزة رئيسية] **إضافة "أوضاع التنفيذ" (Execution Modes):**
#       - يمكن الآن ضبط كل منصة على (تلقائي | يدوي | معطل) بشكل مستقل.
#   ✅ [ميزة رئيسية] **استعادة قناة التوصيات اليدوية:**
#       - إذا كان وضع المنصة "يدوي"، يرسل العقل توصية مفصلة إلى قناة تليجرام المخصصة.
#   ✅ [تكامل] **توجيه الإشارات:**
#       - إذا كان الوضع "تلقائي"، يتم نشر الإشارة إلى Redis لتنفذها "اليد" الآلية.
#   ✅ [واجهة المستخدم] إضافة قائمة جديدة في إعدادات تليجرام للتحكم في "أوضاع التنفيذ".
#   ✅ [تحسين] إعادة إضافة متغير `TELEGRAM_SIGNAL_CHANNEL_ID` للإعدادات.
#
# =======================================================================================

# --- المكتبات الأساسية ---
import ccxt.async_support as ccxt_async
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import time
import aiosqlite
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
from collections import deque, Counter, defaultdict
import copy

# --- مكتبات التواصل والأدوات الإضافية ---
import redis.asyncio as redis
import feedparser
import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest

try:
    import nltk
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    NLTK_AVAILABLE = True
except ImportError: NLTK_AVAILABLE = False

try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError: SCIPY_AVAILABLE = False


# --- الإعدادات الأساسية ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID) # [إعادة إضافة]
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_SIGNAL_CHANNEL = "trade_signals"
REDIS_STATS_CHANNEL = "trade_statistics"

# --- إعدادات البوت ---
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
SCAN_INTERVAL_SECONDS = 900
STRATEGY_ANALYSIS_INTERVAL_SECONDS = 7200

APP_ROOT = '.'
DB_FILE = os.path.join(APP_ROOT, 'brain_v1.2.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'brain_settings_v1.2.json')

EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logger = logging.getLogger("Brain")

# --- الحالة العامة للعقل ---
class BrainState:
    def __init__(self):
        self.settings = {}
        self.exchanges = {}
        self.redis_publisher = None
        self.redis_subscriber = None
        self.application = None
        self.last_signal_time = defaultdict(lambda: defaultdict(float))
        self.strategy_performance = {}
        self.pending_strategy_proposal = {}
        self.active_preset_name = "مخصص"
        self.scan_history = deque(maxlen=20)
        self.market_mood = {"mood": "UNKNOWN", "reason": "تحليل لم يتم بعد"}

brain_state = BrainState()
scan_lock = asyncio.Lock()

# --- تعريف الاستراتيجيات وأسماؤها ---
STRATEGY_NAMES_AR = {
    "momentum_breakout": "زخم اختراقي", "breakout_squeeze_pro": "اختراق انضغاطي",
    "rsi_divergence": "دايفرجنس RSI", "supertrend_pullback": "انعكاس سوبرترند",
    "support_rebound": "ارتداد الدعم", "sniper_pro": "القناص المحترف",
    "whale_radar": "رادار الحيتان", "arbitrage_hunter": "صياد الفرص (أربيتراج)"
}

# --- الإعدادات الافتراضية للعقل ---
DEFAULT_SETTINGS = {
    # [إضافة جديدة] أوضاع التنفيذ لكل منصة
    "execution_modes": {
        "okx": "AUTOMATIC",
        "binance": "MANUAL",
        "bybit": "MANUAL",
        "kucoin": "DISABLED",
        "gate": "DISABLED",
        "mexc": "DISABLED",
    },
    "top_n_symbols_by_volume": 300,
    "concurrent_workers": 10,
    "min_signal_strength": 1,
    "active_scanners": list(STRATEGY_NAMES_AR.keys()),
    "liquidity_filters": {"min_quote_volume_24h_usd": 1000000, "min_rvol": 1.5},
    "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.8},
    "spread_filter": {"max_spread_percent": 0.5},
    "market_mood_filter_enabled": True,
    "fear_and_greed_threshold": 30,
    "btc_trend_filter_enabled": True,
    "news_filter_enabled": True,
    "adaptive_intelligence_enabled": True,
    "dynamic_trade_sizing_enabled": True,
    "strategy_proposal_enabled": True,
    "strategy_analysis_min_trades": 10,
    "strategy_deactivation_threshold_wr": 45.0,
    "dynamic_sizing_max_increase_pct": 25.0,
    "dynamic_sizing_max_decrease_pct": 50.0,
    "arbitrage_scanner_enabled": True,
    "min_arbitrage_profit_percent": 0.5,
    "arbitrage_estimated_fees_percent": 0.2,
    "atr_sl_multiplier": 2.5,
    "risk_reward_ratio": 2.0,
}
# ... (بقية الكود من v1.1 لم يتغير بشكل كبير، سيتم إدراج الأجزاء المعدلة فقط)
# --- The unchanged parts of the code from v1.1 are omitted for brevity ---
# --- Only the modified and new functions will be shown below ---
def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f: brain_state.settings = json.load(f)
        else: brain_state.settings = copy.deepcopy(DEFAULT_SETTINGS)
    except Exception: brain_state.settings = copy.deepcopy(DEFAULT_SETTINGS)
    for key, value in DEFAULT_SETTINGS.items():
        if isinstance(value, dict):
            if key not in brain_state.settings or not isinstance(brain_state.settings[key], dict): brain_state.settings[key] = {}
            for sub_key, sub_value in value.items(): brain_state.settings[key].setdefault(sub_key, sub_value)
        else: brain_state.settings.setdefault(key, value)
    # Ensure all exchanges have an execution mode
    for ex_id in EXCHANGES_TO_SCAN:
        if ex_id not in brain_state.settings['execution_modes']:
            brain_state.settings['execution_modes'][ex_id] = 'DISABLED'
    save_settings()
    logger.info("Brain settings loaded successfully.")

async def send_telegram_recommendation(bot, signal):
    """Formats and sends a manual trade recommendation to the signal channel."""
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"

    target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
    strength_stars = '⭐' * signal.get('strength', 1)
    
    if signal.get('reason') == 'arbitrage_hunter':
        title = f"**🏹 فرصة أربيتراج | {signal['symbol']}**"
        buy_price, sell_price, profit = signal['buy_price'], signal['sell_price'], signal['profit_percent']
        message = (f"**Arbitrage Alert | تنبيه أربيتراج**\n------------------------------------\n{title}\n------------------------------------\n"
                   f"🔹 **شراء من:** `{signal['buy_exchange'].upper()}` بسعر `{format_price(buy_price)}`\n"
                   f"🔸 **بيع في:** `{signal['sell_exchange'].upper()}` بسعر `{format_price(sell_price)}`\n\n"
                   f"💰 **الربح الصافي المحتمل:** **`{profit:+.2f}%`**\n\n"
                   f"*ملاحظة: هذه الفرص لحظية وتتطلب التنفيذ السريع.*")
    else:
        title = f"**💡 توصية يدوية | {signal['symbol']}**"
        entry, tp, sl = signal['entry_price'], signal['take_profit'], signal['stop_loss']
        tp_percent = ((tp - entry) / entry * 100) if entry > 0 else 0
        sl_percent = ((entry - sl) / entry * 100) if entry > 0 else 0
        reasons_en = signal.get('reason', 'N/A').split(' + ')
        reasons_ar = ' + '.join([STRATEGY_NAMES_AR.get(r, r) for r in reasons_en])
        message = (f"**Manual Signal | توصية يدوية**\n------------------------------------\n{title}\n------------------------------------\n"
                   f"🔹 **المنصة:** {signal['exchange'].upper()}\n"
                   f"⭐ **قوة الإشارة:** {strength_stars}\n"
                   f"🔍 **الاستراتيجية:** {reasons_ar}\n\n"
                   f"📈 **نقطة الدخول:** `{format_price(entry)}`\n"
                   f"🎯 **الهدف:** `{format_price(tp)}` (+{tp_percent:.2f}%)\n"
                   f"🛑 **الوقف:** `{format_price(sl)}` (-{sl_percent:.2f}%)")
    try:
        await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Sent manual recommendation for {signal['symbol']} to Telegram channel.")
    except Exception as e:
        logger.error(f"Failed to send Telegram recommendation: {e}")

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    async with scan_lock:
        logger.info("--- Brain starting new scan cycle ---")
        settings = brain_state.settings
        mood = await get_market_mood() # Placeholder
        brain_state.market_mood = mood
        if settings['market_mood_filter_enabled'] and mood['mood'] in ["NEGATIVE", "DANGEROUS"]:
            logger.warning(f"SCAN SKIPPED: Market mood is {mood['mood']}. Reason: {mood['reason']}")
            await context.bot.send_message(TELEGRAM_CHAT_ID, f"🚨 **تنبيه: فحص السوق تم إيقافه!**\n**السبب:** {mood['reason']}")
            return

        # --- [Logic unchanged] Fetching and filtering symbols ---
        top_symbols = await aggregate_top_movers() # Placeholder
        # ... and so on

        all_signals = [{"symbol": "BTC/USDT", "exchange": "okx", "reason": "momentum_breakout", "entry_price": 70000, "take_profit": 72000, "stop_loss": 69000, "strength": 2, "weight": 1.1},
                       {"symbol": "ETH/USDT", "exchange": "binance", "reason": "support_rebound", "entry_price": 3500, "take_profit": 3600, "stop_loss": 3450, "strength": 1, "weight": 1.0}] # Placeholder for actual scan results

        logger.info(f"Scan complete. Found {len(all_signals)} potential signals.")
        brain_state.scan_history.append(len(all_signals))

        for signal in all_signals:
            exchange_id = signal.get('exchange') or signal.get('buy_exchange') # Works for both TA and Arbitrage
            if not exchange_id: continue

            # --- [THE NEW CORE LOGIC] ---
            execution_mode = settings.get('execution_modes', {}).get(exchange_id, 'DISABLED')
            
            if execution_mode == 'AUTOMATIC':
                try:
                    # نشر الإشارة إلى Redis لليد الآلية
                    await brain_state.redis_publisher.publish(REDIS_SIGNAL_CHANNEL, json.dumps(signal))
                    logger.info(f"Brain published AUTOMATIC signal to Redis: {signal['symbol']} on {exchange_id}")
                    await context.bot.send_message(TELEGRAM_CHAT_ID, f"🧠 **العقل أرسل إشارة آلية إلى يد {exchange_id.upper()}**\n`{signal['symbol']}` - `{signal['reason']}`")
                except Exception as e:
                    logger.error(f"Failed to publish signal to Redis: {e}")
            
            elif execution_mode == 'MANUAL':
                # إرسال توصية يدوية إلى قناة التليجرام
                await send_telegram_recommendation(context.bot, signal)

            # If mode is 'DISABLED', do nothing.
            
            await asyncio.sleep(0.5) # To avoid spamming

# --- واجهة تليجرام ---
async def show_execution_modes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the menu for managing execution modes."""
    query = update.callback_query
    settings = brain_state.settings
    modes = settings.get('execution_modes', {})
    
    keyboard = []
    mode_map = {"AUTOMATIC": "✅ تلقائي", "MANUAL": " manual", "DISABLED": "❌ معطل"}
    
    for ex_id in EXCHANGES_TO_SCAN:
        current_mode = modes.get(ex_id, "DISABLED")
        button_text = f"{ex_id.upper()}: {mode_map[current_mode]}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"mode_cycle_{ex_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="settings_main")])
    
    message_text = "🔧 **أوضاع التنفيذ للمنصات**\n\nاختر منصة لتبديل وضعها (تلقائي, يدوي, معطل):"
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def handle_cycle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycles the execution mode for a given exchange."""
    query = update.callback_query
    ex_id = query.data.split('_')[-1]
    
    modes_cycle = ["AUTOMATIC", "MANUAL", "DISABLED"]
    current_mode = brain_state.settings['execution_modes'].get(ex_id, "DISABLED")
    current_index = modes_cycle.index(current_mode)
    new_index = (current_index + 1) % len(modes_cycle)
    new_mode = modes_cycle[new_index]
    
    brain_state.settings['execution_modes'][ex_id] = new_mode
    save_settings()
    
    await query.answer(f"تم تغيير وضع {ex_id.upper()} إلى {new_mode}")
    await show_execution_modes_menu(update, context) # Refresh the menu


# --- [Modified] Main Settings Menu ---
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🤖 أوضاع التنفيذ", callback_data="settings_modes")], # NEW
        [InlineKeyboardButton("🧠 إعدادات الذكاء التكيفي", callback_data="settings_adaptive")],
        [InlineKeyboardButton("🎛️ تعديل المعايير المتقدمة", callback_data="settings_params")],
        [InlineKeyboardButton("🔭 تفعيل/تعطيل الماسحات", callback_data="settings_scanners")],
        # ... other settings buttons
    ]
    message_text = "⚙️ *الإعدادات الرئيسية*\n\nاختر فئة الإعدادات التي تريد تعديلها."
    target_message = update.message or update.callback_query.message
    if update.callback_query:
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


# --- [Modified] Button Handler ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    
    if data == "settings_modes":
        await show_execution_modes_menu(update, context)
    elif data.startswith("mode_cycle_"):
        await handle_cycle_mode(update, context)
    # ... handle all other callbacks
    else:
        # Placeholder for other button handlers
        await query.message.reply_text(f"Button '{data}' pressed.")

# The rest of the main, post_init, etc. functions remain the same
# but with the added button handlers.

# --- نقطة انطلاق العقل ---
async def post_init(application: Application):
    brain_state.application = application
    load_settings()
    await init_database()
    if not await initialize_redis(): return
    await initialize_exchanges()
    if not brain_state.exchanges:
        logger.critical("No exchanges connected. Brain cannot operate."); return

    jq = application.job_queue
    jq.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name="perform_scan")
    jq.run_repeating(update_strategy_performance, interval=STRATEGY_ANALYSIS_INTERVAL_SECONDS, first=60, name="update_strategy_performance")
    jq.run_repeating(propose_strategy_changes, interval=STRATEGY_ANALYSIS_INTERVAL_SECONDS + 300, first=120, name="propose_strategy_changes")

    logger.info("--- Brain is fully operational and jobs are scheduled ---")
    await application.bot.send_message(TELEGRAM_CHAT_ID, "*🧠 العقل الخارق | v1.2 - بدأ العمل...*")

def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("FATAL ERROR: Please set your Telegram Token and Chat ID."); return
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start_command)) # Add start command
    # Add the generic button handler
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    # ... add other handlers

    application.run_polling()

if __name__ == '__main__':
    main()

