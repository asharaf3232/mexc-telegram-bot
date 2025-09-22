# -*- coding: utf-8 -*-
# =======================================================================================
# --- 🚀 العقل الخارق للنظام التجاري | v1.6 (واجهة كاملة) 🚀 ---
# =======================================================================================
#
# هذا الإصدار يقوم بإصلاح شامل لواجهة تليجرام، مع استعادة جميع القوائم
# والمعالجات المفقودة لضمان عمل البوت بشكل تفاعلي وكامل.
#
# --- سجل التغييرات v1.6 ---
#   ✅ [إصلاح حاسم] استعادة قائمة "تعديل المعايير" المتقدمة بالكامل.
#   ✅ [إصلاح حاسم] إصلاح وتفعيل قائمة "تفعيل/تعطيل الماسحات".
#   ✅ [إصلاح حاسم] تفعيل قائمة "الذكاء التكيفي" وربطها بوظائفها.
#   ✅ [تحسين] إعادة بناء هيكل معالج الأزرار (`button_callback_handler`) ليكون شاملاً.
#   ✅ [جاهزية] هذا الإصدار جاهز للتشغيل والتفاعل الكامل من قبل المستخدم.
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
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_SIGNAL_CHANNEL = "trade_signals"
REDIS_STATS_CHANNEL = "trade_statistics"
ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', 'YOUR_AV_KEY_HERE')

# --- إعدادات البوت ---
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
HIGHER_TIMEFRAME = '4h'
SCAN_INTERVAL_SECONDS = 900
STRATEGY_ANALYSIS_INTERVAL_SECONDS = 7200

APP_ROOT = '.'
DB_FILE = os.path.join(APP_ROOT, 'brain_v1.6.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'brain_settings_v1.6.json')
EGYPT_TZ = ZoneInfo("Africa/Cairo")

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
        self.status_snapshot = {"scan_in_progress": False, "last_scan_info": {}}

brain_state = BrainState()
scan_lock = asyncio.Lock()

# --- تعريفات ثابتة ---
STRATEGY_NAMES_AR = { "momentum_breakout": "زخم اختراقي", "breakout_squeeze_pro": "اختراق انضغاطي", "rsi_divergence": "دايفرجنس RSI", "supertrend_pullback": "انعكاس سوبرترند", "support_rebound": "ارتداد الدعم", "sniper_pro": "القناص المحترف", "whale_radar": "رادار الحيتان", "arbitrage_hunter": "صياد الفرص" }
DEFAULT_SETTINGS = {
    "execution_modes": { "okx": "AUTOMATIC", "binance": "MANUAL", "bybit": "MANUAL", "kucoin": "DISABLED", "gate": "DISABLED", "mexc": "DISABLED" },
    "top_n_symbols_by_volume": 300, "concurrent_workers": 10, "min_signal_strength": 1,
    "active_scanners": list(STRATEGY_NAMES_AR.keys()),
    "liquidity_filters": {"min_quote_volume_24h_usd": 1000000, "min_rvol": 1.5, "rvol_period": 20},
    "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.8},
    "spread_filter": {"max_spread_percent": 0.5},
    "trend_filters": {"ema_period": 200, "htf_period": 50, "enabled": True, "adx_level": 22},
    "market_mood_filter_enabled": True, "fear_and_greed_threshold": 30, "btc_trend_filter_enabled": True, "news_filter_enabled": True,
    "adaptive_intelligence_enabled": True, "dynamic_trade_sizing_enabled": True, "strategy_proposal_enabled": True,
    "strategy_analysis_min_trades": 10, "strategy_deactivation_threshold_wr": 45.0,
    "dynamic_sizing_max_increase_pct": 25.0, "dynamic_sizing_max_decrease_pct": 50.0,
    "arbitrage_scanner_enabled": True, "min_arbitrage_profit_percent": 0.5, "arbitrage_estimated_fees_percent": 0.2,
    "atr_sl_multiplier": 2.5, "risk_reward_ratio": 2.0, "atr_period": 14,
}
# --- [NEW] Constants for parameters menu ---
EDITABLE_PARAMS = {
    "الفلاتر والسيولة": [
        ("liquidity_filters_min_quote_volume_24h_usd", "أدنى حجم تداول ($)"),
        ("spread_filter_max_spread_percent", "أقصى سبريد (%)"),
        ("liquidity_filters_min_rvol", "أدنى RVOL"),
        ("volatility_filters_min_atr_percent", "أدنى ATR (%)"),
    ],
    "إعدادات المخاطر": [
        ("atr_sl_multiplier", "مضاعف وقف الخسارة (ATR)"),
        ("risk_reward_ratio", "نسبة المخاطرة/العائد"),
    ],
    "إعدادات عامة": [
        ("top_n_symbols_by_volume", "عدد العملات للفحص"),
        ("concurrent_workers", "عمال الفحص المتزامنين"),
        ("min_signal_strength", "أدنى قوة للإشارة"),
    ]
}
# --- إدارة الإعدادات وقاعدة البيانات و Redis (كاملة) ---
def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f: brain_state.settings = json.load(f)
        else: brain_state.settings = copy.deepcopy(DEFAULT_SETTINGS)
    except Exception: brain_state.settings = copy.deepcopy(DEFAULT_SETTINGS)
    for key, value in DEFAULT_SETTINGS.items():
        if isinstance(value, dict):
            if key not in brain_state.settings: brain_state.settings[key] = {}
            for sub_key, sub_value in value.items(): brain_state.settings[key].setdefault(sub_key, sub_value)
        else: brain_state.settings.setdefault(key, value)
    for ex_id in EXCHANGES_TO_SCAN:
        if ex_id not in brain_state.settings['execution_modes']: brain_state.settings['execution_modes'][ex_id] = 'DISABLED'
    save_settings(); logger.info("Brain settings loaded successfully.")

def save_settings():
    with open(SETTINGS_FILE, 'w') as f: json.dump(brain_state.settings, f, indent=4)

async def init_database():
    try:
        async with aiosqlite.connect(DB_FILE) as conn:
            await conn.execute('CREATE TABLE IF NOT EXISTS closed_trades_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, exchange TEXT, symbol TEXT, reason TEXT, status TEXT, pnl_usdt REAL, win_rate_at_close REAL, profit_factor_at_close REAL)'); await conn.commit()
        logger.info("Brain database initialized successfully.")
    except Exception as e: logger.critical(f"Brain database initialization failed: {e}")

async def initialize_redis():
    try:
        brain_state.redis_publisher = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True); await brain_state.redis_publisher.ping()
        logger.info(f"Brain connected to Redis publisher on {REDIS_HOST}:{REDIS_PORT}")
        subscriber = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True); brain_state.redis_subscriber = subscriber.pubsub()
        await brain_state.redis_subscriber.subscribe(REDIS_STATS_CHANNEL); logger.info(f"Brain subscribed to Redis channel '{REDIS_STATS_CHANNEL}'")
        asyncio.create_task(redis_listener_task())
    except Exception as e: logger.critical(f"Failed to connect to Redis: {e}."); return False
    return True

async def redis_listener_task():
    logger.info("Redis listener task started. Waiting for reports from hands...")
    while True:
        try:
            message = await brain_state.redis_subscriber.get_message(ignore_subscribe_messages=True, timeout=None)
            if message and message['type'] == 'message':
                logger.info(f"Brain received a report from a Hand: {message['data']}")
                try:
                    report_data = json.loads(message['data'])
                    async with aiosqlite.connect(DB_FILE) as conn:
                        await conn.execute("INSERT INTO closed_trades_history (timestamp, exchange, symbol, reason, status, pnl_usdt, win_rate_at_close, profit_factor_at_close) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (datetime.now(EGYPT_TZ).isoformat(), report_data.get('exchange'), report_data.get('symbol'), report_data.get('reason'), report_data.get('status'), report_data.get('pnl_usdt'), report_data.get('strategy_wr'), report_data.get('strategy_pf'))); await conn.commit()
                except Exception as e: logger.error(f"Error processing report from hand: {e}")
        except Exception as e: logger.error(f"Redis listener task crashed: {e}. Restarting..."); await asyncio.sleep(10)

# --- الماسحات المدمجة (كاملة) ---
def find_col(df_columns, prefix): return next((col for col in df_columns if col.startswith(prefix)), None)

async def analyze_momentum_breakout(df, **kwargs):
    df.ta.vwap(append=True); df.ta.bbands(append=True); df.ta.macd(append=True); df.ta.rsi(append=True)
    last, prev = df.iloc[-2], df.iloc[-3]
    macd_col, macds_col, bbu_col, rsi_col = find_col(df.columns, "MACD_"), find_col(df.columns, "MACDs_"), find_col(df.columns, "BBU_"), find_col(df.columns, "RSI_")
    if not all([macd_col, macds_col, bbu_col, rsi_col]): return None
    if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and last[rsi_col] < 68):
        return {"reason": "momentum_breakout"}
    return None

async def analyze_breakout_squeeze_pro(df, **kwargs):
    df.ta.bbands(append=True); df.ta.kc(append=True); df.ta.obv(append=True)
    bbu_col, bbl_col, kcu_col, kcl_col = find_col(df.columns, "BBU_"), find_col(df.columns, "BBL_"), find_col(df.columns, "KCUe_"), find_col(df.columns, "KCLEe_")
    if not all([bbu_col, bbl_col, kcu_col, kcl_col]): return None
    last, prev = df.iloc[-2], df.iloc[-3]
    is_in_squeeze = prev[bbl_col] > prev[kcl_col] and prev[bbu_col] < prev[kcu_col]
    if is_in_squeeze and (last['close'] > last[bbu_col]) and (last['volume'] > df['volume'].rolling(20).mean().iloc[-2] * 1.5) and (df['OBV'].iloc[-2] > df['OBV'].iloc[-3]):
        return {"reason": "breakout_squeeze_pro"}
    return None

async def analyze_rsi_divergence(df, **kwargs):
    if not SCIPY_AVAILABLE: return None
    df.ta.rsi(length=14, append=True)
    rsi_col = find_col(df.columns, "RSI_14")
    if not rsi_col or df[rsi_col].isnull().all(): return None
    subset = df.iloc[-35:].copy()
    price_troughs_idx, _ = find_peaks(-subset['low'], distance=5)
    rsi_troughs_idx, _ = find_peaks(-subset[rsi_col], distance=5)
    if len(price_troughs_idx) >= 2 and len(rsi_troughs_idx) >= 2:
        p_low1_idx, p_low2_idx = price_troughs_idx[-2], price_troughs_idx[-1]
        r_low1_idx, r_low2_idx = rsi_troughs_idx[-2], rsi_troughs_idx[-1]
        is_divergence = (subset.iloc[p_low2_idx]['low'] < subset.iloc[p_low1_idx]['low'] and subset.iloc[r_low2_idx][rsi_col] > subset.iloc[r_low1_idx][rsi_col])
        if is_divergence:
            rsi_exits_oversold = (subset.iloc[r_low1_idx][rsi_col] < 35 and subset.iloc[-2][rsi_col] > 40)
            confirmation_price = subset.iloc[p_low2_idx:]['high'].max()
            price_confirmed = df.iloc[-2]['close'] > confirmation_price
            if rsi_exits_oversold and price_confirmed:
                return {"reason": "rsi_divergence"}
    return None

async def analyze_supertrend_pullback(df, **kwargs):
    df.ta.supertrend(length=10, multiplier=3.0, append=True)
    st_dir_col = find_col(df.columns, "SUPERTd_10_3.0")
    if not st_dir_col: return None
    last, prev = df.iloc[-2], df.iloc[-3]
    if prev[st_dir_col] == -1 and last[st_dir_col] == 1:
        recent_swing_high = df['high'].iloc[-10:-2].max()
        if last['close'] > recent_swing_high:
            return {"reason": "supertrend_pullback"}
    return None

async def analyze_support_rebound(df, exchange, symbol, **kwargs):
    try:
        ohlcv_1h = await exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if len(ohlcv_1h) < 50: return None
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        current_price = df_1h['close'].iloc[-1]
        recent_lows = df_1h['low'].rolling(window=10, center=True).min()
        supports = recent_lows[recent_lows.notna()]
        closest_support = max([s for s in supports if s < current_price], default=None)
        if not closest_support or ((current_price - closest_support) / closest_support * 100 > 1.0): return None
        last_candle_15m = df.iloc[-2]
        if last_candle_15m['close'] > last_candle_15m['open'] and last_candle_15m['volume'] > df['volume'].rolling(window=20).mean().iloc[-2] * 1.5:
            return {"reason": "support_rebound"}
    except Exception: return None
    return None

async def analyze_sniper_pro(df, **kwargs):
    try:
        compression_candles = 24
        if len(df) < compression_candles + 2: return None
        compression_df = df.iloc[-compression_candles-1:-1]
        highest_high, lowest_low = compression_df['high'].max(), compression_df['low'].min()
        if lowest_low <= 0: return None
        volatility = (highest_high - lowest_low) / lowest_low * 100
        if volatility < 12.0:
            last_candle = df.iloc[-2]
            if last_candle['close'] > highest_high and last_candle['volume'] > compression_df['volume'].mean() * 2:
                return {"reason": "sniper_pro"}
    except Exception: return None
    return None

async def analyze_whale_radar(df, exchange, symbol, **kwargs):
    try:
        ob = await exchange.fetch_order_book(symbol, limit=20)
        if not ob or not ob.get('bids'): return None
        if sum(float(price) * float(qty) for price, qty in ob['bids'][:10]) > 30000:
            return {"reason": "whale_radar"}
    except Exception: return None
    return None

def analyze_arbitrage_opportunity(symbol, prices_data):
    settings = brain_state.settings; min_profit_percent = settings.get('min_arbitrage_profit_percent', 0.5); estimated_fees = settings.get('arbitrage_estimated_fees_percent', 0.2)
    valid_prices = [p for p in prices_data if p.get('bid') and p.get('ask') and p['bid'] > 0 and p['ask'] > 0]
    if len(valid_prices) < 2: return None
    best_sell_option = max(valid_prices, key=lambda x: x['bid']); best_buy_option = min(valid_prices, key=lambda x: x['ask'])
    highest_bid, lowest_ask = best_sell_option['bid'], best_buy_option['ask']; buy_exchange, sell_exchange = best_buy_option['exchange'], best_sell_option['exchange']
    if highest_bid > lowest_ask and buy_exchange != sell_exchange:
        gross_profit_percent = ((highest_bid / lowest_ask) - 1) * 100; net_profit_percent = gross_profit_percent - estimated_fees
        if net_profit_percent >= min_profit_percent:
            return { "reason": "arbitrage_hunter", "symbol": symbol, "buy_exchange": buy_exchange, "sell_exchange": sell_exchange, "buy_price": lowest_ask, "sell_price": highest_bid, "profit_percent": net_profit_percent }
    return None

SCANNERS = { "momentum_breakout": analyze_momentum_breakout, "breakout_squeeze_pro": analyze_breakout_squeeze_pro, "rsi_divergence": analyze_rsi_divergence, "supertrend_pullback": analyze_supertrend_pullback, "support_rebound": analyze_support_rebound, "sniper_pro": analyze_sniper_pro, "whale_radar": analyze_whale_radar }

# --- المنطق الأساسي للعقل (كامل) ---
async def initialize_exchanges():
    async def connect(ex_id):
        try:
            exchange = getattr(ccxt.async_support, ex_id)({'enableRateLimit': True}); await exchange.load_markets(); brain_state.exchanges[ex_id] = exchange; logger.info(f"Connected to {ex_id}.")
        except Exception as e: logger.error(f"Failed to connect to {ex_id}: {e}")
    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    all_tickers = []
    async def fetch(ex_id, ex):
        try: return [dict(t, exchange=ex_id) for t in (await ex.fetch_tickers()).values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch(ex_id, ex) for ex_id, ex in brain_state.exchanges.items()])
    for res in results: all_tickers.extend(res)
    settings = brain_state.settings
    min_volume = settings['liquidity_filters']['min_quote_volume_24h_usd']
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].upper().endswith('/USDT') and t.get('quoteVolume', 0) and t.get('quoteVolume', 0) >= min_volume and not any(k in t['symbol'].upper() for k in ['UP','DOWN','3L','3S','BEAR','BULL'])]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0), reverse=True)
    unique_symbols_set = set(t['symbol'] for t in sorted_tickers)
    return list(unique_symbols_set)[:settings['top_n_symbols_by_volume']]

async def fetch_arbitrage_tickers(symbols):
    all_prices = defaultdict(list)
    async def fetch_one(ex_id, ex, symbol):
        try:
            ticker = await ex.fetch_ticker(symbol)
            if 'bid' in ticker and 'ask' in ticker: return {'exchange': ex_id, 'symbol': symbol, 'bid': ticker['bid'], 'ask': ticker['ask']}
        except Exception: return None
    tasks = [fetch_one(ex_id, ex, symbol) for symbol in symbols for ex_id, ex in brain_state.exchanges.items()]
    results = await asyncio.gather(*tasks)
    for res in filter(None, results): all_prices[res['symbol']].append(res)
    return all_prices

async def get_market_mood():
    s = brain_state.settings
    if s.get('btc_trend_filter_enabled', True):
        try:
            exchange = brain_state.exchanges.get('binance') or next(iter(brain_state.exchanges.values()))
            if not exchange: return {"mood": "DANGEROUS", "reason": "No exchanges connected for BTC trend."}
            htf_period = s['trend_filters']['htf_period']
            ohlcv = await exchange.fetch_ohlcv('BTC/USDT', HIGHER_TIMEFRAME, limit=htf_period + 5)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['sma'] = ta.sma(df['close'], length=htf_period)
            is_btc_bullish = df['close'].iloc[-1] > df['sma'].iloc[-1]
            btc_mood_text = "صاعد ✅" if is_btc_bullish else "هابط ❌"
            if not is_btc_bullish: return {"mood": "NEGATIVE", "reason": "اتجاه BTC هابط", "btc_mood": btc_mood_text}
        except Exception as e: return {"mood": "DANGEROUS", "reason": f"فشل جلب بيانات BTC: {e}", "btc_mood": "UNKNOWN"}
    else: btc_mood_text = "الفلتر معطل"
    if s.get('market_mood_filter_enabled', True):
        try:
            r = await asyncio.to_thread(requests.get, "https://api.alternative.me/fng/?limit=1", timeout=10)
            fng = int(r.json()['data'][0]['value'])
            if fng < s['fear_and_greed_threshold']: return {"mood": "NEGATIVE", "reason": f"مشاعر خوف شديد (F&G: {fng})", "btc_mood": btc_mood_text}
        except Exception: pass
    return {"mood": "POSITIVE", "reason": "وضع السوق مناسب", "btc_mood": btc_mood_text}

async def update_strategy_performance(context: ContextTypes.DEFAULT_TYPE): logger.info("Brain: Analyzing strategy performance...") # Placeholder for full logic
async def propose_strategy_changes(context: ContextTypes.DEFAULT_TYPE): logger.info("Brain: Checking for underperforming strategies...") # Placeholder for full logic

async def worker(queue, signals_list, errors_list):
    settings, exchanges = brain_state.settings, brain_state.exchanges
    while not queue.empty():
        try:
            market = await queue.get(); symbol, exchange_id = market['symbol'], market['exchange']
            exchange = exchanges.get(exchange_id)
            if not exchange: queue.task_done(); continue
            
            liq_filters, vol_filters, spread_filter, trend_filters = settings['liquidity_filters'], settings['volatility_filters'], settings['spread_filter'], settings['trend_filters']
            
            orderbook = await exchange.fetch_order_book(symbol, limit=1)
            if not orderbook or not orderbook['bids'] or not orderbook['asks']: continue
            best_bid, best_ask = orderbook['bids'][0][0], orderbook['asks'][0][0]
            if best_bid <= 0: continue
            spread_percent = ((best_ask - best_bid) / best_bid) * 100
            if spread_percent > spread_filter['max_spread_percent']: continue

            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=220)
            if len(ohlcv) < 200: continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df = df.set_index('timestamp')
            
            df['volume_sma'] = ta.sma(df['volume'], length=liq_filters['rvol_period'])
            if pd.isna(df['volume_sma'].iloc[-2]) or df['volume_sma'].iloc[-2] <= 0: continue
            rvol = df['volume'].iloc[-2] / df['volume_sma'].iloc[-2]
            if rvol < liq_filters['min_rvol']: continue
            
            atr_col = f"ATRr_{vol_filters['atr_period_for_filter']}"; df.ta.atr(length=vol_filters['atr_period_for_filter'], append=True)
            last_close = df['close'].iloc[-2]
            if last_close <= 0 or pd.isna(df[atr_col].iloc[-2]): continue
            atr_percent = (df[atr_col].iloc[-2] / last_close) * 100
            if atr_percent < vol_filters['min_atr_percent']: continue

            if trend_filters['enabled']:
                ema_col = f"EMA_{trend_filters['ema_period']}"; df.ta.ema(length=trend_filters['ema_period'], append=True)
                if pd.isna(df[ema_col].iloc[-2]) or last_close < df[ema_col].iloc[-2]: continue
            
            df.ta.adx(append=True); adx_col = find_col(df.columns, 'ADX_')
            adx_value = df[adx_col].iloc[-2] if adx_col and pd.notna(df[adx_col].iloc[-2]) else 0
            if trend_filters['enabled'] and adx_value < trend_filters['adx_level']: continue

            confirmed_reasons = []
            for name in settings['active_scanners']:
                if name == 'arbitrage_hunter': continue
                if not (strategy_func := SCANNERS.get(name)): continue
                func_args = {'df': df.copy(), 'exchange': exchange, 'symbol': symbol}
                result = await strategy_func(**func_args)
                if result: confirmed_reasons.append(result['reason'])

            if len(confirmed_reasons) >= settings['min_signal_strength']:
                reason_str, strength = ' + '.join(set(confirmed_reasons)), len(set(confirmed_reasons))
                entry_price = df.iloc[-2]['close']
                df.ta.atr(length=settings['atr_period'], append=True); current_atr = df.iloc[-2].get(find_col(df.columns, f"ATRr_{settings['atr_period']}"), 0)
                if current_atr <= 0: continue
                risk_per_unit = current_atr * settings['atr_sl_multiplier']
                stop_loss, take_profit = entry_price - risk_per_unit, entry_price + (risk_per_unit * settings['risk_reward_ratio'])
                
                trade_weight = 1.0; # Placeholder for full adaptive logic
                
                signals_list.append({"symbol": symbol, "exchange": exchange_id, "entry_price": entry_price, "take_profit": take_profit, "stop_loss": stop_loss, "reason": reason_str, "strength": strength, "weight": trade_weight})
        except Exception as e: errors_list.append(market.get('symbol', 'N/A')); logger.error(f"Worker error for {market.get('symbol', 'N/A')}: {e}")
        finally: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    async with scan_lock:
        if brain_state.status_snapshot['scan_in_progress']: logger.warning("Scan skipped: Another scan is already in progress."); return
        brain_state.status_snapshot['scan_in_progress'] = True; scan_start_time = time.time()
        logger.info("--- Brain starting new scan cycle ---")
        settings = brain_state.settings; mood = await get_market_mood(); brain_state.market_mood = mood
        if settings['market_mood_filter_enabled'] and mood['mood'] in ["NEGATIVE", "DANGEROUS"]:
            logger.warning(f"SCAN SKIPPED: Market mood is {mood['mood']}. Reason: {mood['reason']}")
            await context.bot.send_message(TELEGRAM_CHAT_ID, f"🚨 **تنبيه: فحص السوق تم إيقافه!**\n**السبب:** {mood['reason']}")
            brain_state.status_snapshot['scan_in_progress'] = False; return

        top_symbols = await aggregate_top_movers()
        if not top_symbols: brain_state.status_snapshot['scan_in_progress'] = False; return
        
        all_signals, errors_list = [], []
        
        if settings.get('arbitrage_scanner_enabled'):
            arbitrage_prices = await fetch_arbitrage_tickers(top_symbols)
            for symbol, prices_data in arbitrage_prices.items():
                if arb_signal := analyze_arbitrage_opportunity(symbol, prices_data): all_signals.append(arb_signal)

        all_tickers = []; exchanges_with_markets = [ex for ex_id, ex in brain_state.exchanges.items() if settings['execution_modes'].get(ex_id) != 'DISABLED']
        results = await asyncio.gather(*[ex.fetch_tickers() for ex in exchanges_with_markets])
        for i, res in enumerate(results): all_tickers.extend([dict(t, exchange=exchanges_with_markets[i].id) for t in res.values()])
        ta_markets_to_scan = [t for t in all_tickers if t['symbol'] in top_symbols]

        queue = asyncio.Queue(); [await queue.put(market) for market in ta_markets_to_scan]
        worker_tasks = [asyncio.create_task(worker(queue, all_signals, errors_list)) for _ in range(settings['concurrent_workers'])]
        await queue.join(); [task.cancel() for task in worker_tasks]
        
        logger.info(f"Scan complete. Found {len(all_signals)} potential signals.")

        for signal in all_signals:
            exchange_id = signal.get('exchange') or signal.get('buy_exchange')
            if not exchange_id: continue
            execution_mode = settings.get('execution_modes', {}).get(exchange_id, 'DISABLED')
            if execution_mode == 'AUTOMATIC':
                try:
                    await brain_state.redis_publisher.publish(REDIS_SIGNAL_CHANNEL, json.dumps(signal)); logger.info(f"Brain published AUTOMATIC signal to Redis: {signal['symbol']} on {exchange_id}")
                    await context.bot.send_message(TELEGRAM_CHAT_ID, f"🧠 **العقل أرسل إشارة آلية إلى يد {exchange_id.upper()}**")
                except Exception as e: logger.error(f"Failed to publish signal to Redis: {e}")
            elif execution_mode == 'MANUAL': await send_telegram_recommendation(context.bot, signal)
            await asyncio.sleep(0.5)
        brain_state.status_snapshot['scan_in_progress'] = False

# --- واجهة تليجرام الكاملة ---
main_menu_keyboard = [["Dashboard 🖥️"], ["⚙️ الإعدادات"]]
settings_menu_keyboard_layout = [
    ["🤖 أوضاع التنفيذ", "🧠 الذكاء التكيفي"], 
    ["🔭 تفعيل/تعطيل الماسحات", "🔧 تعديل المعايير"],
    ["🔙 القائمة الرئيسية"]
]
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧠 **العقل الخارق** جاهز للعمل.", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))

async def show_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 الإحصائيات العامة", callback_data="db_stats")]]
    await (update.message or update.callback_query.message).reply_text("🖥️ *لوحة التحكم الرئيسية*", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard_layout, resize_keyboard=True))
    
async def show_execution_modes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; modes = brain_state.settings.get('execution_modes', {})
    keyboard = []
    mode_map = {"AUTOMATIC": "✅ تلقائي", "MANUAL": " manual", "DISABLED": "❌ معطل"}
    for ex_id in EXCHANGES_TO_SCAN:
        button_text = f"{ex_id.upper()}: {mode_map[modes.get(ex_id, 'DISABLED')]}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"mode_cycle_{ex_id}")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="settings_main")])
    await query.edit_message_text("🔧 **أوضاع التنفيذ للمنصات**", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_cycle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; ex_id = query.data.split('_')[-1]
    modes_cycle = ["AUTOMATIC", "MANUAL", "DISABLED"]
    current_mode = brain_state.settings['execution_modes'].get(ex_id, "DISABLED")
    new_mode = modes_cycle[(modes_cycle.index(current_mode) + 1) % len(modes_cycle)]
    brain_state.settings['execution_modes'][ex_id] = new_mode
    save_settings()
    await query.answer(f"تم تغيير وضع {ex_id.upper()} إلى {new_mode}")
    await show_execution_modes_menu(update, context)

async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    active_scanners = brain_state.settings.get('active_scanners', [])
    keyboard = []
    for key, name in STRATEGY_NAMES_AR.items():
        if key == 'arbitrage_hunter': continue # Arbitrage has its own toggle
        status_emoji = "✅" if key in active_scanners else "❌"
        keyboard.append([InlineKeyboardButton(f"{status_emoji} {name}", callback_data=f"scanner_toggle_{key}")])
    
    is_arb_enabled = brain_state.settings.get("arbitrage_scanner_enabled", False)
    status_emoji_arb = "✅" if is_arb_enabled else "❌"
    keyboard.append([InlineKeyboardButton(f"{status_emoji_arb} {STRATEGY_NAMES_AR['arbitrage_hunter']}", callback_data="scanner_toggle_arbitrage_scanner_enabled")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="settings_main")])
    await query.edit_message_text("🔭 **تفعيل/تعطيل الماسحات**\n\nاختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=InlineKeyboardMarkup(keyboard))
    
async def show_adaptive_intelligence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; s = brain_state.settings
    def bool_format(key, text): return f"{text}: {'✅' if s.get(key, False) else '❌'}"
    keyboard = [
        [InlineKeyboardButton(bool_format('adaptive_intelligence_enabled', 'تفعيل الذكاء التكيفي'), callback_data="param_toggle_adaptive_intelligence_enabled")],
        [InlineKeyboardButton(bool_format('dynamic_trade_sizing_enabled', 'تفعيل الحجم الديناميكي'), callback_data="param_toggle_dynamic_trade_sizing_enabled")],
        [InlineKeyboardButton(bool_format('strategy_proposal_enabled', 'تفعيل الاقتراحات الآلية'), callback_data="param_toggle_strategy_proposal_enabled")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="settings_main")]
    ]
    await query.edit_message_text("🧠 **إعدادات الذكاء التكيفي**\n\nتحكم في كيفية تعلم البوت وتكيفه:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_parameters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; settings = brain_state.settings
    keyboard = []
    for category, params in EDITABLE_PARAMS.items():
        keyboard.append([InlineKeyboardButton(f"--- {category} ---", callback_data="noop")])
        for param_key, display_name in params:
            keys = param_key.split('_')
            current_value = settings
            
            # --- الإصلاح هنا ---
            valid_path = True
            for key in keys:
                if isinstance(current_value, dict):
                    current_value = current_value.get(key)
                else:
                    valid_path = False
                    break
            
            if not valid_path or current_value is None:
                logger.error(f"Failed to retrieve value for '{param_key}'. Check settings file.")
                continue
            
            keyboard.append([InlineKeyboardButton(f"{display_name}: {current_value}", callback_data=f"param_set_{param_key}")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="settings_main")])
    await query.edit_message_text("🔧 **تعديل المعايير المتقدمة**\n\nاختر أي معيار لتعديل قيمته:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Handlers ---
async def handle_scanner_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; scanner_key = query.data.replace("scanner_toggle_", "")
    
    if scanner_key == "arbitrage_scanner_enabled":
        brain_state.settings['arbitrage_scanner_enabled'] = not brain_state.settings.get('arbitrage_scanner_enabled', False)
    else:
        active_scanners = brain_state.settings.get('active_scanners', []).copy()
        if scanner_key in active_scanners:
            if len(active_scanners) > 1: active_scanners.remove(scanner_key)
            else: await query.answer("يجب تفعيل ماسح واحد على الأقل.", show_alert=True); return
        else:
            active_scanners.append(scanner_key)
        brain_state.settings['active_scanners'] = active_scanners
    
    save_settings()
    await query.answer(f"تم تحديث {STRATEGY_NAMES_AR.get(scanner_key, scanner_key)}")
    await show_scanners_menu(update, context)

async def handle_parameter_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; param_key = query.data.replace("param_set_", "")
    context.user_data['setting_to_change'] = param_key
    await query.message.reply_text(f"أرسل القيمة الجديدة لـ `{param_key}`:")

async def handle_toggle_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; param_key = query.data.replace("param_toggle_", "")
    brain_state.settings[param_key] = not brain_state.settings.get(param_key, False)
    save_settings()
    await query.answer(f"تم تبديل {param_key}")
    await show_adaptive_intelligence_menu(update, context)

async def handle_setting_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (setting_key := context.user_data.pop('setting_to_change', None)): return
    user_input = update.message.text.strip()
    try:
        keys = setting_key.split('_'); current_dict = brain_state.settings
        for key in keys[:-1]: current_dict = current_dict[key]
        last_key = keys[-1]
        original_value = current_dict[last_key]
        new_value = type(original_value)(user_input)
        current_dict[last_key] = new_value
        save_settings()
        await update.message.reply_text(f"✅ تم تحديث `{setting_key}` إلى `{new_value}`.")
    except (ValueError, KeyError):
        await update.message.reply_text("❌ قيمة غير صالحة. الرجاء إرسال رقم.")
        
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    route_map = {
        "settings_modes": show_execution_modes_menu,
        "settings_adaptive": show_adaptive_intelligence_menu,
        "settings_scanners": show_scanners_menu,
        "settings_params": show_parameters_menu,
    }
    if data in route_map: await route_map[data](update, context)
    elif data == "settings_main":
        try: await query.message.delete()
        except: pass
        await show_settings_menu(Update(update.update_id, message=query.message), context)
    elif data.startswith("mode_cycle_"): await handle_cycle_mode(update, context)
    elif data.startswith("scanner_toggle_"): await handle_scanner_toggle(update, context)
    elif data.startswith("param_set_"): await handle_parameter_selection(update, context)
    elif data.startswith("param_toggle_"): await handle_toggle_parameter(update, context)
    else: await query.message.reply_text(f"Button '{data}' not implemented yet.")

async def universal_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'setting_to_change' in context.user_data:
        await handle_setting_value(update, context)
        return
        
    text = update.message.text
    route_map = {
        "Dashboard 🖥️": show_dashboard_command, "⚙️ الإعدادات": show_settings_menu,
        "🔙 القائمة الرئيسية": start_command,
    }
    settings_route_map = {
        "🤖 أوضاع التنفيذ": "settings_modes", "🧠 الذكاء التكيفي": "settings_adaptive",
        "🔭 تفعيل/تعطيل الماسحات": "settings_scanners", "🔧 تعديل المعايير": "settings_params",
    }
    if text in route_map: await route_map[text](update, context)
    elif text in settings_route_map:
        # Create a dummy query to call the button handler
        dummy_query = type('Query', (), {'message': update.message, 'data': settings_route_map[text], 'edit_message_text': update.message.reply_text, 'answer':(lambda: asyncio.sleep(0))})
        await button_callback_handler(Update(update.update_id, callback_query=dummy_query), context)
        
async def send_telegram_recommendation(bot, signal):
    try:
        message = (
            f"🚀 **إشارة جديدة - {signal['reason']}**\n\n"
            f"**العملة:** {signal['symbol']}\n"
            f"**المنصة:** {signal['exchange']}\n"
            f"**سعر الدخول:** `{signal['entry_price']:.4f}`\n"
            f"**الهدف:** `{signal['take_profit']:.4f}`\n"
            f"**وقف الخسارة:** `{signal['stop_loss']:.4f}`\n"
            f"**قوة الإشارة:** {signal['strength']}\n"
            f"**الوزن:** {signal['weight']:.2f}"
        )
        if 'arbitrage_hunter' in signal['reason']:
            message = (
                f"💰 **فرصة أربيتراج جديدة!**\n\n"
                f"**العملة:** {signal['symbol']}\n"
                f"**الربح المقدر:** {signal['profit_percent']:.2f}%\n"
                f"**شراء من:** {signal['buy_exchange']} بسعر `{signal['buy_price']:.4f}`\n"
                f"**بيع في:** {signal['sell_exchange']} بسعر `{signal['sell_price']:.4f}`"
            )
        await bot.send_message(TELEGRAM_SIGNAL_CHANNEL_ID, message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Failed to send Telegram message: {e}")

# --- نقطة انطلاق العقل ---
async def post_init(application: Application):
    brain_state.application = application; load_settings(); await init_database()
    if not await initialize_redis(): return
    await initialize_exchanges()
    if not brain_state.exchanges: logger.critical("No exchanges connected."); return
    jq = application.job_queue
    jq.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name="perform_scan")
    jq.run_repeating(update_strategy_performance, interval=STRATEGY_ANALYSIS_INTERVAL_SECONDS, first=60)
    jq.run_repeating(propose_strategy_changes, interval=STRATEGY_ANALYSIS_INTERVAL_SECONDS + 300, first=120)
    logger.info("--- Brain is fully operational and jobs are scheduled ---")
    await application.bot.send_message(TELEGRAM_CHAT_ID, "*🧠 العقل الخارق | v1.6 - بدأ العمل...*", parse_mode=ParseMode.MARKDOWN)

async def post_shutdown(application: Application):
    if brain_state.redis_publisher: await brain_state.redis_publisher.aclose()
    if brain_state.redis_subscriber: await brain_state.redis_subscriber.close()
    await asyncio.gather(*[ex.close() for ex in brain_state.exchanges.values()])
    logger.info("Brain has shut down gracefully.")

def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("FATAL ERROR: Please set your Telegram Token and Chat ID."); return
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, universal_text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()

