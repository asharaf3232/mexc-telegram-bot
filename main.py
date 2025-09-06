# -*- coding: utf-8 -*-

import ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import time
import os
import logging
from datetime import datetime
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

## --- CONFIGURATION --- ##

# 1. API Keys from Environment Variables
MEXC_API_KEY = os.getenv('MEXC_API_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_API_SECRET')

# 2. Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not all([MEXC_API_KEY, MEXC_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("FATAL ERROR: Missing one or more environment variables.")
    exit()

# 3. Trading Strategy & Market Scan Configuration
TIMEFRAME = '15m'
LOOP_INTERVAL_SECONDS = 900  # 15 minutes
EXCLUDED_SYMBOLS = ['BTC/USDT', 'ETH/USDT']
STABLECOINS = ['USDC', 'DAI', 'BUSD', 'TUSD', 'USDP']
PERFORMANCE_FILE = 'recommendations_log.csv'

# 4. Advanced Strategy Parameters
VWAP_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STDDEV = 2.0
RSI_PERIOD = 14
RSI_MAX_LEVEL = 68

# 5. Risk Management
TAKE_PROFIT_PERCENTAGE = 4.0
STOP_LOSS_PERCENTAGE = 2.0

# --- Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
bot_data = {
    "exchange": None,
    "symbols_to_watch": [],
    "last_signal_time": {}
}

## --- CORE FUNCTIONS (Unchanged) --- ##

def get_exchange_client():
    """Initializes the MEXC exchange client."""
    try:
        exchange = ccxt.mexc({
            'apiKey': MEXC_API_KEY,
            'secret': MEXC_SECRET_KEY,
            'options': {'defaultType': 'spot'},
        })
        exchange.load_markets()
        logging.info("Successfully connected to MEXC exchange.")
        return exchange
    except Exception as e:
        logging.error(f"Failed to connect to MEXC: {e}")
        return None

async def fetch_dynamic_symbols(exchange):
    """Fetches all USDT pairs from the exchange, excluding specified symbols and stablecoins."""
    logging.info("Fetching dynamic symbols from MEXC...")
    try:
        all_symbols = [s for s in exchange.symbols if s.endswith('/USDT')]
        filtered_symbols = [s for s in all_symbols if not any(stable in s for stable in STABLECOINS)]
        final_symbols = [s for s in filtered_symbols if s not in EXCLUDED_SYMBOLS]
        logging.info(f"Found {len(final_symbols)} symbols to monitor.")
        return final_symbols
    except Exception as e:
        logging.error(f"Error fetching dynamic symbols: {e}")
        return []

def fetch_data(exchange, symbol, timeframe):
    """Fetches historical OHLCV data for a given symbol."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.warning(f"Could not fetch data for {symbol}: {e}")
        return None

def analyze_market_data(df, symbol):
    """Applies the advanced trading strategy."""
    if df is None or len(df) < BBANDS_PERIOD: return None
    try:
        df.ta.vwap(length=VWAP_PERIOD, append=True)
        df.ta.bbands(length=BBANDS_PERIOD, std=BBANDS_STDDEV, append=True)
        df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
        df.ta.rsi(length=RSI_PERIOD, append=True)
        last, prev = df.iloc[-2], df.iloc[-3]

        macd_crossover = prev[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] <= prev[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] and \
                         last[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'] > last[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
        bollinger_breakout = last['close'] > last[f'BBU_{BBANDS_PERIOD}_{BBANDS_STDDEV}']
        vwap_confirmation = last['close'] > last[f'VWAP_{VWAP_PERIOD}']
        rsi_condition = last[f'RSI_{RSI_PERIOD}'] < RSI_MAX_LEVEL

        if macd_crossover and bollinger_breakout and vwap_confirmation and rsi_condition:
            entry_price = last['close']
            return {
                "symbol": symbol, "entry_price": entry_price,
                "take_profit": entry_price * (1 + TAKE_PROFIT_PERCENTAGE / 100),
                "stop_loss": entry_price * (1 - STOP_LOSS_PERCENTAGE / 100),
                "timestamp": last['timestamp'], "reason": "MACD Crossover & Bollinger Breakout"
            }
    except Exception as e:
        logging.error(f"Error analyzing data for {symbol}: {e}")
    return None

async def send_telegram_message(bot: Bot, signal):
    """Formats and sends the trading signal to Telegram."""
    message = f"""
✅ *New Trading Signal* ✅

*Symbol:* `{signal['symbol']}`
*Strategy:* `{signal['reason']}`
*Action:* `BUY`

*Entry Price:* `${signal['entry_price']:,.4f}`

🎯 *Take Profit ({TAKE_PROFIT_PERCENTAGE}%):* `${signal['take_profit']:,.4f}`
🛑 *Stop Loss ({STOP_LOSS_PERCENTAGE}%):* `${signal['stop_loss']:,.4f}`

*Disclaimer: High-risk trade. DYOR.*
"""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logging.info(f"Successfully sent signal for {signal['symbol']} to Telegram.")
        log_recommendation(signal)
    except Exception as e:
        logging.error(f"Failed to send message to Telegram: {e}")

def log_recommendation(signal):
    """Saves the sent signal to a CSV file for performance tracking."""
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    df = pd.DataFrame([{'timestamp': signal['timestamp'], 'symbol': signal['symbol'], 'entry_price': signal['entry_price'],
                        'take_profit': signal['take_profit'], 'stop_loss': signal['stop_loss'],
                        'status': 'active', 'exit_price': None, 'closed_at': None}])
    with open(PERFORMANCE_FILE, 'a') as f:
        df.to_csv(f, header=not file_exists, index=False)
    logging.info(f"Logged recommendation for {signal['symbol']}")

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    """Performs a single market scan for all watched symbols."""
    exchange, symbols, last_signal_time = bot_data['exchange'], bot_data['symbols_to_watch'], bot_data['last_signal_time']
    found_signals = 0
    logging.info(f"Starting new market scan of {len(symbols)} symbols...")
    for symbol in symbols:
        df = fetch_data(exchange, symbol, TIMEFRAME)
        if df is not None:
            signal = analyze_market_data(df, symbol)
            if signal:
                current_time = time.time()
                if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (LOOP_INTERVAL_SECONDS * 4):
                    await send_telegram_message(context.bot, signal)
                    last_signal_time[symbol] = current_time
                    found_signals += 1
                else:
                    logging.info(f"Ignoring repeated signal for {symbol}.")
    logging.info(f"Scan complete. Found {found_signals} new signals.")

## --- TELEGRAM HANDLERS (UPDATED) --- ##

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command and displays the main menu with persistent buttons."""
    keyboard = [
        ["📊 Statistics", "ℹ️ Help"],
        ["🔍 Manual Scan"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Welcome to the Advanced Trading Bot! I am now monitoring the market. Use the buttons below to interact.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the help message."""
    help_text = """
*Advanced Trading Bot Help*

`🔍 Manual Scan` - Triggers an immediate market scan.
`📊 Statistics` - Displays performance statistics of past signals.
`ℹ️ Help` - Shows this help message.

The bot automatically scans all MEXC USDT pairs (except BTC & ETH) every 15 minutes.
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the manual scan request."""
    await update.message.reply_text("👍 Roger that! Starting a manual market scan now...")
    await perform_scan(context)
    await update.message.reply_text("✅ Manual scan complete.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays performance statistics."""
    if not os.path.exists(PERFORMANCE_FILE):
        await update.message.reply_text("No signals logged yet. Statistics are unavailable.")
        return

    df = pd.read_csv(PERFORMANCE_FILE)
    total_recs, tp_hits, sl_hits, active_trades = len(df), len(df[df['status'] == 'tp_hit']), len(df[df['status'] == 'sl_hit']), len(df[df['status'] == 'active'])
    win_rate = (tp_hits / (tp_hits + sl_hits) * 100) if (tp_hits + sl_hits) > 0 else 0
    
    stats_message = f"""
*Performance Statistics*

- *Total Signals Sent:* {total_recs}
- *Active Trades:* {active_trades}
- *Trades Hit Take Profit:* {tp_hits}
- *Trades Hit Stop Loss:* {sl_hits}
- *Win Rate (on closed trades):* `{win_rate:.2f}%`

*Note: This is a simplified performance tracker.*
"""
    await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses from the custom keyboard."""
    text = update.message.text
    if text == "📊 Statistics":
        await stats_command(update, context)
    elif text == "ℹ️ Help":
        await help_command(update, context)
    elif text == "🔍 Manual Scan":
        await manual_scan_command(update, context)

async def post_init(application: Application):
    """Function to run after the bot is initialized."""
    bot_data['exchange'] = get_exchange_client()
    if not bot_data['exchange']:
        logging.error("Could not connect to exchange. Bot cannot scan.")
        return
    bot_data['symbols_to_watch'] = await fetch_dynamic_symbols(bot_data['exchange'])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *Bot is online and running!*\n- Strategy: `Advanced (MACD, BB, VWAP)`\n- Monitored Symbols: `{len(bot_data['symbols_to_watch'])}`",
        parse_mode=ParseMode.MARKDOWN
    )
    application.job_queue.run_repeating(perform_scan, interval=LOOP_INTERVAL_SECONDS, first=10)

## --- MAIN EXECUTION --- ##

if __name__ == '__main__':
    print("🚀 Starting bot...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    # This new handler processes text messages, which includes button clicks
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print("✅ Bot is polling for updates...")
    application.run_polling()

