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
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest, RetryAfter, TimedOut

# [UPGRADE] Gracefully handle optional libraries for fundamental analysis
try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logging.warning("Library 'scipy' not found. RSI Divergence strategy will be disabled.")

try:
    import investpy
    INVESTPY_AVAILABLE = True
except ImportError:
    INVESTPY_AVAILABLE = False
    logging.warning("Library 'investpy' not found. Economic calendar checks will be disabled.")

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    logging.warning("Library 'feedparser' not found. News fetching will be disabled.")

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    import nltk
    # Check if vader_lexicon is downloaded, if not, download it
    try:
        nltk.data.find('sentiment/vader_lexicon.zip')
    except nltk.downloader.DownloadError:
        logging.info("NLTK 'vader_lexicon' not found. Downloading...")
        nltk.download('vader_lexicon')
    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False
    logging.warning("Library 'nltk' not found. Sentiment analysis will be disabled.")


# --- الإعدادات الأساسية --- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)

if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
HIGHER_TIMEFRAME = '1h'
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120

APP_ROOT = '.'
DB_FILE = os.path.join(APP_ROOT, 'trading_bot_v26.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'settings_v26.json')

EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) --- #
LOG_FILE = os.path.join(APP_ROOT, 'bot_v26.log')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler(LOG_FILE, 'w'), logging.StreamHandler()])
for logger_name in ['httpx', 'apscheduler', 'telegram', 'requests', 'investpy']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)


# --- [IMPROVEMENT] Preset Configurations ---
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

# --- Expanded Constants for Interactive Settings menu ---
EDITABLE_PARAMS = {
    "إعدادات عامة": [
        "max_concurrent_trades", "top_n_symbols_by_volume", "concurrent_workers", "min_signal_strength"
    ],
    "إعدادات المخاطر": [
        "virtual_trade_size_percentage", "atr_sl_multiplier", "risk_reward_ratio", "trailing_sl_activate_percent", "trailing_sl_percent"
    ],
    "الفلاتر والاتجاه": [
        "market_regime_filter_enabled", "use_master_trend_filter", "fear_and_greed_filter_enabled", "fundamental_analysis_enabled", # New
        "master_adx_filter_level", "master_trend_filter_ma_period", "trailing_sl_enabled", "fear_and_greed_threshold"
    ]
}
PARAM_DISPLAY_NAMES = {
    "virtual_trade_size_percentage": "حجم الصفقة (%)",
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
    "fundamental_analysis_enabled": "فلتر الأخبار والبيانات", # New
}


# --- متغيرات الحالة العامة للبوت --- #
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}, "status_snapshot": {"last_scan_start_time": "N/A", "last_scan_end_time": "N/A", "markets_found": 0, "signals_found": 0, "active_trades_count": 0, "scan_in_progress": False}}
scan_lock = asyncio.Lock()

# --- إدارة الإعدادات --- #
DEFAULT_SETTINGS = {
    "virtual_portfolio_balance_usdt": 1000.0, "virtual_trade_size_percentage": 5.0, "max_concurrent_trades": 5, "top_n_symbols_by_volume": 250, "concurrent_workers": 10,
    "active_scanners": ["momentum_breakout", "breakout_squeeze_pro", "rsi_divergence", "supertrend_pullback"],
    "market_regime_filter_enabled": True, "use_master_trend_filter": True, "master_trend_filter_ma_period": 50, "master_adx_filter_level": 22,
    "fear_and_greed_filter_enabled": True, "fear_and_greed_threshold": 30,
    "fundamental_analysis_enabled": True, # [UPGRADE] New setting to enable/disable the news/events filter
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
    "active_preset_name": "Default"
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
            if [key for key in bot_data["settings"] if key not in DEFAULT_SETTINGS]:
                updated = True
            if updated: save_settings()
        else:
            bot_data["settings"] = DEFAULT_SETTINGS
            save_settings()
        logging.info(f"Settings loaded successfully from {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to load settings: {e}")
        bot_data["settings"] = DEFAULT_SETTINGS

def save_settings():
    try:
        # Prune obsolete keys before saving
        current_settings = bot_data["settings"].copy()
        for key in list(current_settings.keys()):
            if key not in DEFAULT_SETTINGS:
                del current_settings[key]
        bot_data["settings"] = current_settings
        with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
        logging.info(f"Settings saved successfully to {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")


# --- إدارة قاعدة البيانات --- #
def init_database():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, exchange TEXT, symbol TEXT, entry_price REAL, take_profit REAL, stop_loss REAL, quantity REAL, entry_value_usdt REAL, status TEXT, exit_price REAL, closed_at TEXT, exit_value_usdt REAL, pnl_usdt REAL, trailing_sl_active BOOLEAN, highest_price REAL, reason TEXT)
        ''')
        conn.commit()
        conn.close()
        logging.info(f"Database initialized successfully at: {DB_FILE}")
    except Exception as e:
        logging.error(f"Failed to initialize database at {DB_FILE}: {e}")

def log_recommendation_to_db(signal):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO trades (timestamp, exchange, symbol, entry_price, take_profit, stop_loss, quantity, entry_value_usdt, status, trailing_sl_active, highest_price, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'], signal['entry_price'], signal['take_profit'], signal['stop_loss'], signal['quantity'], signal['entry_value_usdt'], 'نشطة', False, signal['entry_price'], signal['reason']))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    except Exception as e:
        logging.error(f"Failed to log recommendation to DB: {e}")
        return None

# --- [UPGRADE] Section for Fundamental & Sentiment Analysis --- #
def get_high_impact_economic_events():
    """Fetches high-impact economic events for the current day."""
    if not INVESTPY_AVAILABLE:
        return [] # Return empty list if library is not available
    try:
        df = investpy.economic_calendar(
            countries=['united states', 'euro zone'],
            importances=['high']
        )
        today_str = datetime.now().strftime('%d/%m/%Y')
        today_events = df[df['date'] == today_str]
        if not today_events.empty:
            event_list = today_events['event'].tolist()
            logging.warning(f"High-impact economic events found for today: {event_list}")
            return event_list
        return []
    except Exception as e:
        logging.error(f"Could not fetch economic calendar data: {e}")
        return None # Return None on failure to distinguish from no events

def get_latest_crypto_news(limit=15):
    """Fetches latest crypto news headlines from various RSS feeds."""
    if not FEEDPARSER_AVAILABLE:
        return []
    urls = [
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://bitcoinmagazine.com/feed",
    ]
    headlines = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                headlines.append(entry.title)
        except Exception as e:
            logging.error(f"Could not fetch news from {url}: {e}")
    return list(set(headlines))[:limit]

def analyze_sentiment_of_headlines(headlines):
    """Analyzes a list of headlines and returns an average sentiment score."""
    if not NLTK_AVAILABLE or not headlines:
        return 0.0
    sia = SentimentIntensityAnalyzer()
    total_compound_score = sum(sia.polarity_scores(h)['compound'] for h in headlines)
    return total_compound_score / len(headlines)

async def get_fundamental_market_mood():
    """
    Determines the overall market mood based on economic events and news sentiment.
    Returns a tuple: (mood_string, reason_string)
    """
    # 1. Check for high-impact economic events
    high_impact_events = get_high_impact_economic_events()
    if high_impact_events is None:
        return "DANGEROUS", "فشل في جلب بيانات التقويم الاقتصادي."
    if high_impact_events:
        return "DANGEROUS", f"أحداث اقتصادية هامة اليوم: {', '.join(high_impact_events[:2])}..."

    # 2. Fetch and analyze news sentiment
    latest_headlines = get_latest_crypto_news()
    sentiment_score = analyze_sentiment_of_headlines(latest_headlines)
    logging.info(f"Market sentiment score based on news: {sentiment_score:.3f}")

    # 3. Determine final mood
    if sentiment_score >= 0.2:
        return "POSITIVE", f"مشاعر الأخبار إيجابية (النتيجة: {sentiment_score:.2f})."
    elif sentiment_score <= -0.2:
        return "NEGATIVE", f"مشاعر الأخبار سلبية (النتيجة: {sentiment_score:.2f})."
    else:
        return "NEUTRAL", f"مشاعر الأخبار محايدة (النتيجة: {sentiment_score:.2f})."

# --- وحدات المسح المتقدمة (Scanners) --- #
def find_col(df_columns, prefix):
    try: return next(col for col in df_columns if col.startswith(prefix))
    except StopIteration: return None
def analyze_momentum_breakout(df, params, rvol):
    df.ta.vwap(append=True); df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
    df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True); df.ta.rsi(length=params['rsi_period'], append=True)
    macd_col, macds_col, bbu_col, rsi_col = find_col(df.columns, "MACD_"), find_col(df.columns, "MACDs_"), find_col(df.columns, "BBU_"), find_col(df.columns, "RSI_")
    if not all([macd_col, macds_col, bbu_col, rsi_col]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    volume_ok = last['volume'] > (df['volume'].rolling(20).mean().iloc[-2] * params['volume_spike_multiplier'])
    rvol_ok = rvol >= bot_data['settings']['liquidity_filters']['min_rvol']
    if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and last[rsi_col] < params['rsi_max_level'] and volume_ok and rvol_ok):
        return {"reason": "Momentum Breakout", "type": "long"}
    return None
def analyze_breakout_squeeze_pro(df, params, rvol):
    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True); df.ta.kc(length=params['keltner_period'], scalar=params['keltner_atr_multiplier'], append=True); df.ta.obv(append=True)
    bbu_col, bbl_col, kcu_col, kcl_col = find_col(df.columns, "BBU_"), find_col(df.columns, "BBL_"), find_col(df.columns, "KCUe_"), find_col(df.columns, "KCLEe_")
    if not all([bbu_col, bbl_col, kcu_col, kcl_col]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    if prev[bbl_col] > prev[kcl_col] and prev[bbu_col] < prev[kcu_col]:
        if (last['close'] > last[bbu_col] and (not params['volume_confirmation_enabled'] or last['volume'] > df['volume'].rolling(20).mean().iloc[-2] * 1.5) and (rvol >= bot_data['settings']['liquidity_filters']['min_rvol']) and (df['OBV'].iloc[-2] > df['OBV'].iloc[-3])):
            return {"reason": "Squeeze Breakout", "type": "long"}
    return None
def find_divergence_points(series, lookback):
    if not SCIPY_AVAILABLE: return [], []
    peaks, _ = find_peaks(series, distance=lookback); troughs, _ = find_peaks(-series, distance=lookback)
    return peaks, troughs
def analyze_rsi_divergence(df, params):
    if not SCIPY_AVAILABLE: return None
    df.ta.rsi(length=params['rsi_period'], append=True)
    rsi_col = find_col(df.columns, f"RSI_{params['rsi_period']}")
    if not rsi_col or df[rsi_col].isnull().all(): return None
    subset = df.iloc[-params['lookback_period']:].copy()
    price_troughs_idx, _ = find_divergence_points(-subset['low'], params['peak_trough_lookback']); rsi_troughs_idx, _ = find_divergence_points(-subset[rsi_col], params['peak_trough_lookback'])
    if len(price_troughs_idx) >= 2 and len(rsi_troughs_idx) >= 2:
        p_low1_idx, p_low2_idx = price_troughs_idx[-2], price_troughs_idx[-1]; r_low1_idx, r_low2_idx = rsi_troughs_idx[-2], rsi_troughs_idx[-1]
        is_divergence = (subset.iloc[p_low2_idx]['low'] < subset.iloc[p_low1_idx]['low'] and subset.iloc[r_low2_idx][rsi_col] > subset.iloc[r_low1_idx][rsi_col])
        if is_divergence:
            rsi_exits_oversold = (subset.iloc[r_low1_idx][rsi_col] < 35 and subset.iloc[-2][rsi_col] > 40)
            price_confirmed = df.iloc[-2]['close'] > subset.iloc[p_low2_idx:]['high'].max()
            if (not params['confirm_with_rsi_exit'] or rsi_exits_oversold) and price_confirmed:
                return {"reason": "RSI Divergence", "type": "long"}
    return None
def analyze_supertrend_pullback(df, params, rvol, adx_value):
    df.ta.supertrend(length=params['atr_period'], multiplier=params['atr_multiplier'], append=True)
    st_dir_col, ema_col = find_col(df.columns, f"SUPERTd_{params['atr_period']}_"), find_col(df.columns, 'EMA_')
    if not st_dir_col or not ema_col: return None
    last, prev = df.iloc[-2], df.iloc[-3]
    if prev[st_dir_col] == -1 and last[st_dir_col] == 1:
        settings = bot_data['settings']
        breakout_ok = last['close'] > df['high'].iloc[-params.get('swing_high_lookback', 10):-2].max()
        if (last['close'] > last[ema_col] and adx_value >= settings['master_adx_filter_level'] and rvol >= settings['liquidity_filters']['min_rvol'] and breakout_ok):
            return {"reason": "Supertrend Flip", "type": "long"}
    return None

SCANNERS = {"momentum_breakout": analyze_momentum_breakout, "breakout_squeeze_pro": analyze_breakout_squeeze_pro, "rsi_divergence": analyze_rsi_divergence, "supertrend_pullback": analyze_supertrend_pullback}

# --- الدوال الأساسية للبوت --- #
async def initialize_exchanges():
    async def connect(ex_id):
        try:
            exchange = getattr(ccxt, ex_id)({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logging.info(f"Connected to {ex_id} (spot markets only).")
        except Exception as e:
            logging.error(f"Failed to connect to {ex_id}: {e}")
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
    logging.info(f"Aggregated markets. Found {len(all_tickers)} tickers -> Post-filter: {len(usdt_tickers)} -> Selected top {len(final_list)} unique pairs.")
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
    except Exception as e: return None, f"Error: {e}"

async def worker(queue, results_list, settings, failure_counter):
    while not queue.empty():
        market_info = await queue.get()
        symbol, exchange = market_info.get('symbol', 'N/A'), bot_data["exchanges"].get(market_info['exchange'])
        if not exchange: queue.task_done(); continue
        try:
            logging.info(f"--- Checking {symbol} on {exchange.id} ---")
            liq_filters, vol_filters, ema_filters = settings['liquidity_filters'], settings['volatility_filters'], settings['ema_trend_filter']
            orderbook = await exchange.fetch_order_book(symbol, limit=20)
            if not orderbook or not orderbook['bids'] or not orderbook['asks']: logging.info(f"Reject {symbol}: No order book."); continue
            best_bid, best_ask = orderbook['bids'][0][0], orderbook['asks'][0][0]
            spread_percent = ((best_ask - best_bid) / best_ask) * 100
            if spread_percent > liq_filters['max_spread_percent']: logging.info(f"Reject {symbol}: High Spread ({spread_percent:.2f}%)"); continue
            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=200)
            if len(ohlcv) < 100: logging.info(f"Skipping {symbol}: Not enough data."); continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)
            rvol = (df['volume'].iloc[-2] / ta.sma(df['volume'], length=liq_filters['rvol_period']).iloc[-2]) if ta.sma(df['volume'], length=liq_filters['rvol_period']).iloc[-2] > 0 else 0
            if rvol < liq_filters['min_rvol']: logging.info(f"Reject {symbol}: Low RVOL ({rvol:.2f})"); continue
            df.ta.atr(length=vol_filters['atr_period_for_filter'], append=True); atr_col_name = find_col(df.columns, 'ATRr_')
            atr_percent = (df[atr_col_name].iloc[-2] / df['close'].iloc[-2]) * 100 if df['close'].iloc[-2'] > 0 else 0
            if atr_percent < vol_filters['min_atr_percent']: logging.info(f"Reject {symbol}: Low ATR% ({atr_percent:.2f}%)"); continue
            if ema_filters['enabled']:
                df.ta.ema(length=ema_filters['ema_period'], append=True); ema_col_name = f"EMA_{ema_filters['ema_period']}"
                if df['close'].iloc[-2] < df[ema_col_name].iloc[-2]: logging.info(f"Reject {symbol}: Below EMA{ema_filters['ema_period']}"); continue
            if settings.get('use_master_trend_filter'):
                is_htf_bullish, reason = await get_higher_timeframe_trend(exchange, symbol, settings['master_trend_filter_ma_period'])
                if is_htf_bullish is False: logging.info(f"HTF Trend Filter FAILED for {symbol}: {reason}"); continue
            df.ta.adx(append=True); adx_col = find_col(df.columns, 'ADX_'); adx_value = df[adx_col].iloc[-2] if adx_col and not pd.isna(df[adx_col].iloc[-2]) else 0
            if settings.get('use_master_trend_filter') and adx_value < settings['master_adx_filter_level']: logging.info(f"ADX Filter FAILED for {symbol}: {adx_value:.2f}"); continue
            
            confirmed_reasons = []
            for name, func in SCANNERS.items():
                if name in settings['active_scanners']:
                    args = {'df': df.copy(), 'params': settings.get(name, {})}
                    if name in ["momentum_breakout", "breakout_squeeze_pro", "supertrend_pullback"]: args['rvol'] = rvol
                    if name == "supertrend_pullback": args['adx_value'] = adx_value
                    if result := func(**args): confirmed_reasons.append(result['reason'])
            
            if len(confirmed_reasons) >= settings.get("min_signal_strength", 1):
                reason_str = ' + '.join(confirmed_reasons)
                logging.info(f"SIGNAL FOUND for {symbol} with strength {len(confirmed_reasons)} via {reason_str}")
                entry_price = df.iloc[-2]['close']
                df.ta.atr(length=settings['atr_period'], append=True); current_atr = df[find_col(df.columns, f"ATRr_{settings['atr_period']}")].iloc[-2]
                if settings.get("use_dynamic_risk_management", False) and current_atr > 0:
                    risk = current_atr * settings['atr_sl_multiplier']
                    sl, tp = entry_price - risk, entry_price + (risk * settings['risk_reward_ratio'])
                else:
                    sl = entry_price * (1 - settings['stop_loss_percentage'] / 100)
                    tp = entry_price * (1 + settings['take_profit_percentage'] / 100)
                if ((tp - entry_price) / entry_price * 100) >= settings['min_tp_sl_filter']['min_tp_percent'] and ((entry_price - sl) / entry_price * 100) >= settings['min_tp_sl_filter']['min_sl_percent']:
                    results_list.append({"symbol": symbol, "exchange": market_info['exchange'].capitalize(), "entry_price": entry_price, "take_profit": tp, "stop_loss": sl, "timestamp": df.index[-2], "reason": reason_str, "strength": len(confirmed_reasons)})
        except Exception as e: logging.error(f"CRITICAL ERROR in worker for {symbol}: {e}", exc_info=False); failure_counter[0] += 1
        finally: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    async with scan_lock:
        if bot_data['status_snapshot']['scan_in_progress']:
            logging.warning("Scan attempted while another was in progress. Skipped.")
            return

        settings = bot_data["settings"]

        # --- [UPGRADE] Fundamental & Sentiment Analysis Check ---
        if settings.get("fundamental_analysis_enabled", True):
            market_mood, mood_reason = await get_fundamental_market_mood()
            logging.info(f"Fundamental Market Mood Check: {market_mood}. Reason: {mood_reason}")
            if market_mood in ["DANGEROUS", "NEGATIVE"]:
                logging.warning(f"Scan aborted due to {market_mood} market mood. No new long positions will be opened.")
                await send_telegram_message(context.bot, {
                    'custom_message': f"⚠️ *تنبيه:* تم إيقاف الفحص مؤقتاً.\n*السبب:* `{mood_reason}`",
                    'target_chat': TELEGRAM_CHAT_ID
                })
                return # Stop the scan completely

        # --- Technical & Fear/Greed Regime Filter ---
        if settings.get('market_regime_filter_enabled', True):
            is_market_ok, reason = await check_market_regime()
            if not is_market_ok:
                logging.info(f"Skipping scan due to technical regime filter: {reason}")
                return

        status = bot_data['status_snapshot']
        status.update({"scan_in_progress": True, "last_scan_start_time": datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), "signals_found": 0})
        
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'"); active_trades_count = cursor.fetchone()[0]; conn.close()
        except Exception as e: logging.error(f"DB Error in perform_scan: {e}"); active_trades_count = settings.get("max_concurrent_trades", 5)

        top_markets = await aggregate_top_movers()
        if not top_markets: logging.info("Scan complete: No markets to scan."); status['scan_in_progress'] = False; return

        queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
        signals, failure_counter = [], [0]
        worker_tasks = [asyncio.create_task(worker(queue, signals, settings, failure_counter)) for _ in range(settings['concurrent_workers'])]
        await queue.join(); [task.cancel() for task in worker_tasks]

        signals.sort(key=lambda s: s.get('strength', 0), reverse=True)
        new_trades, opportunities = 0, 0
        last_signal_time = bot_data['last_signal_time']

        for signal in signals:
            if time.time() - last_signal_time.get(signal['symbol'], 0) <= (SCAN_INTERVAL_SECONDS * 4): continue
            trade_amount_usdt = settings["virtual_portfolio_balance_usdt"] * (settings["virtual_trade_size_percentage"] / 100)
            signal.update({'quantity': trade_amount_usdt / signal['entry_price'], 'entry_value_usdt': trade_amount_usdt})

            if active_trades_count < settings.get("max_concurrent_trades", 5):
                if trade_id := log_recommendation_to_db(signal):
                    signal['trade_id'] = trade_id; await send_telegram_message(context.bot, signal, is_new=True)
                    active_trades_count += 1; new_trades += 1
            else:
                await send_telegram_message(context.bot, signal, is_opportunity=True); opportunities += 1
            await asyncio.sleep(0.5)
            last_signal_time[signal['symbol']] = time.time()

        failures = failure_counter[0]
        logging.info(f"Scan complete. Found: {len(signals)}, Entered: {new_trades}, Opportunities: {opportunities}, Failures: {failures}.")
        summary_message = f"🔹 *ملخص الفحص* 🔹\n\n▫️ إجمالي الإشارات: *{len(signals)}*\n✅ صفقات جديدة: *{new_trades}*\n💡 فرص إضافية: *{opportunities}*\n⚠️ عملات فشل تحليلها: *{failures}*"
        await send_telegram_message(context.bot, {'custom_message': summary_message, 'target_chat': TELEGRAM_CHAT_ID})
        status.update({'signals_found': new_trades + opportunities, 'last_scan_end_time': datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), 'scan_in_progress': False})

async def send_telegram_message(bot, signal_data, is_new=False, is_opportunity=False, update_type=None):
    message, keyboard, target_chat = "", None, TELEGRAM_CHAT_ID
    def format_price(p): return f"{p:,.8f}" if p < 0.01 else f"{p:,.4f}"

    if 'custom_message' in signal_data:
        message, target_chat = signal_data['custom_message'], signal_data.get('target_chat', TELEGRAM_CHAT_ID)
    elif is_new or is_opportunity:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        strength_stars = '⭐' * signal_data.get('strength', 1)
        title = f"✅ توصية صفقة جديدة ({strength_stars}) ✅" if is_new else f"💡 فرصة تداول محتملة ({strength_stars}) 💡"
        entry, tp, sl = signal_data['entry_price'], signal_data['take_profit'], signal_data['stop_loss']
        tp_percent, sl_percent = ((tp - entry) / entry * 100), ((sl - entry) / entry * 100)
        message = (f"{title}\n\n"
                   f"▫️ العملة: `{signal_data['symbol']}`\n▫️ المنصة: *{signal_data['exchange']}*\n"
                   f"▫️ الاستراتيجية: `{signal_data['reason']}`\n"
                   f"{f'▫️ قيمة الصفقة: *${signal_data['entry_value_usdt']:,.2f}*' if is_new else ''}\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"📈 سعر الدخول: *{format_price(entry)} $*\n"
                   f"🎯 الهدف: *{format_price(tp)} $* `({tp_percent:+.2f}%)`\n"
                   f"🛑 الوقف: *{format_price(sl)} $* `({sl_percent:+.2f}%)`"
                   f"{f'\n*للمتابعة، استخدم الأمر `/check {signal_data['trade_id']}`*' if is_new else ''}")
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين صفقة* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى نقطة الدخول: `${format_price(signal_data['new_sl'])}`."

    if not message: return
    for _ in range(3):
        try:
            await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard); return
        except RetryAfter as e: await asyncio.sleep(e.retry_after)
        except TimedOut: await asyncio.sleep(5)
        except Exception as e: logging.error(f"Failed to send msg to {target_chat}: {e}"); await asyncio.sleep(2)
    logging.error(f"Failed to send msg to {target_chat} after 3 attempts.")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'"); active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e: logging.error(f"DB error in track_open_trades: {e}"); return
    bot_data['status_snapshot']['active_trades_count'] = len(active_trades)
    if not active_trades: return

    async def check_trade(trade):
        exchange = bot_data["exchanges"].get(trade['exchange'].lower())
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last', ticker.get('close'))
            if not current_price: return None
            if current_price >= trade['take_profit']: return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']: return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price}
            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade.get('highest_price', current_price), current_price)
                if not trade.get('trailing_sl_active') and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price']
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade.get('trailing_sl_active'):
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
            if current_price > trade.get('highest_price', 0): return {'id': trade['id'], 'status': 'update_peak', 'highest_price': current_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db, portfolio_pnl = [], 0.0
    for result in filter(None, results):
        original_trade = next((t for t in active_trades if t['id'] == result['id']), None)
        if not original_trade: continue
        status = result['status']
        if status in ['ناجحة', 'فاشلة']:
            pnl = (result['exit_price'] - original_trade['entry_price']) * original_trade['quantity']; portfolio_pnl += pnl
            closed_at_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')
            updates_to_db.append(("UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?", (status, result['exit_price'], closed_at_str, result['exit_price'] * original_trade['quantity'], pnl, result['id'])))
            
            # [UPGRADE] Detailed trade closure message
            pnl_percent = (pnl / original_trade['entry_value_usdt'] * 100) if original_trade.get('entry_value_usdt', 0) > 0 else 0
            icon, reason_str, reason_icon = ("✅", "Take Profit Hit", "🎯") if status == 'ناجحة' else ("❌", "Stop Loss Hit", "🛑")
            def format_price(p): return f"{p:,.8f}" if p < 0.01 else f"{p:,.4f}"
            message = (f"{icon} **تم إغلاق الصفقة #{original_trade['id']}** {icon}\n\n"
                       f"{reason_icon} **السبب:** *{reason_str}*\n"
                       f"▫️ **العملة:** `{original_trade['symbol']}`\n"
                       f"▫️ **الاستراتيجية:** `{original_trade.get('reason', 'N/A')}`\n"
                       f"━━━━━━━━━━━━━━\n"
                       f"📈 **سعر الدخول:** `{format_price(original_trade['entry_price'])}`\n"
                       f"📉 **سعر الخروج:** `{format_price(result['exit_price'])}`\n"
                       f"💰 **الربح/الخسارة:** **`${pnl:+.2f}`** (`{pnl_percent:+.2f}%`)")
            await send_telegram_message(context.bot, {'custom_message': message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})

        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl': updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], result['id'])))
        elif status == 'update_peak': updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], result['id'])))
    if updates_to_db:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            [cursor.execute(q, p) for q, p in updates_to_db]; conn.commit(); conn.close()
        except Exception as e: logging.error(f"DB update failed in track_open_trades: {e}")
    if portfolio_pnl != 0.0: bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl; save_settings(); logging.info(f"Portfolio balance updated by ${portfolio_pnl:.2f}.")

def get_fear_and_greed_index():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10); r.raise_for_status()
        return int(r.json().get('data', [{}])[0].get('value', 50))
    except Exception as e: logging.error(f"Could not fetch F&G Index: {e}"); return None

async def check_market_regime():
    settings = bot_data['settings']
    is_technically_bullish, is_sentiment_bullish = True, True
    try:
        if not (binance := bot_data["exchanges"].get('binance')): logging.warning("Binance not available for regime check."); return True, "Binance unavailable"
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma50'] = ta.sma(df['close'], length=50)
        is_technically_bullish = df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception as e: logging.error(f"Error checking BTC trend: {e}")
    if settings.get("fear_and_greed_filter_enabled", True):
        if (fng_index := get_fear_and_greed_index()) is not None: is_sentiment_bullish = fng_index >= settings.get("fear_and_greed_threshold", 30)
    if not is_technically_bullish: return False, "Bearish regime (BTC below 4h SMA50)."
    if not is_sentiment_bullish: return False, f"Extreme fear (F&G: {fng_index} < {settings.get('fear_and_greed_threshold')})."
    return True, "Market regime is favorable for longs."

def generate_performance_report_string():
    if not os.path.exists(DB_FILE): return "❌ لم يتم العثور على قاعدة البيانات."
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("SELECT reason, status, entry_price, highest_price FROM trades WHERE status IN ('ناجحة', 'فاشلة') AND timestamp >= ?", (start_date,)); trades = cursor.fetchall(); conn.close()
    except Exception as e: return f"❌ خطأ: {e}"
    if not trades: return "ℹ️ لا توجد صفقات مغلقة في آخر 30 يومًا."
    stats = {}
    for trade in trades:
        reason = trade['reason']
        if reason not in stats: stats[reason] = {'total': 0, 'successful': 0, 'max_profits': []}
        stats[reason]['total'] += 1
        if trade['status'] == 'ناجحة': stats[reason]['successful'] += 1
        if trade['entry_price'] > 0 and trade['highest_price']:
            stats[reason]['max_profits'].append(((trade['highest_price'] - trade['entry_price']) / trade['entry_price']) * 100)
    lines = ["📊 **تقرير أداء الاستراتيجيات (آخر 30 يومًا)** 📊", "="*35]
    for reason, data in sorted(stats.items(), key=lambda i: i[1]['total'], reverse=True):
        if (total := data['total']) == 0: continue
        win_rate = (data['successful'] / total) * 100
        avg_profit = sum(data['max_profits']) / len(data['max_profits']) if data['max_profits'] else 0
        lines.extend([f"--- **{reason}** ---", f"- **إجمالي:** {total}", f"- **نجاح:** {win_rate:.1f}%", f"- **متوسط أقصى ربح:** {avg_profit:.2f}%", ""])
    return "\n".join(lines)

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [["📊 الإحصائيات", "📈 الصفقات النشطة"], ["📜 تقرير الاستراتيجيات", "⚙️ الإعدادات"], ["👀 ماذا يجري في الخلفية؟", "🔬 فحص يدوي الآن"],["ℹ️ مساعدة"]]
settings_menu_keyboard = [["🎭 تفعيل/تعطيل الماسحات", "🏁 أنماط جاهزة"], ["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في بوت المحلل الآلي! (v26 - News Aware)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))
async def scan_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot'].get('scan_in_progress', False): await update.message.reply_text("⚠️ فحص آخر قيد التنفيذ."); return
    await update.message.reply_text("⏳ جاري بدء الفحص اليدوي..."); context.job_queue.run_once(perform_scan, 0, name='manual_scan')
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message or update.callback_query.message
    await target.reply_text("اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
def get_scanners_keyboard():
    active = bot_data["settings"].get("active_scanners", [])
    keys = [[InlineKeyboardButton(f"{'✅' if name in active else '❌'} {name}", callback_data=f"toggle_{name}")] for name in SCANNERS.keys()]
    keys.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keys)
def get_presets_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚦 احترافية (متوازنة)", callback_data="preset_PRO"), InlineKeyboardButton("🎯 متشددة", callback_data="preset_STRICT")],
        [InlineKeyboardButton("🌙 متساهلة", callback_data="preset_LAX"), InlineKeyboardButton("⚠️ فائق التساهل", callback_data="preset_VERY_LAX")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")]
    ])
async def show_presets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message or update.callback_query.message
    await target.reply_text("اختر نمط إعدادات جاهز:", reply_markup=get_presets_keyboard())
async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message or update.callback_query.message
    await target.reply_text("اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
async def toggle_scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, name = update.callback_query, "_".join(query.data.split("_")[1:])
    active = bot_data["settings"].get("active_scanners", []).copy()
    if name in active: active.remove(name)
    else: active.append(name)
    bot_data["settings"]["active_scanners"] = active; save_settings()
    try: await query.edit_message_text(text="اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
    except BadRequest as e:
        if "Message is not modified" not in str(e): raise
async def show_parameters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    settings = bot_data["settings"]
    for category, params in EDITABLE_PARAMS.items():
        keyboard.append([InlineKeyboardButton(f"--- {category} ---", callback_data="ignore")])
        row = []
        for key in params:
            name = PARAM_DISPLAY_NAMES.get(key, key)
            val = settings.get(key, "N/A")
            text = f"{name}: {'مُفعّل ✅' if isinstance(val, bool) and val else 'مُعطّل ❌' if isinstance(val, bool) else val}"
            row.append(InlineKeyboardButton(text, callback_data=f"param_{key}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    target, text = (update.message or update.callback_query.message), "⚙️ *الإعدادات المتقدمة* ⚙️\n\nاختر الإعداد لتعديله:"
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except BadRequest as e:
            if "Message is not modified" not in str(e): logging.error(f"Error editing params menu: {e}")
    else:
        sent = await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.user_data['settings_menu_id'] = sent.message_id
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("*مساعدة البوت*\n`/start` - بدء\n`/scan` - فحص يدوي\n`/report` - تقرير يومي\n`/strategyreport` - تقرير أداء الاستراتيجيات\n`/check <ID>` - متابعة صفقة\n`/debug` - فحص الحالة", parse_mode=ParseMode.MARKDOWN)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor(); cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status")
        stats, counts, pnl = cursor.fetchall(), {}, {}
        for s, c, p in stats: counts[s] = c; pnl[s] = p or 0
        total, active, successful, failed = sum(counts.values()), counts.get('نشطة', 0), counts.get('ناجحة', 0), counts.get('فاشلة', 0)
        win_rate = (successful / (successful + failed) * 100) if (successful + failed) > 0 else 0
        msg = (f"*📊 إحصائيات المحفظة*\n\n"
               f"📈 *الرصيد الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n"
               f"💰 *إجمالي الربح/الخسارة:* `${sum(pnl.values()):+.2f}`\n"
               f"⚙️ *النمط الحالي:* `{bot_data['settings'].get('active_preset_name', 'N/A')}`\n\n"
               f"- *إجمالي الصفقات:* `{total}` (`{active}` نشطة)\n"
               f"- *الناجحة:* `{successful}` | *الربح:* `${pnl.get('ناجحة', 0):.2f}`\n"
               f"- *الفاشلة:* `{failed}` | *الخسارة:* `${abs(pnl.get('فاشلة', 0)):.2f}`\n"
               f"- *معدل النجاح:* `{win_rate:.2f}%`")
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Error in stats_command: {e}"); await update.message.reply_text("خطأ في جلب الإحصائيات.")
async def strategy_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إعداد التقرير..."); report = generate_performance_report_string()
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d'); logging.info("Generating daily report...")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
        cursor.execute("SELECT status, pnl_usdt FROM trades WHERE DATE(closed_at) = ?", (today,)); closed = cursor.fetchall(); conn.close()
        if not closed: msg = f"🗓️ *التقرير اليومي ليوم {today}*\n\nلم يتم إغلاق أي صفقات اليوم."
        else:
            wins, losses, total_pnl = 0, 0, sum(pnl for _, pnl in closed if pnl is not None)
            for status, _ in closed:
                if status == 'ناجحة': wins += 1
                else: losses += 1
            win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
            msg = (f"🗓️ *التقرير اليومي ليوم {today}*\n\n"
                   f"▫️ *إجمالي الصفقات المغلقة:* `{wins + losses}`\n✅ *الرابحة:* `{wins}` | ❌ *الخاسرة:* `{losses}`\n\n"
                   f"📈 *معدل النجاح:* `{win_rate:.2f}%`\n💰 *الربح/الخسارة:* `${total_pnl:+.2f}`")
        await send_telegram_message(context.bot, {'custom_message': msg, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})
    except Exception as e: logging.error(f"Failed to generate daily report: {e}")
async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إرسال التقرير اليومي..."); await send_daily_report(context)
    await update.message.reply_text("✅ تم إرسال التقرير بنجاح إلى القناة.")
async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']; next_scan = "N/A"
    if not status['scan_in_progress'] and context.job_queue:
        if jobs := context.job_queue.get_jobs_by_name('perform_scan'):
            if jobs[0].next_t: next_scan = jobs[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S')
    msg = (f"🤖 *حالة البوت في الخلفية*\n\n*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n"
           f"- *آخر فحص:* `{status['last_scan_end_time']}`\n- *العملات المفحوصة:* `{status['markets_found']}`\n"
           f"- *الإشارات الجديدة:* `{status['signals_found']}`\n- *الصفقات النشطة:* `{status['active_trades_count']}`\n- *الفحص التالي:* `{next_scan}`")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إعداد تقرير التشخيص..."); settings = bot_data.get("settings", {})
    parts = ["🩺 *تقرير التشخيص والحالة* 🩺", "\n*--- الإعدادات العامة ---*", f"- *النمط النشط:* `{settings.get('active_preset_name', 'N/A')}`", f"- *الماسحات النشطة:* `{', '.join(settings.get('active_scanners', ['None']))}`", "\n*--- إعدادات المخاطر ---*", f"- *رأس المال:* `${settings.get('virtual_portfolio_balance_usdt', 0):,.2f}`", f"- *حجم الصفقة:* `{settings.get('virtual_trade_size_percentage', 0)}%`", f"- *مضاعف ATR SL:* `{settings.get('atr_sl_multiplier', 0)}`", f"- *نسبة R:R:* `1:{settings.get('risk_reward_ratio', 0)}`", "\n*--- حالة المهام ---*"]
    if context.job_queue and context.job_queue.jobs():
        for job in context.job_queue.jobs(): parts.append(f"- *{job.name}:* `{job.next_t.astimezone(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S') if job.next_t else 'Not scheduled'}`")
    else: parts.append("- `🔴 لا توجد مهام مجدولة!`")
    parts.append("\n*--- حالة المنصات ---*")
    for ex_id in EXCHANGES_TO_SCAN: parts.append(f"- *{ex_id.capitalize()}:* {'✅ متصل' if ex_id in bot_data.get('exchanges', {}) else '❌ غير متصل'}")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)
async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    target = update.callback_query.message if trade_id_from_callback else update.message
    def format_price(p): return f"{p:,.8f}" if p < 0.01 else f"{p:,.4f}"
    try:
        trade_id = trade_id_from_callback or int(context.args[0])
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor(); cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)); trade = dict(cursor.fetchone()); conn.close()
        if not trade: await target.reply_text(f"لم يتم العثور على صفقة بالرقم `{trade_id}`."); return
        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            msg = f"📋 *ملخص الصفقة #{trade_id}*\n\n*العملة:* `{trade['symbol']}`\n*الحالة:* `{trade['status']}`\n*تاريخ الإغلاق:* `{trade['closed_at']}`\n*الربح/الخسارة:* `${trade.get('pnl_usdt', 0):+.2f} ({pnl_percent:+.2f}%)`"
        else:
            if not (exchange := bot_data["exchanges"].get(trade['exchange'].lower())): await target.reply_text("المنصة غير متصلة."); return
            if not (current_price := (await exchange.fetch_ticker(trade['symbol'])).get('last')): await target.reply_text(f"لم أتمكن من جلب السعر لـ `{trade['symbol']}`."); return
            pnl = (current_price - trade['entry_price']) * trade['quantity']
            pnl_percent = (pnl / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            msg = (f"📈 *متابعة حية للصفقة #{trade_id}*\n\n"
                   f"▫️ *العملة:* `{trade['symbol']}` | *الحالة:* `نشطة`\n"
                   f"▫️ *سعر الدخول:* `${format_price(trade['entry_price'])}`\n"
                   f"▫️ *السعر الحالي:* `${format_price(current_price)}`\n\n"
                   f"💰 *الربح/الخسارة الحالية:*\n`${pnl:+.2f} ({pnl_percent:+.2f}%)`")
        await target.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await target.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e: logging.error(f"Error in check_trade_command: {e}"); await target.reply_text("حدث خطأ.")
async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, entry_value_usdt, exchange FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
        if not (trades := cursor.fetchall()): await update.message.reply_text("لا توجد صفقات نشطة حالياً."); return
        keys = [[InlineKeyboardButton(f"#{t['id']} | {t['symbol']} | ${t['entry_value_usdt']:.2f} | {t['exchange']}", callback_data=f"check_{t['id']}")] for t in trades]
        await update.message.reply_text("اختر صفقة لمتابعتها:", reply_markup=InlineKeyboardMarkup(keys))
    except Exception as e: logging.error(f"Error in show_active_trades: {e}"); await update.message.reply_text("خطأ في جلب الصفقات.")
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    if data.startswith("preset_"):
        preset_name = data.split("_", 1)[1]
        if preset_data := PRESETS.get(preset_name):
            for key, value in preset_data.items():
                if isinstance(bot_data["settings"].get(key), dict): bot_data["settings"][key].update(value)
                else: bot_data["settings"][key] = value
            bot_data["settings"]["active_preset_name"] = preset_name; save_settings()
            titles = {"PRO": "احترافي", "STRICT": "متشدد", "LAX": "متساهل", "VERY_LAX": "فائق التساهل"}
            lf, vf = preset_data['liquidity_filters'], preset_data['volatility_filters']
            text = f"✅ *تم تفعيل النمط: {titles.get(preset_name, preset_name)}*\n\n*أهم القيم:*\n`- min_rvol: {lf['min_rvol']}`\n`- max_spread: {lf['max_spread_percent']}%`\n`- min_atr: {vf['min_atr_percent']}%`"
            try: await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_presets_keyboard())
            except BadRequest as e:
                if "Message is not modified" not in str(e): raise
    elif data.startswith("param_"):
        key = data.split("_", 1)[1]; name = PARAM_DISPLAY_NAMES.get(key, key); val = bot_data["settings"].get(key, "N/A")
        context.user_data.update({'awaiting_input_for_param': key, 'settings_menu_id': query.message.message_id})
        if isinstance(val, bool):
            bot_data["settings"][key] = not val; bot_data["settings"]["active_preset_name"] = "Custom"; save_settings()
            await query.answer(f"✅ تم تبديل '{name}'"); await show_parameters_menu(update, context)
        else: await query.edit_message_text(f"📝 *تعديل '{name}'*\n\n*القيمة الحالية:* `{val}`\n\nالرجاء إرسال القيمة الجديدة.", parse_mode=ParseMode.MARKDOWN)
    elif data == "ignore": return
    elif data.startswith("toggle_"): await toggle_scanner_callback(update, context)
    elif data == "back_to_settings":
        if query.message: await query.message.delete()
        await context.bot.send_message(query.message.chat_id, "اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif data.startswith("check_"): await check_trade_command(update, context, trade_id_from_callback=int(data.split("_")[1]))

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input_for_param' in context.user_data:
        param, val_str, menu_id = context.user_data.pop('awaiting_input_for_param'), update.message.text, context.user_data.pop('settings_menu_id', None)
        await context.bot.delete_message(update.message.chat_id, update.message.message_id)
        settings = bot_data["settings"]
        try:
            current = settings[param]
            new = val_str.lower() in ['true', '1', 'yes', 'on', 'نعم'] if isinstance(current, bool) else type(current)(val_str)
            settings[param] = new; settings["active_preset_name"] = "Custom"; save_settings()
            if menu_id: context.user_data['settings_menu_id'] = menu_id; await show_parameters_menu(update, context)
            confirm = await update.message.reply_text(f"✅ تم تحديث **{PARAM_DISPLAY_NAMES.get(param, param)}** إلى `{new}`.", parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(4); await context.bot.delete_message(confirm.chat_id, confirm.message_id)
        except (ValueError, KeyError):
            context.user_data.update({'awaiting_input_for_param': param, 'settings_menu_id': menu_id})
            if menu_id: await context.bot.edit_message_text(update.message.chat_id, menu_id, "❌ قيمة غير صالحة. حاول مرة أخرى.")
            await asyncio.sleep(3); await show_parameters_menu(update, context)
        return
    handlers = {"📊 الإحصائيات": stats_command, "📈 الصفقات النشطة": show_active_trades_command, "ℹ️ مساعدة": help_command, "⚙️ الإعدادات": show_settings_menu, "👀 ماذا يجري في الخلفية؟": background_status_command, "🔬 فحص يدوي الآن": scan_now_command, "🔧 تعديل المعايير": show_parameters_menu, "🔙 القائمة الرئيسية": start_command, "🎭 تفعيل/تعطيل الماسحات": show_scanners_menu, "🏁 أنماط جاهزة": show_presets_menu, "📜 تقرير الاستراتيجيات": strategy_report_command}
    if (text := update.message.text) in handlers: await handlers[text](update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None: logging.error(f"Exception: {context.error}", exc_info=context.error)
async def post_init(application: Application):
    logging.info("Post-init: Initializing exchanges...")
    await initialize_exchanges()
    if not bot_data["exchanges"]: logging.critical("CRITICAL: Failed to connect to any exchange. Bot will not run properly."); return
    logging.info("Exchanges initialized. Setting up job queue...")
    if jq := application.job_queue:
        jq.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
        jq.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
        jq.run_daily(send_daily_report, time=dt_time(hour=23, minute=55, tzinfo=EGYPT_TZ), name='daily_report')
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *بوت المحلل الآلي (v26) جاهز للعمل!*", parse_mode=ParseMode.MARKDOWN)
    logging.info("Bot startup sequence complete.")
async def post_shutdown(application: Application):
    await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()])
    logging.info("All exchange connections have been closed gracefully.")

def main():
    print("🚀 Starting Pro Trading Analyst Bot (v26)...")
    load_settings(); init_database()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start_command)); app.add_handler(CommandHandler("scan", scan_now_command))
    app.add_handler(CommandHandler("report", daily_report_command)); app.add_handler(CommandHandler("check", check_trade_command))
    app.add_handler(CommandHandler("debug", debug_command)); app.add_handler(CommandHandler("strategyreport", strategy_report_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler)); app.add_error_handler(error_handler)
    print("✅ Bot is now polling for updates...")
    app.run_polling()

if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error in main loop: {e}", exc_info=True)

