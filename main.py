# -*- coding: utf-8 -*-
import os
import asyncio
import logging
import time
from datetime import datetime

import ccxt.async_support as ccxt_async
import pandas as pd
import pandas_ta as ta

from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---------- إعدادات عامة ----------
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# المنصات المدعومة
EXCHANGE_IDS = ["binance", "okx", "bybit", "kucoin", "gate"]

# بيانات تليجرام (ضع القيم هنا مباشرة أو من متغيرات البيئة)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ضع_التوكن_هنا")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ضع_الشات_آي_دي_هنا")

# Strategy / runtime params
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 900  # 15 دقيقة
EXCLUDED_SYMBOLS = ['BTC/USDT', 'ETH/USDT']
STABLECOINS = ['USDC', 'DAI', 'BUSD', 'TUSD', 'USDP', 'USDT']
TOP_N_SYMBOLS_BY_VOLUME = 50   # عدد العملات اللي هنفحصها

VWAP_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STDDEV = 2.0
RSI_PERIOD = 14
RSI_MAX_LEVEL = 68

TAKE_PROFIT_PERCENTAGE = 4.0
STOP_LOSS_PERCENTAGE = 2.0

PERFORMANCE_FILE = 'recommendations_log.csv'

# state
clients = {}
bot_data = {"last_signal_time": {}, "clients": clients}

# ------------------- تهيئة عملاء المنصات (بدون مفاتيح) -------------------
async def create_exchange_clients():
    created = {}
    for ex_id in EXCHANGE_IDS:
        try:
            client = getattr(ccxt_async, ex_id)({'enableRateLimit': True})
            await client.load_markets()
            created[ex_id] = client
            logging.info(f"Connected (public) to {ex_id}")
        except Exception as e:
            logging.error(f"Failed to init {ex_id}: {e}")
    return created

# ------------------- جلب أفضل العملات -------------------
async def get_top_movers_aggregated():
    volumes = {}
    clients_local = bot_data["clients"]
    for ex_id, client in clients_local.items():
        try:
            tickers = await client.fetch_tickers()
            for symbol, tk in tickers.items():
                if not symbol.endswith('/USDT'):
                    continue
                if any(s in symbol for s in STABLECOINS):
                    continue
                if symbol in EXCLUDED_SYMBOLS:
                    continue
                qv = tk.get('quoteVolume') or tk.get('baseVolume') or 0
                key = f"{ex_id}:{symbol}"
                try:
                    volumes[key] = float(qv)
                except Exception:
                    volumes[key] = 0.0
        except Exception as e:
            logging.warning(f"[{ex_id}] fetch_tickers failed: {e}")
        await asyncio.sleep(0.2)

    sorted_keys = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
    top = [k for k, v in sorted_keys[:TOP_N_SYMBOLS_BY_VOLUME]]
    logging.info(f"Aggregated top {len(top)} markets across exchanges.")
    return top

# ------------------- جلب بيانات الشموع -------------------
async def fetch_ohlcv_for_market(ex_id_symbol, limit=150):
    try:
        ex_id, symbol = ex_id_symbol.split(":", 1)
        client = bot_data["clients"].get(ex_id)
        if not client:
            return None
        ohlcv = await client.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        if not ohlcv or len(ohlcv) < BBANDS_PERIOD:
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        logging.warning(f"[{ex_id_symbol}] fetch_ohlcv failed: {e}")
        return None

# ------------------- تحليل البيانات -------------------
def analyze_market_data(df, ex_id_symbol):
    if df is None or len(df) < BBANDS_PERIOD + 3:
        return None
    try:
        df.ta.vwap(length=VWAP_PERIOD, append=True)
        df.ta.bbands(length=BBANDS_PERIOD, std=BBANDS_STDDEV, append=True)
        df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)

        bbu_col = f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}'
        vwap_col = f'VWAP_{VWAP_PERIOD}'
        macd_col = f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
        macds_col = f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
        rsi_col = f'RSI_{RSI_PERIOD}'

        required_columns = [bbu_col, vwap_col, macd_col, macds_col, rsi_col]
        if not all(c in df.columns for c in required_columns):
            return None

        last = df.iloc[-2]
        prev = df.iloc[-3]

        macd_crossover = (prev[macd_col] <= prev[macds_col]) and (last[macd_col] > last[macds_col])
        bollinger_breakout = last['close'] > last[bbu_col]
        vwap_confirm = last['close'] > last[vwap_col]
        rsi_ok = last[rsi_col] < RSI_MAX_LEVEL

        if macd_crossover and bollinger_breakout and vwap_confirm and rsi_ok:
            entry_price = float(last['close'])
            tp = entry_price * (1 + TAKE_PROFIT_PERCENTAGE / 100.0)
            sl = entry_price * (1 - STOP_LOSS_PERCENTAGE / 100.0)
            return {
                "market": ex_id_symbol,
                "entry_price": entry_price,
                "take_profit": tp,
                "stop_loss": sl,
                "timestamp": df.index[-2],
                "reason": "MACD crossover + Bollinger breakout + VWAP confirm"
            }
    except Exception as e:
        logging.error(f"[{ex_id_symbol}] analysis error: {e}")
    return None

# ------------------- إرسال رسالة تليجرام -------------------
async def send_telegram_message(bot: Bot, signal):
    try:
        market = signal['market']
        msg = (
            "✅ *توصية تداول جديدة*\n\n"
            f"*السوق:* `{market}`\n"
            f"*الإستراتيجية:* `{signal['reason']}`\n"
            f"*الإجراء:* `شراء (BUY)`\n"
            f"*سعر الدخول:* `${signal['entry_price']:,.6f}`\n"
            f"🎯 *جني الأرباح ({TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.6f}`\n"
            f"🛑 *وقف الخسارة ({STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.6f}`\n\n"
            "_تنبيه: التداول عالي المخاطر._"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"Sent telegram for {market}")
        log_recommendation(signal)
    except Exception as e:
        logging.error(f"Failed to send telegram message: {e}")

# ------------------- تسجيل التوصية -------------------
def log_recommendation(signal):
    df = pd.DataFrame([{
        'timestamp': signal['timestamp'],
        'market': signal['market'],
        'entry_price': signal['entry_price'],
        'take_profit': signal['take_profit'],
        'stop_loss': signal['stop_loss'],
        'status': 'نشطة',
        'exit_price': None,
        'closed_at': None
    }])
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    df.to_csv(PERFORMANCE_FILE, mode='a', header=not file_exists, index=False, encoding='utf-8-sig')

# ------------------- عملية الفحص -------------------
async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    symbols_to_scan = await get_top_movers_aggregated()
    if not symbols_to_scan:
        logging.info("No markets to scan this round.")
        return

    found_signals = 0
    for market in symbols_to_scan:
        try:
            df = await fetch_ohlcv_for_market(market)
            if df is None:
                continue
            signal = analyze_market_data(df, market)
            if signal:
                now = time.time()
                if market not in bot_data["last_signal_time"] or (now - bot_data["last_signal_time"].get(market, 0)) > (LOOP_INTERVAL_SECONDS * 4):
                    await send_telegram_message(context.bot, signal)
                    bot_data["last_signal_time"][market] = now
                    found_signals += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"[{market}] scan loop error: {e}")

    logging.info(f"Scan complete. found_signals={found_signals}")

# ------------------- أوامر بوت التليجرام -------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً! بوت التداول المتعدد المنصات جاهز.", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*مساعدة البوت*\n\n"
        "`🔍 فحص يدوي` - يفحص أفضل الأسواق الآن.\n"
        "`📊 الإحصائيات` - يعرض الإحصائيات.\n\n"
        "_يتم الفحص تلقائياً كل 15 دقيقة._"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 جاري الفحص اليدوي...")
    await perform_scan(context)
    await update.message.reply_text("✅ انتهى الفحص اليدوي.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(PERFORMANCE_FILE):
        await update.message.reply_text("لا توجد توصيات مسجلة بعد.")
        return
    df = pd.read_csv(PERFORMANCE_FILE)
    total = len(df)
    active = len(df[df['status'] == 'نشطة'])
    msg = f"*إحصائيات الأداء*\n\n- إجمالي التوصيات: {total}\n- الصفقات النشطة: {active}"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 الإحصائيات":
        await stats_command(update, context)
    elif text == "ℹ️ مساعدة":
        await help_command(update, context)
    elif text == "🔍 فحص يدوي":
        await manual_scan_command(update, context)

# ------------------- تهيئة التطبيق -------------------
async def post_init(application: Application):
    bot_data["clients"] = await create_exchange_clients()
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🚀 البوت متصل وجاهز.", parse_mode=ParseMode.MARKDOWN)
    application.job_queue.run_repeating(perform_scan, interval=LOOP_INTERVAL_SECONDS, first=10)

def main():
    print("🚀 Starting multi-exchange scanner bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()