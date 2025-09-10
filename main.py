# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة --- #
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import re
import time
import sqlite3
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import deque, Counter, defaultdict

# [UPGRADE] المكتبات الجديدة لتحليل الأخبار
import feedparser
try:
    import nltk
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False
    logging.warning("Library 'nltk' not found. Sentiment analysis will be disabled.")

import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest, RetryAfter, TimedOut

try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logging.warning("Library 'scipy' not found. RSI Divergence strategy will be disabled.")


# --- الإعدادات الأساسية --- #
# !!! هام: قم بتعيين هذه المتغيرات في بيئة التشغيل الخاصة بك
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)
ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', 'YOUR_AV_KEY_HERE')

# --- [تداول حقيقي] إعدادات مفاتيح Binance API --- #
# !!! هام جدًا: لا تكتب مفاتيحك هنا مباشرة. استخدم متغيرات البيئة.
# قم بتعيينها في نظامك كالتالي:
# export BINANCE_API_KEY="your_api_key"
# export BINANCE_API_SECRET="your_secret_key"
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', 'YOUR_BINANCE_SECRET_KEY')


if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()
if ALPHA_VANTAGE_API_KEY == 'YOUR_AV_KEY_HERE':
    logging.warning("Alpha Vantage API key not set. Economic calendar will be disabled.")


# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
HIGHER_TIMEFRAME = '1h'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120

APP_ROOT = '.'
# [تحديث] تم تحديث الإصدار إلى v5.1 (تداول حقيقي)
DB_FILE = os.path.join(APP_ROOT, 'trading_bot_v5_real.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'settings_v5_real.json')


EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) --- #
LOG_FILE = os.path.join(APP_ROOT, 'bot_v5_real.log')
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler(LOG_FILE, 'a'), logging.StreamHandler()])
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)
logger = logging.getLogger("TradingBot")


# --- Preset Configurations ---
PRESET_PRO = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 1000000, "max_spread_percent": 0.45, "rvol_period": 18, "min_rvol": 1.5},
  "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.85},
  "ema_trend_filter": {"enabled": True, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 1.1, "min_sl_percent": 0.6}
}
PRESET_LAX = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 400000, "max_spread_percent": 1.3, "rvol_period": 12, "min_rvol": 1.1},
  "volatility_filters": {"atr_period_for_filter": 10, "min_atr_percent": 0.3},
  "ema_trend_filter": {"enabled": False, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 0.4, "min_sl_percent": 0.2}
}
PRESET_STRICT = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 2500000, "max_spread_percent": 0.22, "rvol_period": 25, "min_rvol": 2.2},
  "volatility_filters": {"atr_period_for_filter": 20, "min_atr_percent": 1.4},
  "ema_trend_filter": {"enabled": True, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 1.8, "min_sl_percent": 0.9}
}
PRESET_VERY_LAX = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 200000, "max_spread_percent": 2.0, "rvol_period": 10, "min_rvol": 0.8},
  "volatility_filters": {"atr_period_for_filter": 10, "min_atr_percent": 0.2},
  "ema_trend_filter": {"enabled": False, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 0.3, "min_sl_percent": 0.15}
}
PRESETS = {"PRO": PRESET_PRO, "LAX": PRESET_LAX, "STRICT": PRESET_STRICT, "VERY_LAX": PRESET_VERY_LAX}

# [تحديث واجهة المستخدم] ترجمة أسماء الاستراتيجيات
STRATEGY_NAMES_AR = {
    "Momentum Breakout": "زخم اختراقي",
    "Squeeze Breakout": "اختراق انضغاطي",
    "RSI Divergence": "دايفرجنس RSI",
    "Supertrend Flip": "انعكاس سوبرترند"
}

# --- Constants for Interactive Settings menu ---
EDITABLE_PARAMS = {
    "إعدادات عامة": [
        "max_concurrent_trades", "top_n_symbols_by_volume", "concurrent_workers",
        "min_signal_strength"
    ],
    "إعدادات المخاطر": [
        "REAL_TRADING_ENABLED", # [تداول حقيقي] إضافة مفتاح التبديل
        "trade_size_usdt", "atr_sl_multiplier", "risk_reward_ratio",
        "trailing_sl_activate_percent", "trailing_sl_percent"
    ],
    "الفلاتر والاتجاه": [
        "market_regime_filter_enabled", "use_master_trend_filter", "fear_and_greed_filter_enabled",
        "master_adx_filter_level", "master_trend_filter_ma_period", "trailing_sl_enabled", "fear_and_greed_threshold",
        "fundamental_analysis_enabled"
    ]
}
PARAM_DISPLAY_NAMES = {
    "REAL_TRADING_ENABLED": "وضع التداول الحقيقي", # [تداول حقيقي]
    "trade_size_usdt": "حجم الصفقة (USDT)", # [تداول حقيقي] تم التغيير من نسبة مئوية
    "max_concurrent_trades": "أقصى عدد للصفقات",
    "top_n_symbols_by_volume": "عدد العملات للفحص",
    "concurrent_workers": "عمال الفحص المتزامنين",
    "min_signal_strength": "أدنى قوة للإشارة",
    "atr_sl_multiplier": "مضاعف وقف الخسارة (ATR)",
    "risk_reward_ratio": "نسبة المخاطرة/العائد",
    "trailing_sl_activate_percent": "تفعيل الوقف المتحرك (%)",
    "trailing_sl_percent": "مسافة الوقف المتحرك (%)",
    "market_regime_filter_enabled": "فلتر وضع السوق (فني)",
    "use_master_trend_filter": "فلتر الاتجاه العام (BTC)",
    "master_adx_filter_level": "مستوى فلتر ADX",
    "master_trend_filter_ma_period": "فترة فلتر الاتجاه",
    "trailing_sl_enabled": "تفعيل الوقف المتحرك",
    "fear_and_greed_filter_enabled": "فلتر الخوف والطمع",
    "fear_and_greed_threshold": "حد مؤشر الخوف",
    "fundamental_analysis_enabled": "فلتر الأخبار والبيانات",
}


# --- Global Bot State ---
bot_data = {
    "exchanges": {},
    "last_signal_time": {},
    "settings": {},
    "status_snapshot": {
        "last_scan_start_time": "N/A", "last_scan_end_time": "N/A",
        "markets_found": 0, "signals_found": 0, "active_trades_count": 0,
        "scan_in_progress": False, "btc_market_mood": "غير محدد",
        "trading_mode": "وهمي 📝" # [تداول حقيقي]
    },
    "scan_history": deque(maxlen=10)
}
scan_lock = asyncio.Lock()

# --- Settings Management ---
DEFAULT_SETTINGS = {
    "REAL_TRADING_ENABLED": False, # [تداول حقيقي] معطل افتراضياً للسلامة
    "trade_size_usdt": 20.0, # [تداول حقيقي] حجم الصفقة ثابت بالدولار
    "virtual_portfolio_balance_usdt": 1000.0, "virtual_trade_size_percentage": 5.0, # [تداول حقيقي] الإعدادات القديمة محفوظة للوضع الوهمي
    "max_concurrent_trades": 5, "top_n_symbols_by_volume": 250, "concurrent_workers": 10,
    "market_regime_filter_enabled": True, "fundamental_analysis_enabled": True,
    "active_scanners": ["momentum_breakout", "breakout_squeeze_pro", "rsi_divergence", "supertrend_pullback"],
    "use_master_trend_filter": True, "master_trend_filter_ma_period": 50, "master_adx_filter_level": 22,
    "fear_and_greed_filter_enabled": True, "fear_and_greed_threshold": 30,
    "use_dynamic_risk_management": True, "atr_period": 14, "atr_sl_multiplier": 2.0, "risk_reward_ratio": 1.5,
    "take_profit_percentage": 4.0, "stop_loss_percentage": 2.0, "trailing_sl_enabled": True, "trailing_sl_activate_percent": 2.0, "trailing_sl_percent": 1.5,
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68, "volume_spike_multiplier": 1.5},
    "breakout_squeeze_pro": {"bbands_period": 20, "bbands_stddev": 2.0, "keltner_period": 20, "keltner_atr_multiplier": 1.5, "volume_confirmation_enabled": True},
    "rsi_divergence": {"rsi_period": 14, "lookback_period": 35, "peak_trough_lookback": 5, "confirm_with_rsi_exit": True},
    "supertrend_pullback": {"atr_period": 10, "atr_multiplier": 3.0, "swing_high_lookback": 10},
    "liquidity_filters": {"min_quote_volume_24h_usd": 1_000_000, "max_spread_percent": 0.5, "rvol_period": 20, "min_rvol": 1.5},
    "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.8},
    "stablecoin_filter": {"exclude_bases": ["USDT","USDC","DAI","FDUSD","TUSD","USDE","PYUSD","GUSD","EURT","USDJ"]},
    "ema_trend_filter": {"enabled": True, "ema_period": 200},
    "min_tp_sl_filter": {"min_tp_percent": 1.0, "min_sl_percent": 0.5},
    "min_signal_strength": 1,
    "active_preset_name": "PRO",
    "last_market_mood": {"timestamp": "N/A", "mood": "UNKNOWN", "reason": "No scan performed yet."},
    "last_suggestion_time": 0
}


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
            updated = False
            for key, value in DEFAULT_SETTINGS.items():
                if key not in bot_data["settings"]:
                    bot_data["settings"][key] = value; updated = True
                elif isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if sub_key not in bot_data["settings"].get(key, {}):
                            bot_data["settings"][key][sub_key] = sub_value; updated = True
            if updated: save_settings()
        else:
            bot_data["settings"] = DEFAULT_SETTINGS.copy()
            save_settings()
        
        # [تداول حقيقي] تحديث حالة البوت عند التحميل
        mode = "حقيقي 🟢" if bot_data["settings"].get("REAL_TRADING_ENABLED") else "وهمي 📝"
        bot_data['status_snapshot']['trading_mode'] = mode
        logger.info(f"Settings loaded successfully. Current Trading Mode: {mode}")

    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        bot_data["settings"] = DEFAULT_SETTINGS.copy()


def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
        logger.info(f"Settings saved successfully to {SETTINGS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")

# --- Database Management ---
def init_database():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        # [تداول حقيقي] إضافة حقول لأرقام الأوامر الحقيقية
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                exchange TEXT,
                symbol TEXT,
                entry_price REAL,
                take_profit REAL,
                stop_loss REAL,
                quantity REAL,
                entry_value_usdt REAL,
                status TEXT,
                exit_price REAL,
                closed_at TEXT,
                exit_value_usdt REAL,
                pnl_usdt REAL,
                trailing_sl_active BOOLEAN,
                highest_price REAL,
                reason TEXT,
                is_real_trade BOOLEAN DEFAULT FALSE,
                entry_order_id TEXT,
                sl_tp_order_id TEXT
            )
        ''')
        # Check and add columns if they don't exist (for migration)
        cols = [i[1] for i in cursor.execute("PRAGMA table_info(trades)").fetchall()]
        if 'is_real_trade' not in cols:
            cursor.execute('ALTER TABLE trades ADD COLUMN is_real_trade BOOLEAN DEFAULT FALSE')
        if 'entry_order_id' not in cols:
            cursor.execute('ALTER TABLE trades ADD COLUMN entry_order_id TEXT')
        if 'sl_tp_order_id' not in cols:
            cursor.execute('ALTER TABLE trades ADD COLUMN sl_tp_order_id TEXT')

        conn.commit()
        conn.close()
        logger.info(f"Database initialized successfully at: {DB_FILE}")
    except Exception as e:
        logger.error(f"Failed to initialize database at {DB_FILE}: {e}")

def log_trade_to_db(signal, is_real=False, order_ids=None):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        
        entry_order_id = order_ids.get('entry_order_id') if order_ids else None
        sl_tp_order_id = order_ids.get('sl_tp_order_id') if order_ids else None

        cursor.execute('''
            INSERT INTO trades (
                timestamp, exchange, symbol, entry_price, take_profit, stop_loss,
                quantity, entry_value_usdt, status, trailing_sl_active, highest_price,
                reason, is_real_trade, entry_order_id, sl_tp_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'],
            signal['entry_price'], signal['take_profit'], signal['stop_loss'],
            signal['quantity'], signal['entry_value_usdt'], 'نشطة', False, signal['entry_price'],
            signal['reason'], is_real, entry_order_id, sl_tp_order_id
        ))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    except Exception as e:
        logger.error(f"Failed to log trade to DB: {e}")
        return None

# --- [تداول حقيقي] وظيفة تنفيذ الصفقة الحقيقية --- #
async def execute_real_trade_on_binance(signal, context: ContextTypes.DEFAULT_TYPE):
    """
    Attempts to execute a full trade (market buy + OCO sell for TP/SL) on Binance.
    """
    settings = bot_data['settings']
    binance = bot_data['exchanges'].get('binance')
    symbol = signal['symbol']
    trade_size_usdt = settings['trade_size_usdt']

    if not binance:
        return {"success": False, "message": "Binance exchange not connected."}
    
    try:
        # 1. التحقق من رصيد USDT
        balance = await binance.fetch_balance()
        usdt_balance = balance['total'].get('USDT', 0)
        if usdt_balance < trade_size_usdt:
            msg = f"رصيد غير كافٍ. مطلوب: {trade_size_usdt} USDT, المتاح: {usdt_balance:.2f} USDT."
            logger.error(msg)
            return {"success": False, "message": msg}

        # 2. جلب معلومات السوق للحصول على دقة الأسعار والكميات
        markets = await binance.load_markets()
        market = binance.market(symbol)
        
        # 3. حساب الكمية للشراء
        # We buy slightly less to account for price fluctuations and fees
        amount_to_buy = (trade_size_usdt / signal['entry_price']) * 0.995 
        quantity = binance.amount_to_precision(symbol, amount_to_buy)

        # 4. تنفيذ أمر الشراء (Market Buy)
        logger.info(f"REAL TRADE: Placing MARKET BUY for {quantity} {symbol}")
        buy_order = await binance.create_market_buy_order(symbol, quantity)
        logger.info(f"REAL TRADE: Market buy order placed successfully. Order ID: {buy_order['id']}")
        
        await asyncio.sleep(2) # انتظار بسيط للتأكد من تنفيذ الأمر

        # 5. جلب تفاصيل أمر الشراء المنفذ للحصول على السعر والكمية الفعلية
        filled_order = await binance.fetch_order(buy_order['id'], symbol)
        if not filled_order or filled_order['status'] != 'closed':
            msg = "فشل التحقق من تنفيذ أمر الشراء."
            logger.error(msg)
            return {"success": False, "message": msg}
        
        actual_quantity = filled_order['filled']
        actual_entry_price = filled_order['average']
        signal.update({
            'quantity': actual_quantity, 
            'entry_price': actual_entry_price,
            'entry_value_usdt': filled_order['cost']
        })

        # 6. تجهيز ووضع أمر OCO (أمر يلغي الآخر) للهدف والوقف
        tp_price = binance.price_to_precision(symbol, signal['take_profit'])
        sl_price = binance.price_to_precision(symbol, signal['stop_loss'])
        # سعر الإيقاف لـ stop_loss_limit يجب أن يكون نفس سعر الوقف أو أعلى قليلاً
        sl_trigger_price = binance.price_to_precision(symbol, signal['stop_loss'])

        logger.info(f"REAL TRADE: Placing OCO SELL for {actual_quantity} {symbol} -> TP: {tp_price}, SL: {sl_trigger_price}")
        
        params = {'stopLimitPrice': sl_price} # Required for STOP_LOSS_LIMIT
        oco_order = await binance.create_order(
            symbol,
            'oco',
            'sell',
            actual_quantity,
            price=tp_price, #
            stopPrice=sl_trigger_price,
            params=params
        )
        
        logger.info(f"REAL TRADE: OCO order placed successfully. List Order ID: {oco_order['orderListId']}")

        return {
            "success": True, 
            "message": "تم تنفيذ الصفقة الحقيقية بنجاح.",
            "order_ids": {
                "entry_order_id": buy_order['id'],
                "sl_tp_order_id": oco_order['orderListId'] # For OCO, we track the list ID
            },
            "filled_signal": signal
        }

    except ccxt.InsufficientFunds as e:
        logger.error(f"REAL TRADE ERROR for {symbol}: Insufficient funds. {e}")
        return {"success": False, "message": f"رصيد USDT غير كافٍ. {e}"}
    except ccxt.NetworkError as e:
        logger.error(f"REAL TRADE ERROR for {symbol}: Network error. {e}")
        return {"success": False, "message": f"خطأ في الشبكة. {e}"}
    except ccxt.ExchangeError as e:
        logger.error(f"REAL TRADE ERROR for {symbol}: Exchange error. {e}")
        return {"success": False, "message": f"خطأ من المنصة: {e}"}
    except Exception as e:
        logger.critical(f"CRITICAL REAL TRADE ERROR for {symbol}: {e}", exc_info=True)
        return {"success": False, "message": f"خطأ فادح وغير متوقع. {e}"}


# --- Fundamental & News Analysis Section (No changes needed) ---
def get_alpha_vantage_economic_events():
    if ALPHA_VANTAGE_API_KEY == 'YOUR_AV_KEY_HERE':
        logger.warning("Alpha Vantage API key is not set. Skipping economic calendar check.")
        return []
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    params = {'function': 'ECONOMIC_CALENDAR', 'horizon': '3month', 'apikey': ALPHA_VANTAGE_API_KEY}
    try:
        response = requests.get('https://www.alphavantage.co/query', params=params, timeout=20)
        response.raise_for_status()
        data_str = response.text
        if "premium" in data_str.lower():
             logger.error("Alpha Vantage API returned a premium feature error for Economic Calendar.")
             return []
        lines = data_str.strip().split('\r\n')
        if len(lines) < 2: return []
        header = [h.strip() for h in lines[0].split(',')]
        high_impact_events = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split(',')]
            event = dict(zip(header, values))
            if event.get('releaseDate', '') == today_str and event.get('impact', '').lower() == 'high' and event.get('country', '') in ['USD', 'EUR']:
                high_impact_events.append(event.get('event', 'Unknown Event'))
        if high_impact_events: logger.warning(f"High-impact events today via Alpha Vantage: {high_impact_events}")
        return high_impact_events
    except requests.RequestException as e:
        logger.error(f"Failed to fetch economic calendar data from Alpha Vantage: {e}")
        return None

def get_latest_crypto_news(limit=15):
    urls = ["https://cointelegraph.com/rss", "https://www.coindesk.com/arc/outboundfeeds/rss/"]
    headlines = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            headlines.extend(entry.title for entry in feed.entries[:5])
        except Exception as e:
            logger.error(f"Failed to fetch news from {url}: {e}")
    return list(set(headlines))[:limit]

def analyze_sentiment_of_headlines(headlines):
    if not headlines or not NLTK_AVAILABLE: return 0.0
    sia = SentimentIntensityAnalyzer()
    total_compound_score = sum(sia.polarity_scores(headline)['compound'] for headline in headlines)
    return total_compound_score / len(headlines) if headlines else 0.0

async def get_fundamental_market_mood():
    high_impact_events = get_alpha_vantage_economic_events()
    if high_impact_events is None: return "DANGEROUS", -1.0, "فشل جلب البيانات الاقتصادية"
    if high_impact_events: return "DANGEROUS", -0.9, f"أحداث هامة اليوم: {', '.join(high_impact_events)}"
    latest_headlines = get_latest_crypto_news()
    sentiment_score = analyze_sentiment_of_headlines(latest_headlines)
    logger.info(f"Market sentiment score based on news: {sentiment_score:.2f}")
    if sentiment_score > 0.25: return "POSITIVE", sentiment_score, f"مشاعر إيجابية (الدرجة: {sentiment_score:.2f})"
    elif sentiment_score < -0.25: return "NEGATIVE", sentiment_score, f"مشاعر سلبية (الدرجة: {sentiment_score:.2f})"
    else: return "NEUTRAL", sentiment_score, f"مشاعر محايدة (الدرجة: {sentiment_score:.2f})"


# --- Advanced Scanners (No changes needed) ---
def find_col(df_columns, prefix):
    try: return next(col for col in df_columns if col.startswith(prefix))
    except StopIteration: return None
def analyze_momentum_breakout(df, params, rvol, adx_value):
    df.ta.vwap(append=True)
    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
    df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True)
    df.ta.rsi(length=params['rsi_period'], append=True)
    macd_col, macds_col, bbu_col, rsi_col = (
        find_col(df.columns, f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"),
        find_col(df.columns, f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}"),
        find_col(df.columns, f"BBU_{params['bbands_period']}_"),
        find_col(df.columns, f"RSI_{params['rsi_period']}")
    )
    if not all([macd_col, macds_col, bbu_col, rsi_col]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    rvol_ok = rvol >= bot_data['settings']['liquidity_filters']['min_rvol']
    if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and
        last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and
        last[rsi_col] < params['rsi_max_level'] and rvol_ok):
        return {"reason": "Momentum Breakout", "type": "long"}
    return None
def analyze_breakout_squeeze_pro(df, params, rvol, adx_value):
    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
    df.ta.kc(length=params['keltner_period'], scalar=params['keltner_atr_multiplier'], append=True)
    df.ta.obv(append=True)
    bbu_col, bbl_col, kcu_col, kcl_col = (
        find_col(df.columns, f"BBU_{params['bbands_period']}_"), find_col(df.columns, f"BBL_{params['bbands_period']}_"),
        find_col(df.columns, f"KCUe_{params['keltner_period']}_"), find_col(df.columns, f"KCLEe_{params['keltner_period']}_")
    )
    if not all([bbu_col, bbl_col, kcu_col, kcl_col]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    is_in_squeeze = prev[bbl_col] > prev[kcl_col] and prev[bbu_col] < prev[kcu_col]
    if is_in_squeeze:
        breakout_fired = last['close'] > last[bbu_col]
        volume_ok = not params.get('volume_confirmation_enabled', True) or last['volume'] > df['volume'].rolling(20).mean().iloc[-2] * 1.5
        rvol_ok = rvol >= bot_data['settings']['liquidity_filters']['min_rvol']
        obv_rising = df['OBV'].iloc[-2] > df['OBV'].iloc[-3]
        if breakout_fired and rvol_ok and obv_rising:
            if params.get('volume_confirmation_enabled', True) and not volume_ok: return None
            return {"reason": "Squeeze Breakout", "type": "long"}
    return None
def analyze_rsi_divergence(df, params, rvol, adx_value):
    if not SCIPY_AVAILABLE: return None
    df.ta.rsi(length=params['rsi_period'], append=True)
    rsi_col = find_col(df.columns, f"RSI_{params['rsi_period']}")
    if not rsi_col or df[rsi_col].isnull().all(): return None
    subset = df.iloc[-params['lookback_period']:].copy()
    price_troughs_idx, _ = find_peaks(-subset['low'], distance=params['peak_trough_lookback'])
    rsi_troughs_idx, _ = find_peaks(-subset[rsi_col], distance=params['peak_trough_lookback'])
    if len(price_troughs_idx) >= 2 and len(rsi_troughs_idx) >= 2:
        p_low1_idx, p_low2_idx = price_troughs_idx[-2], price_troughs_idx[-1]
        r_low1_idx, r_low2_idx = rsi_troughs_idx[-2], rsi_troughs_idx[-1]
        is_divergence = (subset.iloc[p_low2_idx]['low'] < subset.iloc[p_low1_idx]['low'] and
                         subset.iloc[r_low2_idx][rsi_col] > subset.iloc[r_low1_idx][rsi_col])
        if is_divergence:
            rsi_exits_oversold = (subset.iloc[r_low1_idx][rsi_col] < 35 and subset.iloc[-2][rsi_col] > 40)
            confirmation_price = subset.iloc[p_low2_idx:]['high'].max()
            price_confirmed = df.iloc[-2]['close'] > confirmation_price
            if (not params['confirm_with_rsi_exit'] or rsi_exits_oversold) and price_confirmed:
                return {"reason": "RSI Divergence", "type": "long"}
    return None
def analyze_supertrend_pullback(df, params, rvol, adx_value):
    df.ta.supertrend(length=params['atr_period'], multiplier=params['atr_multiplier'], append=True)
    st_dir_col = find_col(df.columns, f"SUPERTd_{params['atr_period']}_")
    ema_col = find_col(df.columns, 'EMA_')
    if not st_dir_col or not ema_col or pd.isna(df[ema_col].iloc[-2]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    if prev[st_dir_col] == -1 and last[st_dir_col] == 1:
        settings = bot_data['settings']
        ema_ok = last['close'] > last[ema_col]
        adx_ok = adx_value >= settings['master_adx_filter_level']
        rvol_ok = rvol >= settings['liquidity_filters']['min_rvol']
        recent_swing_high = df['high'].iloc[-params.get('swing_high_lookback', 10):-2].max()
        breakout_ok = last['close'] > recent_swing_high
        if ema_ok and adx_ok and rvol_ok and breakout_ok:
            return {"reason": "Supertrend Flip", "type": "long"}
    return None

SCANNERS = {
    "momentum_breakout": analyze_momentum_breakout,
    "breakout_squeeze_pro": analyze_breakout_squeeze_pro,
    "rsi_divergence": analyze_rsi_divergence,
    "supertrend_pullback": analyze_supertrend_pullback,
}

# --- Core Bot Functions ---
async def initialize_exchanges():
    settings = bot_data['settings']
    real_trading_enabled = settings.get("REAL_TRADING_ENABLED", False)

    async def connect(ex_id):
        # [تداول حقيقي] إعداد الاتصال مع مفاتيح API إذا كان Binance والتداول الحقيقي مفعلين
        exchange_config = {'enableRateLimit': True, 'options': {'defaultType': 'spot'}}
        if ex_id == 'binance' and real_trading_enabled:
            if BINANCE_API_KEY != 'YOUR_BINANCE_API_KEY' and BINANCE_API_SECRET != 'YOUR_BINANCE_SECRET_KEY':
                exchange_config['apiKey'] = BINANCE_API_KEY
                exchange_config['secret'] = BINANCE_API_SECRET
                logger.info("Binance Real Trading mode: API keys loaded.")
            else:
                logger.error("Binance Real Trading is ENABLED but API keys are not set. Cannot execute real trades.")
        
        exchange = getattr(ccxt, ex_id)(exchange_config)
        try:
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logger.info(f"Connected to {ex_id} (spot markets only).")
        except Exception as e:
            logger.error(f"Failed to connect to {ex_id}: {e}")
            await exchange.close()

    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    all_tickers = []
    async def fetch(ex_id, ex):
        try: return [dict(t, exchange=ex_id) for t in (await ex.fetch_tickers()).values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()])
    for res in results: all_tickers.extend(res)
    settings = bot_data['settings']
    excluded_bases = settings['stablecoin_filter']['exclude_bases']
    min_volume = settings['liquidity_filters']['min_quote_volume_24h_usd']
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].upper().endswith('/USDT') and t['symbol'].split('/')[0] not in excluded_bases and t.get('quoteVolume', 0) >= min_volume and not any(k in t['symbol'].upper() for k in ['UP','DOWN','3L','3S','BEAR','BULL'])]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0), reverse=True)
    unique_symbols = {t['symbol']: {'exchange': t['exchange'], 'symbol': t['symbol']} for t in sorted_tickers}
    final_list = list(unique_symbols.values())[:settings['top_n_symbols_by_volume']]
    logger.info(f"Aggregated markets. Found {len(all_tickers)} tickers -> Post-filter: {len(usdt_tickers)} -> Selected top {len(final_list)} unique pairs.")
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    return final_list

async def get_higher_timeframe_trend(exchange, symbol, ma_period):
    try:
        ohlcv_htf = await exchange.fetch_ohlcv(symbol, HIGHER_TIMEFRAME, limit=ma_period + 5)
        if len(ohlcv_htf) < ma_period: return None, "Not enough HTF data"
        df_htf = pd.DataFrame(ohlcv_htf, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_htf[f'SMA_{ma_period}'] = ta.sma(df_htf['close'], length=ma_period)
        last_candle = df_htf.iloc[-1]
        is_bullish = last_candle['close'] > last_candle[f'SMA_{ma_period}']
        return is_bullish, "Bullish" if is_bullish else "Bearish"
    except Exception as e:
        return None, f"Error: {e}"

async def worker(queue, results_list, settings, failure_counter):
    while not queue.empty():
        market_info = await queue.get()
        symbol = market_info.get('symbol', 'N/A')
        exchange_id = market_info.get('exchange', 'N/A')
        exchange = bot_data["exchanges"].get(exchange_id)
        
        # [تداول حقيقي] الفحص يقتصر على Binance فقط في حال تفعيل التداول الحقيقي
        if settings.get("REAL_TRADING_ENABLED") and exchange_id != 'binance':
            queue.task_done()
            continue

        if not exchange or not settings.get('active_scanners'):
            queue.task_done()
            continue
        try:
            liq_filters, vol_filters, ema_filters = settings['liquidity_filters'], settings['volatility_filters'], settings['ema_trend_filter']

            orderbook = await exchange.fetch_order_book(symbol, limit=20)
            if not orderbook or not orderbook['bids'] or not orderbook['asks']:
                logger.debug(f"Reject {symbol}: Could not fetch order book."); continue

            best_bid, best_ask = orderbook['bids'][0][0], orderbook['asks'][0][0]
            if best_bid <= 0: logger.debug(f"Reject {symbol}: Invalid bid price."); continue

            spread_percent = ((best_ask - best_bid) / best_bid) * 100
            if spread_percent > liq_filters['max_spread_percent']:
                logger.debug(f"Reject {symbol}: High Spread ({spread_percent:.2f}% > {liq_filters['max_spread_percent']}%)"); continue

            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=220)
            if len(ohlcv) < ema_filters['ema_period']:
                logger.debug(f"Skipping {symbol}: Not enough data ({len(ohlcv)} candles) for EMA calculation."); continue

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df.set_index(pd.to_datetime(df['timestamp'], unit='ms'), inplace=True)

            df['volume_sma'] = ta.sma(df['volume'], length=liq_filters['rvol_period'])
            if pd.isna(df['volume_sma'].iloc[-2]) or df['volume_sma'].iloc[-2] <= 0:
                logger.debug(f"Skipping {symbol}: Invalid SMA volume."); continue

            rvol = df['volume'].iloc[-2] / df['volume_sma'].iloc[-2]
            if rvol < liq_filters['min_rvol']:
                logger.debug(f"Reject {symbol}: Low RVOL ({rvol:.2f} < {liq_filters['min_rvol']})"); continue

            atr_col_name = f"ATRr_{vol_filters['atr_period_for_filter']}"
            df.ta.atr(length=vol_filters['atr_period_for_filter'], append=True)
            last_close = df['close'].iloc[-2]
            if last_close <= 0: logger.debug(f"Skipping {symbol}: Invalid close price."); continue

            atr_percent = (df[atr_col_name].iloc[-2] / last_close) * 100
            if atr_percent < vol_filters['min_atr_percent']:
                logger.debug(f"Reject {symbol}: Low ATR% ({atr_percent:.2f}% < {vol_filters['min_atr_percent']}%)"); continue

            ema_col_name = f"EMA_{ema_filters['ema_period']}"
            df.ta.ema(length=ema_filters['ema_period'], append=True)
            if ema_col_name not in df.columns or pd.isna(df[ema_col_name].iloc[-2]):
                logger.debug(f"Skipping {symbol}: EMA_{ema_filters['ema_period']} could not be calculated.")
                continue

            if ema_filters['enabled'] and last_close < df[ema_col_name].iloc[-2]:
                logger.debug(f"Reject {symbol}: Below EMA{ema_filters['ema_period']}"); continue

            if settings.get('use_master_trend_filter'):
                is_htf_bullish, reason = await get_higher_timeframe_trend(exchange, symbol, settings['master_trend_filter_ma_period'])
                if not is_htf_bullish:
                    logger.debug(f"HTF Trend Filter FAILED for {symbol}: {reason}"); continue

            df.ta.adx(append=True)
            adx_col = find_col(df.columns, 'ADX_')
            adx_value = df[adx_col].iloc[-2] if adx_col and pd.notna(df[adx_col].iloc[-2]) else 0
            if settings.get('use_master_trend_filter') and adx_value < settings['master_adx_filter_level']:
                logger.debug(f"ADX Filter FAILED for {symbol}: {adx_value:.2f} < {settings['master_adx_filter_level']}"); continue

            confirmed_reasons = [result['reason'] for scanner_name in settings['active_scanners'] if (result := SCANNERS[scanner_name](df.copy(), settings.get(scanner_name, {}), rvol, adx_value)) and result.get("type") == "long"]

            if confirmed_reasons and len(confirmed_reasons) >= settings.get("min_signal_strength", 1):
                reason_str = ' + '.join(confirmed_reasons)
                entry_price = df.iloc[-2]['close']
                df.ta.atr(length=settings['atr_period'], append=True)
                current_atr = df.iloc[-2].get(find_col(df.columns, f"ATRr_{settings['atr_period']}"), 0)
                if settings.get("use_dynamic_risk_management", False) and current_atr > 0:
                    risk_per_unit = current_atr * settings['atr_sl_multiplier']
                    stop_loss, take_profit = entry_price - risk_per_unit, entry_price + (risk_per_unit * settings['risk_reward_ratio'])
                else:
                    stop_loss, take_profit = entry_price * (1 - settings['stop_loss_percentage'] / 100), entry_price * (1 + settings['take_profit_percentage'] / 100)
                tp_percent, sl_percent = ((take_profit - entry_price) / entry_price * 100), ((entry_price - stop_loss) / entry_price * 100)
                min_filters = settings['min_tp_sl_filter']
                if tp_percent >= min_filters['min_tp_percent'] and sl_percent >= min_filters['min_sl_percent']:
                    results_list.append({"symbol": symbol, "exchange": market_info['exchange'].capitalize(), "entry_price": entry_price, "take_profit": take_profit, "stop_loss": stop_loss, "timestamp": df.index[-2], "reason": reason_str, "strength": len(confirmed_reasons)})
                else:
                    logger.debug(f"Reject {symbol} Signal: Small TP/SL (TP: {tp_percent:.2f}%, SL: {sl_percent:.2f}%)")
        except ccxt.NetworkError as e:
            logger.warning(f"Network error for {symbol}: {e}")
        except Exception as e:
            logger.error(f"CRITICAL ERROR in worker for {symbol}: {e}", exc_info=True)
            failure_counter[0] += 1
        finally:
            queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    async with scan_lock:
        if bot_data['status_snapshot']['scan_in_progress']:
            logger.warning("Scan attempted while another was in progress. Skipped."); return
        settings = bot_data["settings"]
        is_real_trading = settings.get("REAL_TRADING_ENABLED", False)

        if settings.get('fundamental_analysis_enabled', True):
            mood, mood_score, mood_reason = await get_fundamental_market_mood()
            bot_data['settings']['last_market_mood'] = {"timestamp": datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M'), "mood": mood, "reason": mood_reason}
            save_settings()
            logger.info(f"Fundamental Market Mood: {mood} - Reason: {mood_reason}")
            if mood in ["NEGATIVE", "DANGEROUS"]:
                await send_telegram_message(context.bot, {'custom_message': f"**⚠️ تم إيقاف الفحص التلقائي مؤقتاً**\n\n**السبب:** مزاج السوق سلبي/خطر.\n**التفاصيل:** {mood_reason}.\n\n*سيتم استئناف الفحص عندما تتحسن الظروف.*", 'target_chat': TELEGRAM_CHAT_ID}); return
        
        is_market_ok, btc_reason = await check_market_regime()
        bot_data['status_snapshot']['btc_market_mood'] = "إيجابي ✅" if is_market_ok else "سلبي ❌"
        
        if settings.get('market_regime_filter_enabled', True) and not is_market_ok:
            logger.info(f"Skipping scan: {btc_reason}")
            await send_telegram_message(context.bot, {'custom_message': f"**⚠️ تم إيقاف الفحص التلقائي مؤقتاً**\n\n**السبب:** مزاج السوق سلبي/خطر.\n**التفاصيل:** {btc_reason}.\n\n*سيتم استئناف الفحص عندما تتحسن الظروف.*", 'target_chat': TELEGRAM_CHAT_ID}); return
        
        status = bot_data['status_snapshot']
        status.update({"scan_in_progress": True, "last_scan_start_time": datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), "signals_found": 0})
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة' AND is_real_trade = ?", (is_real_trading,))
            active_trades_count = cursor.fetchone()[0]; conn.close()
        except Exception as e:
            logger.error(f"DB Error in perform_scan: {e}"); active_trades_count = settings.get("max_concurrent_trades", 5)
        
        top_markets = await aggregate_top_movers()
        if not top_markets:
            logger.info("Scan complete: No markets to scan."); status['scan_in_progress'] = False; return
        
        queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
        signals, failure_counter = [], [0]
        worker_tasks = [asyncio.create_task(worker(queue, signals, settings, failure_counter)) for _ in range(settings['concurrent_workers'])]
        await queue.join(); [task.cancel() for task in worker_tasks]
        
        signals.sort(key=lambda s: s.get('strength', 0), reverse=True)
        new_trades, opportunities = 0, 0
        last_signal_time = bot_data['last_signal_time']
        
        for signal in signals:
            if time.time() - last_signal_time.get(signal['symbol'], 0) <= (SCAN_INTERVAL_SECONDS * 4):
                logger.info(f"Signal for {signal['symbol']} skipped due to cooldown."); continue
            
            if active_trades_count >= settings.get("max_concurrent_trades", 5):
                await send_telegram_message(context.bot, signal, is_opportunity=True)
                opportunities += 1
                continue
            
            # --- [تداول حقيقي] منطق اتخاذ القرار ---
            if is_real_trading:
                logger.info(f"Executing REAL trade for {signal['symbol']}...")
                trade_result = await execute_real_trade_on_binance(signal, context)

                if trade_result["success"]:
                    filled_signal = trade_result["filled_signal"]
                    if trade_id := log_trade_to_db(filled_signal, is_real=True, order_ids=trade_result["order_ids"]):
                        filled_signal['trade_id'] = trade_id
                        await send_telegram_message(context.bot, filled_signal, is_new=True, is_real=True)
                        active_trades_count += 1
                        new_trades += 1
                else:
                    # إرسال رسالة خطأ للمستخدم
                    error_message = f"**❌ فشل تنفيذ صفقة حقيقية**\n\n- **العملة:** `{signal['symbol']}`\n- **السبب:** `{trade_result['message']}`"
                    await send_telegram_message(context.bot, {'custom_message': error_message, 'target_chat': TELEGRAM_CHAT_ID})
            
            else: # التداول الوهمي
                trade_amount_usdt = settings["virtual_portfolio_balance_usdt"] * (settings["virtual_trade_size_percentage"] / 100)
                signal.update({'quantity': trade_amount_usdt / signal['entry_price'], 'entry_value_usdt': trade_amount_usdt})
                if trade_id := log_trade_to_db(signal, is_real=False):
                    signal['trade_id'] = trade_id
                    await send_telegram_message(context.bot, signal, is_new=True, is_real=False)
                    active_trades_count += 1
                    new_trades += 1

            await asyncio.sleep(0.5)
            last_signal_time[signal['symbol']] = time.time()
        
        failures = failure_counter[0]
        logger.info(f"Scan complete. Found: {len(signals)}, Entered: {new_trades}, Opportunities: {opportunities}, Failures: {failures}.")
        
        scan_duration_str = status['last_scan_start_time']
        scan_duration = (datetime.strptime(datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S') - datetime.strptime(scan_duration_str, '%Y-%m-%d %H:%M:%S')).total_seconds() if scan_duration_str != 'N/A' else 0
        summary_message = (f"**🔬 ملخص الفحص الأخير**\n\n"
                           f"- **وضع التداول:** *{status['trading_mode']}*\n"
                           f"- **الحالة:** اكتمل بنجاح\n"
                           f"- **وضع السوق (BTC):** {status['btc_market_mood']}\n"
                           f"- **المدة:** {scan_duration:.0f} ثانية\n"
                           f"- **العملات المفحوصة:** {len(top_markets)}\n\n"
                           f"- - - - - - - - - - - - - - - - - -\n"
                           f"- **إجمالي الإشارات المكتشفة:** {len(signals)}\n"
                           f"- **✅ صفقات جديدة فُتحت:** {new_trades}\n"
                           f"- **💡 فرص للمراقبة:** {opportunities}\n"
                           f"- **⚠️ أخطاء في التحليل:** {failures}\n"
                           f"- - - - - - - - - - - - - - - - - -\n\n"
                           f"*الفحص التالي مجدول تلقائياً.*")

        await send_telegram_message(context.bot, {'custom_message': summary_message, 'target_chat': TELEGRAM_CHAT_ID})
        
        status['signals_found'] = new_trades + opportunities; status['last_scan_end_time'] = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False
        
        bot_data['scan_history'].append({'signals': len(signals), 'failures': failures})
        await analyze_performance_and_suggest(context)

async def send_telegram_message(bot, signal_data, is_new=False, is_opportunity=False, update_type=None, is_real=False):
    message, keyboard, target_chat = "", None, TELEGRAM_CHAT_ID
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"
    
    if 'custom_message' in signal_data:
        message, target_chat = signal_data['custom_message'], signal_data.get('target_chat', TELEGRAM_CHAT_ID)
        if 'keyboard' in signal_data: keyboard = signal_data['keyboard']
    
    elif is_new or is_opportunity:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        strength_stars = '⭐' * signal_data.get('strength', 1)
        
        # [تداول حقيقي] تخصيص عنوان الرسالة
        mode_icon = "🟢" if is_real else "📝"
        mode_text = "صفقة حقيقية" if is_real else "توصية وهمية"
        title = f"**{mode_icon} {mode_text} | {signal_data['symbol']}**" if is_new else f"**💡 فرصة محتملة | {signal_data['symbol']}**"

        entry, tp, sl = signal_data['entry_price'], signal_data['take_profit'], signal_data['stop_loss']
        tp_percent, sl_percent = ((tp - entry) / entry * 100), ((entry - sl) / entry * 100)
        id_line = f"\n*للمتابعة اضغط: /check {signal_data['trade_id']}*" if is_new else ""
        
        reasons_en = signal_data['reason'].split(' + ')
        reasons_ar = ' + '.join([STRATEGY_NAMES_AR.get(r, r) for r in reasons_en])

        message = (f"* सिग्नल अलर्ट * сигнал тревоги * Signal Alert *\n"
                   f"- - - - - - - - - - - - - - - - - -\n"
                   f"{title}\n"
                   f"- - - - - - - - - - - - - - - - - -\n"
                   f"🔹 **المنصة:** {signal_data['exchange']}\n"
                   f"⭐ **قوة الإشارة:** {strength_stars}\n"
                   f"🔍 **الاستراتيجية:** {reasons_ar}\n\n"
                   f"📈 **نقطة الدخول:** `{format_price(entry)}`\n"
                   f"🎯 **الهدف:** `{format_price(tp)}` (+{tp_percent:.2f}%)\n"
                   f"🛑 **الوقف:** `{format_price(sl)}` (-{sl_percent:.2f}%)"
                   f"{id_line}")
        if is_real and is_new:
            message += "\n\n**تنبيه: تم وضع الأوامر الفعلية في حسابك على Binance.**"

    elif update_type == 'tsl_activation':
        message = (f"**🚀 تأمين الأرباح! | #{signal_data['id']} {signal_data['symbol']}**\n\n"
                   f"تم رفع وقف الخسارة إلى نقطة الدخول.\n"
                   f"**هذه الصفقة الآن مؤمَّنة بالكامل وبدون مخاطرة!**\n\n"
                   f"*دع الأرباح تنمو!*")

    if not message: return
    try:
        await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Failed to send Telegram message to {target_chat}: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        # [تداول حقيقي] متابعة الصفقات الوهمية فقط
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة' AND is_real_trade = 0")
        active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e: logger.error(f"DB error in track_open_trades: {e}"); return

    # [تداول حقيقي] جلب عدد الصفقات الحقيقية والوهمية
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'")
        total_active = cursor.fetchone()[0]
        bot_data['status_snapshot']['active_trades_count'] = total_active
    except Exception as e: logger.error(f"DB error counting active trades: {e}")

    if not active_trades: 
        logger.info("No active PAPER trades to track.")
        return

    async def check_trade(trade):
        exchange = bot_data["exchanges"].get(trade['exchange'].lower())
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None
            
            highest_price = max(trade.get('highest_price', current_price), current_price)
            
            if current_price >= trade['take_profit']: return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price, 'highest_price': highest_price}
            if current_price <= trade['stop_loss']: return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price, 'highest_price': highest_price}
            
            settings = bot_data["settings"]
            
            if settings.get('trailing_sl_enabled', False):
                if not trade.get('trailing_sl_active') and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price']
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade.get('trailing_sl_active'):
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
            
            if highest_price > trade.get('highest_price', 0): return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db, portfolio_pnl = [], 0.0
    for result in filter(None, results):
        original_trade = next((t for t in active_trades if t['id'] == result['id']), None)
        if not original_trade: continue
        status = result['status']
        if status in ['ناجحة', 'فاشلة']:
            pnl_usdt = (result['exit_price'] - original_trade['entry_price']) * original_trade['quantity']
            portfolio_pnl += pnl_usdt
            closed_at_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')
            
            start_dt = datetime.strptime(original_trade['timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=EGYPT_TZ)
            end_dt = datetime.now(EGYPT_TZ)
            duration = end_dt - start_dt
            days, remainder = divmod(duration.total_seconds(), 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, _ = divmod(remainder, 60)
            duration_str = f"{int(days)}d {int(hours)}h {int(minutes)}m" if days > 0 else f"{int(hours)}h {int(minutes)}m"

            updates_to_db.append(("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=?, highest_price=? WHERE id=?", 
                                  (status, result['exit_price'], closed_at_str, result['exit_price'] * original_trade['quantity'], pnl_usdt, result['highest_price'], result['id'])))
            
            highest_price_val = result.get('highest_price', original_trade['entry_price'])
            
            # [تداول حقيقي] تحديد نوع الصفقة في رسالة الإغلاق
            trade_type_text = "صفقة حقيقية 🟢" if original_trade.get('is_real_trade') else "صفقة وهمية 📝"
            
            if status == 'ناجحة':
                message = (f"**📦 إغلاق {trade_type_text} | #{original_trade['id']} {original_trade['symbol']}**\n\n"
                           f"**الحالة: ✅ ناجحة (تهانينا! تم تحقيق الهدف بنجاح)**\n"
                           f"- - - - - - - - - - - - - - - - - -\n"
                           f"💰 **الربح:** `${pnl_usdt:+.2f}` (`{(pnl_usdt / original_trade['entry_value_usdt'] * 100):+.2f}%`)\n"
                           f"- - - - - - - - - - - - - - - - - -\n\n"
                           f"- **سعر الدخول:** `{original_trade['entry_price']}`\n"
                           f"- **سعر الإغلاق:** `{result['exit_price']}`\n"
                           f"- **أعلى قمة وصلت لها:** `{highest_price_val}` (`{((highest_price_val - original_trade['entry_price']) / original_trade['entry_price'] * 100):+.2f}%` من الدخول)\n"
                           f"- **مدة الاحتفاظ بالصفقة:** {duration_str}")
            else:
                message = (f"**📦 إغلاق {trade_type_text} | #{original_trade['id']} {original_trade['symbol']}**\n\n"
                           f"**الحالة: ❌ فاشلة (تم ضرب الوقف)**\n"
                           f"*\"لا بأس، كل صفقة هي درس جديد. الخسارة جزء من رحلة النجاح.\"*\n"
                           f"- - - - - - - - - - - - - - - - - -\n"
                           f"💰 **الخسارة:** `${pnl_usdt:.2f}` (`{(pnl_usdt / original_trade['entry_value_usdt'] * 100):.2f}%`)\n"
                           f"- - - - - - - - - - - - - - - - - -\n\n"
                           f"- **سعر الدخول:** `{original_trade['entry_price']}`\n"
                           f"- **سعر الإغلاق:** `{result['exit_price']}`\n"
                           f"- **أعلى قمة وصلت لها:** `{highest_price_val}` (`{((highest_price_val - original_trade['entry_price']) / original_trade['entry_price'] * 100):+.2f}%` من الدخول)\n"
                           f"- **مدة الاحتفاظ بالصفقة:** {duration_str}")

            await send_telegram_message(context.bot, {'custom_message': message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})

        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl': updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], result['id'])))
        elif status == 'update_peak': updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], result['id'])))
    
    if updates_to_db:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            for q, p in updates_to_db: cursor.execute(q, p)
            conn.commit(); conn.close()
        except Exception as e: logger.error(f"DB update failed in track_open_trades: {e}")
    if portfolio_pnl != 0.0:
        bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl
        save_settings()
        logger.info(f"Virtual portfolio balance updated by ${portfolio_pnl:.2f}.")

async def check_real_trades_status(context: ContextTypes.DEFAULT_TYPE):
    """
    [تداول حقيقي] وظيفة جديدة للتحقق من حالة الصفقات الحقيقية المغلقة على Binance
    """
    settings = bot_data['settings']
    if not settings.get("REAL_TRADING_ENABLED"): return
    
    logger.info("Checking status of active REAL trades on Binance...")
    binance = bot_data["exchanges"].get('binance')
    if not binance or not binance.apiKey:
        logger.warning("Skipping real trade check: Binance not connected with API keys.")
        return

    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة' AND is_real_trade = 1")
        real_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e:
        logger.error(f"DB error in check_real_trades_status: {e}"); return
    
    if not real_trades:
        logger.info("No active REAL trades found in DB to check.")
        return

    for trade in real_trades:
        try:
            # في Binance، أوامر OCO تظهر كأمرين منفصلين بعد التنفيذ
            # نتحقق من سجل أوامر البيع للعملة
            closed_orders = await binance.fetch_closed_orders(trade['symbol'])
            
            # نبحث عن أمر الهدف أو الوقف الذي تم تنفيذه
            for order in closed_orders:
                # نتحقق إذا كان الأمر تابعاً لصفقتنا (نفس الكمية تقريباً) وأغلق بعد فتح الصفقة
                trade_ts = datetime.strptime(trade['timestamp'], '%Y-%m-%d %H:%M:%S').timestamp() * 1000
                if (order['side'] == 'sell' and 
                    order['timestamp'] > trade_ts and
                    abs(order['filled'] - trade['quantity']) / trade['quantity'] < 0.01): # تسامح 1%

                    status = 'ناجحة' if order['price'] >= trade['take_profit'] else 'فاشلة'
                    exit_price = order['average'] if order['average'] else order['price']
                    pnl_usdt = (exit_price - trade['entry_price']) * order['filled']
                    closed_at_str = datetime.fromtimestamp(order['timestamp']/1000, tz=EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')

                    logger.info(f"REAL trade #{trade['id']} for {trade['symbol']} detected as CLOSED. Status: {status}")

                    # تحديث قاعدة البيانات
                    conn = sqlite3.connect(DB_FILE, timeout=10)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?",
                                   (status, exit_price, closed_at_str, exit_price * order['filled'], pnl_usdt, trade['id']))
                    conn.commit()
                    conn.close()

                    # إرسال رسالة الإغلاق
                    # (الكود مكرر من track_open_trades، يمكن تحسينه لاحقاً)
                    trade_type_text = "صفقة حقيقية 🟢"
                    if status == 'ناجحة':
                        message = (f"**📦 إغلاق {trade_type_text} | #{trade['id']} {trade['symbol']}**\n\n"
                                   f"**الحالة: ✅ ناجحة (تم تحقيق الهدف بنجاح)**\n"
                                   f"💰 **الربح:** `${pnl_usdt:+.2f}` (`{(pnl_usdt / trade['entry_value_usdt'] * 100):+.2f}%`)\n"
                                   f"- **سعر الإغلاق:** `{exit_price}`")
                    else:
                        message = (f"**📦 إغلاق {trade_type_text} | #{trade['id']} {trade['symbol']}**\n\n"
                                   f"**الحالة: ❌ فاشلة (تم ضرب الوقف)**\n"
                                   f"💰 **الخسارة:** `${pnl_usdt:.2f}` (`{(pnl_usdt / trade['entry_value_usdt'] * 100):.2f}%`)\n"
                                   f"- **سعر الإغلاق:** `{exit_price}`")
                    await send_telegram_message(context.bot, {'custom_message': message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})

                    break # ننتقل للصفقة التالية
        except Exception as e:
            logger.error(f"Error checking real trade status for {trade['symbol']} (ID: {trade['id']}): {e}")


# --- Helper Functions (No changes needed) ---
def get_fear_and_greed_index():
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        response.raise_for_status()
        if data := response.json().get('data', []):
            return int(data[0]['value'])
    except Exception as e:
        logger.error(f"Could not fetch Fear and Greed Index: {e}")
    return None

async def check_market_regime():
    settings = bot_data['settings']
    is_technically_bullish, is_sentiment_bullish, fng_index = True, True, "N/A"
    try:
        if binance := bot_data["exchanges"].get('binance'):
            ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['sma50'] = ta.sma(df['close'], length=50)
            is_technically_bullish = df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception as e:
        logger.error(f"Error checking BTC trend: {e}")
    if settings.get("fear_and_greed_filter_enabled", True):
        if (fng_value := get_fear_and_greed_index()) is not None:
            fng_index = fng_value
            is_sentiment_bullish = fng_index >= settings.get("fear_and_greed_threshold", 30)
    if not is_technically_bullish:
        return False, "اتجاه BTC هابط (تحت متوسط 50 على 4 ساعات)."
    if not is_sentiment_bullish:
        return False, f"مشاعر خوف شديد (مؤشر F&G: {fng_index} تحت الحد {settings.get('fear_and_greed_threshold')})."
    return True, "وضع السوق مناسب لصفقات الشراء."

# --- Smart Suggestions (No changes needed) ---
async def analyze_performance_and_suggest(context: ContextTypes.DEFAULT_TYPE):
    settings = bot_data['settings']
    history = bot_data['scan_history']
    
    if len(history) < 5 or (time.time() - settings.get('last_suggestion_time', 0)) < 7200:
        return

    avg_signals = sum(item['signals'] for item in history) / len(history)
    current_preset = settings.get('active_preset_name', 'PRO')
    
    suggestion, market_desc, reason = None, None, None

    if avg_signals < 0.5 and current_preset == "STRICT":
        suggestion = "PRO"
        market_desc = "السوق يبدو بطيئاً جداً والإشارات شحيحة."
        reason = "نمط 'PRO' أكثر توازناً وقد يساعدنا في التقاط المزيد من الفرص المناسبة دون التضحية بالكثير من الجودة."
    elif avg_signals < 1 and current_preset == "PRO":
        suggestion = "LAX"
        market_desc = "عدد الفرص المكتشفة منخفض نسبياً."
        reason = "نمط 'LAX' (متساهل) سيوسع نطاق البحث، مما قد يزيد من عدد الإشارات في سوق هادئ."
    elif avg_signals > 8 and current_preset in ["LAX", "VERY_LAX"]:
        suggestion = "PRO"
        market_desc = "السوق نشط جداً وهناك عدد كبير من الإشارات (ضوضاء)."
        reason = "نمط 'PRO' سيساعد في فلترة الإشارات الأضعف والتركيز على الفرص ذات الجودة الأعلى."
    elif avg_signals > 12 and current_preset == "PRO":
        suggestion = "STRICT"
        market_desc = "السوق متقلب وهناك فيضان من الإشارات."
        reason = "نمط 'STRICT' (متشدد) سيطبق أقوى الفلاتر لاصطياد أفضل الفرص فقط في هذا السوق المتقلب."

    if suggestion and suggestion != current_preset:
        message = (f"**💡 اقتراح ذكي لتحسين الأداء**\n\n"
                   f"*مرحباً! بناءً على تحليل آخر {len(history)} فحص، لاحظت تغيراً في طبيعة السوق.*\n\n"
                   f"**الملاحظة:**\n- {market_desc}\n\n"
                   f"**الاقتراح:**\n- أقترح تغيير نمط الإعدادات من `{current_preset}` إلى **`{suggestion}`**.\n\n"
                   f"**السبب:**\n- {reason}\n\n"
                   f"*هل توافق على تطبيق هذا التغيير؟*")
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، قم بتطبيق النمط المقترح", callback_data=f"suggest_accept_{suggestion}")],
            [InlineKeyboardButton("❌ لا شكراً، تجاهل الاقتراح", callback_data="suggest_decline")]
        ])
        
        await send_telegram_message(context.bot, {'custom_message': message, 'keyboard': keyboard})
        bot_data['settings']['last_suggestion_time'] = time.time()
        save_settings()


# --- Reports and Telegram Commands ---

def generate_performance_report_string():
    REPORT_DAYS = 30
    if not os.path.exists(DB_FILE): return "❌ خطأ: لم يتم العثور على ملف قاعدة البيانات."
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        start_date = (datetime.now() - timedelta(days=REPORT_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        # [تداول حقيقي] فصل التقارير
        cursor.execute("SELECT reason, status, entry_price, highest_price, is_real_trade FROM trades WHERE status IN ('ناجحة', 'فاشلة') AND timestamp >= ?", (start_date,)); 
        trades = cursor.fetchall(); conn.close()
    except Exception as e: return f"❌ حدث خطأ غير متوقع: {e}"
    if not trades: return f"ℹ️ لا توجد صفقات مغلقة في آخر {REPORT_DAYS} يومًا."
    
    real_trades = [t for t in trades if t['is_real_trade']]
    paper_trades = [t for t in trades if not t['is_real_trade']]
    
    def generate_report_for_type(trade_list, title):
        if not trade_list: return [f"*{title}: لا توجد صفقات*"]
        
        strategy_stats = {}
        for trade in trade_list:
            reasons = trade['reason'].split(' + ')
            for reason in reasons:
                stats = strategy_stats.setdefault(reason, {'total': 0, 'successful': 0, 'max_profits': []})
                stats['total'] += 1
                if trade['status'] == 'ناجحة': stats['successful'] += 1
                if trade['entry_price'] > 0 and trade['highest_price'] is not None:
                    stats['max_profits'].append(((trade['highest_price'] - trade['entry_price']) / trade['entry_price']) * 100)
        
        report_lines = [f"**{title}**", "="*20]
        for reason_en, stats in sorted(strategy_stats.items(), key=lambda item: item[1]['total'], reverse=True):
            reason_ar = STRATEGY_NAMES_AR.get(reason_en, reason_en)
            if total_trades := stats['total']:
                success_rate = (stats['successful'] / total_trades) * 100
                avg_max_profit = sum(stats['max_profits']) / len(stats['max_profits']) if stats['max_profits'] else 0
                report_lines.extend([f"--- **{reason_ar}** ---", f"- **الإجمالي:** {total_trades}", f"- **النجاح:** {success_rate:.1f}%", f"- **متوسط الربح:** {avg_max_profit:.2f}%", ""])
        return report_lines

    final_report = [f"📊 **تقرير أداء الاستراتيجيات (آخر {REPORT_DAYS} يومًا)** 📊\n"]
    final_report.extend(generate_report_for_type(real_trades, "🟢 التداول الحقيقي"))
    final_report.append("\n- - - - - - - - - - - - - - - - - -\n")
    final_report.extend(generate_report_for_type(paper_trades, "📝 التداول الوهمي"))

    return "\n".join(final_report)


# [تحديث واجهة المستخدم] تحديث القوائم الرئيسية
main_menu_keyboard = [["Dashboard 🖥️", "🔍 فحص يدوي"], ["⚙️ الإعدادات"], ["ℹ️ مساعدة"]]
settings_menu_keyboard = [["🏁 أنماط جاهزة", "🎭 تفعيل/تعطيل الماسحات"], ["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في بوت المحلل الآلي! (v5.1 - تداول حقيقي)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))

async def show_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الإحصائيات العامة", callback_data="dashboard_stats"), InlineKeyboardButton("📈 الصفقات النشطة", callback_data="dashboard_active_trades")],
        [InlineKeyboardButton("📜 تقرير أداء الاستراتيجيات", callback_data="dashboard_strategy_report")],
        [InlineKeyboardButton("🗓️ التقرير اليومي", callback_data="dashboard_daily_report"), InlineKeyboardButton("🕵️‍♂️ تقرير التشخيص", callback_data="dashboard_debug")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="dashboard_refresh")]
    ])
    
    trading_mode = bot_data['status_snapshot']['trading_mode']
    message_text = f"🖥️ *لوحة التحكم الرئيسية*\n\n**وضع التداول الحالي: {trading_mode}**\n\nاختر التقرير أو البيانات التي تريد عرضها:"
    
    if update.callback_query and update.callback_query.data == "dashboard_refresh":
        try:
            await target_message.edit_text(message_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error refreshing dashboard: {e}")
    else:
        await target_message.reply_text(message_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): await (update.message or update.callback_query.message).reply_text("اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))

def get_scanners_keyboard():
    active_scanners = bot_data["settings"].get("active_scanners", [])
    keyboard = [[InlineKeyboardButton(f"{'✅' if name in active_scanners else '❌'} {name}", callback_data=f"toggle_{name}")] for name in SCANNERS.keys()]
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keyboard)

def get_presets_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚦 احترافية (متوازنة)", callback_data="preset_PRO"), InlineKeyboardButton("🎯 متشددة", callback_data="preset_STRICT")],
        [InlineKeyboardButton("🌙 متساهلة", callback_data="preset_LAX"), InlineKeyboardButton("⚠️ فائق التساهل", callback_data="preset_VERY_LAX")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")]
    ])

async def show_presets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر نمط إعدادات جاهز:", reply_markup=get_presets_keyboard())
async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
async def show_parameters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard, settings = [], bot_data["settings"]
    for category, params in EDITABLE_PARAMS.items():
        keyboard.append([InlineKeyboardButton(f"--- {category} ---", callback_data="ignore")])
        for row in [params[i:i + 2] for i in range(0, len(params), 2)]:
            button_row = []
            for param_key in row:
                display_name = PARAM_DISPLAY_NAMES.get(param_key, param_key)
                current_value = settings.get(param_key, "N/A")
                text = f"{display_name}: {'مُفعّل ✅' if current_value else 'مُعطّل ❌'}" if isinstance(current_value, bool) else f"{display_name}: {current_value}"
                button_row.append(InlineKeyboardButton(text, callback_data=f"param_{param_key}"))
            keyboard.append(button_row)
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    message_text = "⚙️ *الإعدادات المتقدمة* ⚙️\n\nاختر الإعداد الذي تريد تعديله بالضغط عليه:"
    target_message = update.callback_query.message if update.callback_query else update.message
    try:
        if update.callback_query:
            await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        else:
            sent_message = await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            context.user_data['settings_menu_id'] = sent_message.message_id
    except BadRequest as e:
        if "Message is not modified" not in str(e): logger.error(f"Error editing parameters menu: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*مساعدة البوت*\n`/start` - بدء\n`/check <ID>` - متابعة صفقة", parse_mode=ParseMode.MARKDOWN)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.callback_query.message if update.callback_query else update.message
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor();
        # [تداول حقيقي] فصل الإحصائيات
        cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades WHERE is_real_trade = 1 GROUP BY status")
        real_stats = cursor.fetchall()
        cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades WHERE is_real_trade = 0 GROUP BY status")
        paper_stats = cursor.fetchall()
        conn.close()
        
        def process_stats(stats_data):
            counts = {s: c for s, c, p in stats_data}; pnl = {s: (p or 0) for s, c, p in stats_data}
            total = sum(counts.values()); active = counts.get('نشطة', 0)
            successful = counts.get('ناجحة', 0); failed = counts.get('فاشلة', 0)
            closed = successful + failed; win_rate = (successful / closed * 100) if closed > 0 else 0
            total_pnl = sum(p for s, p in pnl.items() if s != 'نشطة')
            return {"total": total, "active": active, "successful": successful, "failed": failed, "win_rate": win_rate, "total_pnl": total_pnl}

        real_data = process_stats(real_stats)
        paper_data = process_stats(paper_stats)
        
        trading_mode = bot_data['status_snapshot']['trading_mode']
        preset_name = bot_data["settings"].get("active_preset_name", "N/A")

        stats_msg = (f"*📊 إحصائيات الأداء*\n\n"
                       f"**وضع التداول الحالي: {trading_mode}**\n"
                       f"⚙️ *النمط الحالي:* `{preset_name}`\n"
                       f"- - - - - - - - - - - - - - - - - -\n"
                       f"**🟢 التداول الحقيقي:**\n"
                       f"  💰 *إجمالي الربح/الخسارة:* `${real_data['total_pnl']:+.2f}`\n"
                       f"  - *إجمالي الصفقات:* `{real_data['total']}` (`{real_data['active']}` نشطة)\n"
                       f"  - *الناجحة:* `{real_data['successful']}` | *الفاشلة:* `{real_data['failed']}`\n"
                       f"  - *معدل النجاح:* `{real_data['win_rate']:.2f}%`\n"
                       f"- - - - - - - - - - - - - - - - - -\n"
                       f"**📝 التداول الوهمي:**\n"
                       f"  📈 *الرصيد الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n"
                       f"  💰 *إجمالي الربح/الخسارة:* `${paper_data['total_pnl']:+.2f}`\n"
                       f"  - *إجمالي الصفقات:* `{paper_data['total']}` (`{paper_data['active']}` نشطة)\n"
                       f"  - *الناجحة:* `{paper_data['successful']}` | *الفاشلة:* `{paper_data['failed']}`\n"
                       f"  - *معدل النجاح:* `{paper_data['win_rate']:.2f}%`")

        await target_message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in stats_command: {e}", exc_info=True); await target_message.reply_text("خطأ في جلب الإحصائيات.")
async def strategy_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.callback_query.message if update.callback_query else update.message
    await target_message.reply_text("⏳ جاري إعداد تقرير أداء الاستراتيجيات..."); report_string = generate_performance_report_string()
    await target_message.reply_text(report_string, parse_mode=ParseMode.MARKDOWN)

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d')
    logger.info(f"Generating detailed daily report for {today_str}...")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, status, pnl_usdt, entry_value_usdt, reason, is_real_trade FROM trades WHERE DATE(closed_at) = ?", (today_str,))
        closed_today = [dict(row) for row in cursor.fetchall()]
        conn.close()

        if not closed_today:
            report_message = f"**🗓️ التقرير اليومي | {today_str}**\n\nلم يتم إغلاق أي صفقات اليوم."
        else:
            real_trades = [t for t in closed_today if t['is_real_trade']]
            paper_trades = [t for t in closed_today if not t['is_real_trade']]
            
            def generate_daily_summary(trades, title):
                if not trades: return f"*{title}: لا يوجد نشاط اليوم*"
                wins = [t for t in trades if t['status'] == 'ناجحة']
                total_pnl = sum(t['pnl_usdt'] for t in trades if t['pnl_usdt'] is not None)
                win_rate = (len(wins) / len(trades) * 100) if trades else 0
                return (f"**{title}**\n"
                        f"  - الصفقات المغلقة: {len(trades)}\n"
                        f"  - الربح/الخسارة الصافي: `${total_pnl:+.2f}`\n"
                        f"  - معدل النجاح: {win_rate:.1f}%")

            parts = [f"**🗓️ التقرير اليومي المفصل | {today_str}**\n"]
            parts.append(generate_daily_summary(real_trades, "🟢 الأداء الحقيقي"))
            parts.append("\n" + generate_daily_summary(paper_trades, "📝 الأداء الوهمي"))
            parts.append("\n- - - - - - - - - - - - - - - - - -")
            parts.append("*رسالة اليوم: \"النجاح في التداول هو نتيجة للانضباط والصبر والتعلم المستمر.\"*")
            report_message = "\n".join(parts)

        await send_telegram_message(context.bot, {'custom_message': report_message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})
    except Exception as e:
        logger.error(f"Failed to generate detailed daily report: {e}", exc_info=True)

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.callback_query.message if update.callback_query else update.message
    await target_message.reply_text("⏳ جاري إرسال التقرير اليومي المفصل...")
    await send_daily_report(context)
    await target_message.reply_text("✅ تم إرسال التقرير بنجاح إلى القناة.")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.callback_query.message if update.callback_query else update.message
    await target_message.reply_text("⏳ جاري إعداد تقرير التشخيص الشامل...")
    settings = bot_data.get("settings", {})
    parts = [f"**🕵️‍♂️ تقرير التشخيص الشامل**\n\n*تم إنشاؤه في: {datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')}*"]
    
    parts.append("\n- - - - - - - - - - - - - - - - - -")
    parts.append("**[ ⚙️ حالة النظام والبيئة ]**")
    parts.append(f"- `NLTK (تحليل الأخبار):` {'متاحة ✅' if NLTK_AVAILABLE else 'غير متاحة ❌'}")
    parts.append(f"- `SciPy (تحليل الدايفرجنس):` {'متاحة ✅' if SCIPY_AVAILABLE else 'غير متاحة ❌'}")
    parts.append(f"- `Alpha Vantage (بيانات اقتصادية):` {'موجود ✅' if ALPHA_VANTAGE_API_KEY != 'YOUR_AV_KEY_HERE' else 'مفقود ⚠️'}")

    parts.append("\n**[ 📊 حالة السوق الحالية ]**")
    mood_info = settings.get("last_market_mood", {})
    fng_value = get_fear_and_greed_index()
    fng_text = "غير متاح"
    if fng_value is not None:
        classification = "خوف شديد" if fng_value < 25 else "خوف" if fng_value < 45 else "محايد" if fng_value < 55 else "طمع" if fng_value < 75 else "طمع شديد"
        fng_text = f"{fng_value} ({classification})"
    parts.append(f"- **المزاج الأساسي (أخبار):** `{mood_info.get('mood', 'N/A')}`")
    parts.append(f"  - `{mood_info.get('reason', 'N/A')}`")
    parts.append(f"- **المزاج الفني (BTC):** `{bot_data['status_snapshot']['btc_market_mood']}`")
    parts.append(f"- **مؤشر الخوف والطمع:** `{fng_text}`")
    
    status = bot_data['status_snapshot']
    scan_duration_str = status.get('last_scan_start_time', 'N/A')
    scan_duration = "N/A"
    if status['last_scan_end_time'] != 'N/A' and scan_duration_str != 'N/A':
        duration_sec = (datetime.strptime(status['last_scan_end_time'], '%Y-%m-%d %H:%M:%S') - datetime.strptime(scan_duration_str, '%Y-%m-%d %H:%M:%S')).total_seconds()
        scan_duration = f"{duration_sec:.0f} ثانية"
    parts.append("\n**[ 🔬 أداء آخر فحص ]**")
    parts.append(f"- **وضع التداول:** `{status['trading_mode']}`")
    parts.append(f"- **وقت البدء:** `{status['last_scan_start_time']}`")
    parts.append(f"- **المدة:** `{scan_duration}`")
    parts.append(f"- **العملات المفحوصة:** `{status['markets_found']}`")
    parts.append(f"- **فشل في تحليل:** `{(bot_data['scan_history'][-1]['failures'] if bot_data['scan_history'] else 'N/A')}` عملات")

    parts.append("\n**[ 🔧 الإعدادات النشطة ]**")
    parts.append(f"- **النمط الحالي:** `{settings.get('active_preset_name', 'N/A')}`")
    parts.append(f"- **حجم الصفقة الحقيقي:** `${settings.get('trade_size_usdt', 0)}`")
    parts.append(f"- **الماسحات المفعلة:** `{', '.join(settings.get('active_scanners', []))}`")
    
    parts.append("\n**[ 🔩 حالة العمليات الداخلية ]**")
    if context.job_queue:
        scan_job = context.job_queue.get_jobs_by_name('perform_scan')
        track_job = context.job_queue.get_jobs_by_name('track_open_trades')
        scan_next = scan_job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S') if scan_job and scan_job[0].next_t else 'N/A'
        track_next = track_job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S') if track_job and track_job[0].next_t else 'N/A'
        parts.append("- **المهام المجدولة:**")
        parts.append(f"  - `فحص العملات:` {'يعمل'}, *التالي: {scan_next}*")
        parts.append(f"  - `متابعة الصفقات (وهمي):` {'يعمل'}, *التالي: {track_next}*")
        parts.append(f"  - `متابعة الصفقات (حقيقي):` {'يعمل'}")

    parts.append("- **الاتصال بالمنصات:**")
    for ex_id in EXCHANGES_TO_SCAN: parts.append(f"  - `{ex_id.capitalize()}:` {'متصل ✅' if ex_id in bot_data.get('exchanges', {}) else 'غير متصل ❌'}")

    parts.append("- **قاعدة البيانات:**")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5); cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades"); total_trades = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'"); active_trades = cursor.fetchone()[0]
        conn.close()
        db_size = os.path.getsize(DB_FILE) / (1024 * 1024)
        parts.append(f"  - `الاتصال:` ناجح ✅")
        parts.append(f"  - `حجم الملف:` {db_size:.2f} MB")
        parts.append(f"  - `إجمالي الصفقات:` {total_trades} ({active_trades} نشطة)")
    except Exception as e: parts.append(f"  - `الاتصال:` فشل ❌ ({e})")
    parts.append("- - - - - - - - - - - - - - - - - -")

    await target_message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)


async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    target = update.callback_query.message if trade_id_from_callback else update.message
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"
    try:
        trade_id = trade_id_from_callback or int(context.args[0])
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor(); cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,));
        trade = dict(trade_row) if (trade_row := cursor.fetchone()) else None; conn.close()
        if not trade: await target.reply_text(f"لم يتم العثور على صفقة بالرقم `{trade_id}`."); return
        
        trade_type = "حقيقية 🟢" if trade.get('is_real_trade') else "وهمية 📝"

        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            closed_at_dt = datetime.strptime(trade['closed_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=EGYPT_TZ)
            message = (f"📋 *ملخص الصفقة #{trade_id} ({trade_type})*\n\n"
                       f"*العملة:* `{trade['symbol']}`\n*الحالة:* `{trade['status']}`\n"
                       f"*تاريخ الإغلاق:* `{closed_at_dt.strftime('%Y-%m-%d %I:%M %p')}`\n"
                       f"*الربح/الخسارة:* `${trade.get('pnl_usdt', 0):+.2f} ({pnl_percent:+.2f}%)`")
        else:
            if not (exchange := bot_data["exchanges"].get(trade['exchange'].lower())): await target.reply_text("المنصة غير متصلة."); return
            if not (ticker := await exchange.fetch_ticker(trade['symbol'])) or not (current_price := ticker.get('last') or ticker.get('close')):
                await target.reply_text(f"لم أتمكن من جلب السعر الحالي لـ `{trade['symbol']}`."); return
            live_pnl = (current_price - trade['entry_price']) * trade['quantity']
            live_pnl_percent = (live_pnl / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            message = (f"📈 *متابعة حية للصفقة #{trade_id} ({trade_type})*\n\n"
                       f"▫️ *العملة:* `{trade['symbol']}` | *الحالة:* `نشطة`\n"
                       f"▫️ *سعر الدخول:* `${format_price(trade['entry_price'])}`\n"
                       f"▫️ *السعر الحالي:* `${format_price(current_price)}`\n\n"
                       f"💰 *الربح/الخسارة الحالية:*\n`${live_pnl:+.2f} ({live_pnl_percent:+.2f}%)`")
            if trade.get('is_real_trade'):
                message += f"\n\n*رقم أمر الدخول:* `{trade.get('entry_order_id')}`\n*رقم قائمة SL/TP:* `{trade.get('sl_tp_order_id')}`"
        
        await target.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await target.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e: logger.error(f"Error in check_trade_command: {e}", exc_info=True); await target.reply_text("حدث خطأ.")
async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.callback_query.message if update.callback_query else update.message
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, entry_value_usdt, exchange, is_real_trade FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
        active_trades = cursor.fetchall(); conn.close()
        if not active_trades: await target_message.reply_text("لا توجد صفقات نشطة حالياً."); return
        
        keyboard = []
        for t in active_trades:
            mode_icon = "🟢" if t['is_real_trade'] else "📝"
            button_text = f"{mode_icon} #{t['id']} | {t['symbol']} | ${t['entry_value_usdt']:.2f}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"check_{t['id']}")])
        
        await target_message.reply_text("اختر صفقة لمتابعتها:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e: logger.error(f"Error in show_active_trades: {e}"); await target_message.reply_text("خطأ في جلب الصفقات.")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    
    if data.startswith("dashboard_"):
        action = data.split("_", 1)[1]
        if action == "stats": await stats_command(update, context)
        elif action == "active_trades": await show_active_trades_command(update, context)
        elif action == "strategy_report": await strategy_report_command(update, context)
        elif action == "daily_report": await daily_report_command(update, context)
        elif action == "debug": await debug_command(update, context)
        elif action == "refresh":
            try:
                await show_dashboard_command(update, context)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"Error on dashboard refresh button: {e}")
        return

    elif data.startswith("preset_"):
        preset_name = data.split("_", 1)[1]
        if preset_data := PRESETS.get(preset_name):
            bot_data["settings"]['liquidity_filters'] = preset_data['liquidity_filters']
            bot_data["settings"]['volatility_filters'] = preset_data['volatility_filters']
            bot_data["settings"]['ema_trend_filter'] = preset_data['ema_trend_filter']
            bot_data["settings"]['min_tp_sl_filter'] = preset_data['min_tp_sl_filter']
            bot_data["settings"]["active_preset_name"] = preset_name
            save_settings()
            preset_titles = {"PRO": "احترافي", "STRICT": "متشدد", "LAX": "متساهل", "VERY_LAX": "فائق التساهل"}
            lf, vf = preset_data['liquidity_filters'], preset_data['volatility_filters']
            confirmation_text = f"✅ *تم تفعيل النمط: {preset_titles.get(preset_name, preset_name)}*\n\n*أهم القيم:*\n`- min_rvol: {lf['min_rvol']}`\n`- max_spread: {lf['max_spread_percent']}%`\n`- min_atr: {vf['min_atr_percent']}%`"
            try: await query.edit_message_text(confirmation_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_presets_keyboard())
            except BadRequest as e:
                if "Message is not modified" not in str(e): raise
    elif data.startswith("param_"):
        param_key = data.split("_", 1)[1]
        context.user_data['awaiting_input_for_param'] = param_key
        context.user_data['settings_menu_id'] = query.message.message_id
        current_value = bot_data["settings"].get(param_key)
        if isinstance(current_value, bool):
            new_value = not current_value
            # [تداول حقيقي] تحذير هام عند تفعيل التداول الحقيقي
            if param_key == "REAL_TRADING_ENABLED" and new_value is True:
                if BINANCE_API_KEY == 'YOUR_BINANCE_API_KEY' or BINANCE_API_SECRET == 'YOUR_BINANCE_SECRET_KEY':
                    await query.answer("⚠️ لا يمكن التفعيل! مفاتيح Binance API غير مهيأة.", show_alert=True)
                    return
                await query.answer("‼️ تحذير! أنت على وشك تفعيل التداول بأموال حقيقية.", show_alert=True)
                
            bot_data["settings"][param_key] = new_value
            # [تداول حقيقي] تحديث حالة البوت
            bot_data['status_snapshot']['trading_mode'] = "حقيقي 🟢" if new_value else "وهمي 📝"
            bot_data["settings"]["active_preset_name"] = "Custom"
            save_settings()
            await query.answer(f"✅ تم تبديل '{PARAM_DISPLAY_NAMES.get(param_key, param_key)}'")
            await show_parameters_menu(update, context)
        else:
            await query.edit_message_text(f"📝 *تعديل '{PARAM_DISPLAY_NAMES.get(param_key, param_key)}'*\n\n*القيمة الحالية:* `{current_value}`\n\nالرجاء إرسال القيمة الجديدة.", parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("toggle_"):
        scanner_name = data.split("_", 1)[1]
        active_scanners = bot_data["settings"].get("active_scanners", []).copy()
        if scanner_name in active_scanners: active_scanners.remove(scanner_name)
        else: active_scanners.append(scanner_name)
        bot_data["settings"]["active_scanners"] = active_scanners; save_settings()
        try: await query.edit_message_text(text="اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    elif data == "back_to_settings":
        if query.message: await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text="اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif data.startswith("check_"):
        await check_trade_command(update, context, trade_id_from_callback=int(data.split("_")[1]))
    
    elif data.startswith("suggest_"):
        action = data.split("_", 1)[1]
        if action.startswith("accept"):
            preset_name = data.split("_")[2]
            if preset_data := PRESETS.get(preset_name):
                bot_data["settings"]['liquidity_filters'] = preset_data['liquidity_filters']
                bot_data["settings"]['volatility_filters'] = preset_data['volatility_filters']
                bot_data["settings"]['ema_trend_filter'] = preset_data['ema_trend_filter']
                bot_data["settings"]['min_tp_sl_filter'] = preset_data['min_tp_sl_filter']
                bot_data["settings"]["active_preset_name"] = preset_name
                save_settings()
                await query.edit_message_text(f"✅ **تم قبول الاقتراح!**\n\nتم تغيير النمط بنجاح إلى `{preset_name}`.", parse_mode=ParseMode.MARKDOWN)
        elif action == "decline":
            await query.edit_message_text("👍 **تم تجاهل الاقتراح.**\n\nسيستمر البوت بالعمل على الإعدادات الحالية.", parse_mode=ParseMode.MARKDOWN)


async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if param := context.user_data.pop('awaiting_input_for_param', None):
        value_str = update.message.text
        settings_menu_id = context.user_data.pop('settings_menu_id', None)
        chat_id = update.message.chat_id
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        settings = bot_data["settings"]
        try:
            current_type = type(settings.get(param, ''))
            new_value = current_type(value_str)
            if isinstance(settings.get(param), bool):
                new_value = value_str.lower() in ['true', '1', 'yes', 'on', 'نعم', 'تفعيل']
            settings[param] = new_value
            settings["active_preset_name"] = "Custom"
            save_settings()
            if settings_menu_id: context.user_data['settings_menu_id'] = settings_menu_id
            await show_parameters_menu(update, context)
            confirm_msg = await update.message.reply_text(f"✅ تم تحديث **{PARAM_DISPLAY_NAMES.get(param, param)}** إلى `{new_value}`.", parse_mode=ParseMode.MARKDOWN)
            context.job_queue.run_once(lambda ctx: ctx.bot.delete_message(chat_id, confirm_msg.message_id), 4)
        except (ValueError, KeyError):
            if settings_menu_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=settings_menu_id, text="❌ قيمة غير صالحة. الرجاء المحاولة مرة أخرى.")
                context.job_queue.run_once(lambda _: show_parameters_menu(update, context), 3)
        return

    handlers = {
        "Dashboard 🖥️": show_dashboard_command,
        "🔍 فحص يدوي": manual_scan_command,
        "ℹ️ مساعدة": help_command, 
        "⚙️ الإعدادات": show_settings_menu, 
        "🔧 تعديل المعايير": show_parameters_menu, 
        "🔙 القائمة الرئيسية": start_command, 
        "🎭 تفعيل/تعطيل الماسحات": show_scanners_menu, 
        "🏁 أنماط جاهزة": show_presets_menu, 
    }
    if handler := handlers.get(update.message.text): await handler(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None: logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers a manual scan if one is not already in progress."""
    if scan_lock.locked():
        await update.message.reply_text("⏳ يوجد فحص قيد التنفيذ بالفعل. يرجى الانتظار حتى يكتمل.")
    else:
        await update.message.reply_text("👍 حسنًا, سأبدأ عملية فحص يدوية للأسواق الآن...")
        context.job_queue.run_once(lambda ctx: perform_scan(ctx), 0)

async def post_init(application: Application):
    if NLTK_AVAILABLE:
        try: nltk.data.find('sentiment/vader_lexicon.zip')
        except LookupError: logger.info("Downloading NLTK data (vader_lexicon)..."); nltk.download('vader_lexicon')
    logger.info("Post-init: Initializing exchanges...")
    await initialize_exchanges()
    if not bot_data["exchanges"]: logger.critical("CRITICAL: No exchanges connected. Bot cannot run."); return
    logger.info("Exchanges initialized. Setting up job queue...")
    job_queue = application.job_queue
    job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
    job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
    job_queue.run_repeating(check_real_trades_status, interval=TRACK_INTERVAL_SECONDS * 2, first=30, name='check_real_trades') # [تداول حقيقي]
    job_queue.run_daily(send_daily_report, time=dt_time(hour=23, minute=55, tzinfo=EGYPT_TZ), name='daily_report')
    logger.info(f"Jobs scheduled. Daily report at 23:55 {EGYPT_TZ}.")
    
    trading_mode = bot_data['status_snapshot']['trading_mode']
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *المحلل الآلي جاهز للعمل! (v5.1)*\n\n**وضع التشغيل الحالي: {trading_mode}**", parse_mode=ParseMode.MARKDOWN)
    logger.info("Post-init finished.")
async def post_shutdown(application: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logger.info("All exchange connections closed.")

def main():
    print("🚀 Starting Pro Trading Analyzer Bot v5.1 (Real Trading Enabled)...")
    load_settings(); init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("scan", manual_scan_command))
    application.add_handler(CommandHandler("check", check_trade_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    application.add_error_handler(error_handler)

    print("✅ Bot is now running and polling for updates...")
    application.run_polling()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.critical(f"Bot stopped due to a critical unhandled error: {e}", exc_info=True)
