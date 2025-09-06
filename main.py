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
    exchange_ids = EXCHANGES_TO_SCAN
    async def connect_with_retries(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True})
        for attempt in range(3):
            try:
                await exchange.load_markets()
                bot_data["exchanges"][ex_id] = exchange
                logging.info(f"Successfully connected to {ex_id} on attempt {attempt + 1}")
                return
            except Exception as e:
                logging.error(f"Attempt {attempt + 1} failed to connect to {ex_id}: {type(e).__name__} - {e}")
                if attempt < 2: await asyncio.sleep(10)
        await exchange.close()
    tasks = [connect_with_retries(ex_id) for ex_id in exchange_ids]
    await asyncio.gather(*tasks)

async def aggregate_top_movers():
    all_tickers = []
    logging.info("Aggregating top movers...")
    async def fetch_for_exchange(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            for symbol, data in tickers.items(): data['exchange'] = ex_id
            return list(tickers.values())
        except Exception: return []
    tasks = [fetch_for_exchange(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()]
    results = await asyncio.gather(*tasks)
    for res in results: all_tickers.extend(res)
    usdt_tickers = [t for t in all_tickers if t.get('symbol') and t['symbol'].endswith('/USDT')]
    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0) or 0, reverse=True)
    unique_symbols = {}
    for ticker in sorted_tickers:
        symbol = ticker['symbol']
        if symbol not in unique_symbols: unique_symbols[symbol] = {'exchange': ticker['exchange'], 'symbol': symbol}
    final_list = list(unique_symbols.values())[:bot_data["settings"]['top_n_symbols_by_volume']]
    logging.info(f"Aggregated top {len(final_list)} markets.")
    return final_list

async def worker(queue, results_list, settings):
    while not queue.empty():
        try:
            market_info = await queue.get()
            active_strategy_func = STRATEGIES.get(settings['active_strategy'])
            if not active_strategy_func: continue
            exchange_id, symbol = market_info['exchange'], market_info['symbol']
            exchange = bot_data["exchanges"].get(exchange_id)
            if not exchange: continue
            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=150)
            if len(ohlcv) >= 20:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                analysis_result = active_strategy_func(df, settings[settings['active_strategy']])
                if analysis_result:
                    entry_price = df.iloc[-2]['close']
                    signal = {"symbol": symbol, "exchange": exchange_id.capitalize(), "entry_price": entry_price, "take_profit": entry_price * (1 + settings['take_profit_percentage'] / 100), "stop_loss": entry_price * (1 - settings['stop_loss_percentage'] / 100), "timestamp": df.index[-2], "reason": analysis_result['reason']}
                    results_list.append(signal)
            queue.task_done()
        except Exception: queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    settings = bot_data["settings"]
    if settings.get('market_regime_filter_enabled', True) and not await check_market_regime():
        logging.info("Skipping scan due to bearish market conditions.")
        return
    top_markets = await aggregate_top_movers()
    if not top_markets: logging.info("No markets to scan."); return
    logging.info(f"Starting concurrent scan for {len(top_markets)} markets with {settings['concurrent_workers']} workers...")
    queue = asyncio.Queue()
    for market in top_markets: await queue.put(market)
    signals = []
    worker_tasks = [asyncio.create_task(worker(queue, signals, settings)) for _ in range(settings['concurrent_workers'])]
    await queue.join()
    for task in worker_tasks: task.cancel()
    found_signals = 0
    last_signal_time = bot_data['last_signal_time']
    for signal in signals:
        symbol = signal['symbol']
        current_time = time.time()
        if symbol not in last_signal_time or (current_time - last_signal_time.get(symbol, 0)) > (SCAN_INTERVAL_SECONDS * 4):
            await send_telegram_message(context.bot, signal, is_new=True)
            last_signal_time[symbol] = current_time
            found_signals += 1
    logging.info(f"Concurrent scan complete. Found {found_signals} new signals.")

def log_recommendation(signal):
    file_exists = os.path.isfile(PERFORMANCE_FILE)
    log_entry = {'timestamp': signal['timestamp'], 'exchange': signal['exchange'], 'symbol': signal['symbol'], 'entry_price': signal['entry_price'], 'take_profit': signal['take_profit'], 'stop_loss': signal['stop_loss'], 'status': 'نشطة', 'exit_price': 'N/A', 'closed_at': 'N/A', 'trailing_sl_active': False, 'highest_price': signal['entry_price']}
    df = pd.DataFrame([log_entry])
    with open(PERFORMANCE_FILE, 'a', newline='', encoding='utf-8-sig') as f:
        df.to_csv(f, header=not file_exists, index=False)

async def send_telegram_message(bot, signal_data, is_new=False, status=None, update_type=None):
    message = ""
    if is_new:
        message = f"✅ *توصية تداول جديدة* ✅\n\n*المنصة:* `{signal_data['exchange']}`\n*العملة:* `{signal_data['symbol']}`\n\n*سعر الدخول:* `${signal_data['entry_price']:,.4f}`\n🎯 *جني الأرباح:* `${signal_data['take_profit']:,.4f}`\n🛑 *وقف الخسارة:* `${signal_data['stop_loss']:,.4f}`"
        log_recommendation(signal_data)
    elif status == 'ناجحة':
        profit_percent = (signal_data['exit_price'] / signal_data['entry_price'] - 1) * 100
        message = f"🎯 *هدف محقق!* 🎯\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n*الربح:* `~{profit_percent:.2f}%`"
    elif status == 'فاشلة':
        loss_percent = (1 - signal_data['exit_price'] / signal_data['entry_price']) * 100
        message = f"🛑 *تم تفعيل وقف الخسارة* 🛑\n\n*العملة:* `{signal_data['symbol']}`\n*المنصة:* `{signal_data['exchange']}`\n*الخسارة:* `~{loss_percent:.2f}%`"
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين أرباح* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم تفعيل وقف الخسارة المتحرك عند سعر `${signal_data['stop_loss']:,.4f}`."
    if message:
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logging.error(f"Failed to send Telegram message: {e}")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(PERFORMANCE_FILE): return
    df = pd.read_csv(PERFORMANCE_FILE)
    active_trades_df = df[df['status'] == 'نشطة'].copy()
    if active_trades_df.empty: return
    logging.info(f"Tracking {len(active_trades_df)} active trade(s)...")
    async def check_trade(index, trade):
        exchange_id = trade['exchange'].lower()
        exchange = bot_data["exchanges"].get(exchange_id)
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol'])
            current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None
            if current_price >= trade['take_profit']: return {'index': index, 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']: return {'index': index, 'status': 'فاشلة', 'exit_price': current_price}
            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade.get('highest_price', trade['entry_price']), current_price)
                trailing_sl_active = trade.get('trailing_sl_active', False)
                if not trailing_sl_active and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price'] * (1 + (settings['trailing_sl_activate_percent'] - settings['trailing_sl_percent']) / 100)
                    if new_sl > trade['stop_loss']: return {'index': index, 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price, 'tsl_active': True}
                elif trailing_sl_active:
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'index': index, 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade['highest_price']: return {'index': index, 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None
    tasks = [check_trade(index, trade) for index, trade in active_trades_df.iterrows()]
    results = await asyncio.gather(*tasks)
    file_was_updated = False
    for result in filter(None, results):
        index, status = result['index'], result['status']
        if status in ['ناجحة', 'فاشلة']:
            df.loc[index, 'status'] = status; df.loc[index, 'exit_price'] = result['exit_price']; df.loc[index, 'closed_at'] = pd.to_datetime('now', utc=True)
            await send_telegram_message(context.bot, df.loc[index].to_dict(), status=status)
            file_was_updated = True
        elif status == 'update_tsl':
            df.loc[index, 'stop_loss'] = result['new_sl']; df.loc[index, 'highest_price'] = result['highest_price']; df.loc[index, 'trailing_sl_active'] = True
            await send_telegram_message(context.bot, df.loc[index].to_dict(), update_type='tsl_activation')
            file_was_updated = True
        elif status == 'update_sl':
            df.loc[index, 'stop_loss'] = result['new_sl']; df.loc[index, 'highest_price'] = result['highest_price']
            file_was_updated = True
        elif status == 'update_peak':
            df.loc[index, 'highest_price'] = result['highest_price']
            file_was_updated = True
    if file_was_updated:
        df.to_csv(PERFORMANCE_FILE, index=False, encoding='utf-8-sig')
        logging.info(f"Updated {len(list(filter(None, results)))} trade(s) in performance file.")

async def check_market_regime():
    try:
        binance = bot_data["exchanges"].get('binance')
        if not binance: return True
        ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma50'] = df['close'].rolling(window=50).mean()
        is_bullish = df['close'].iloc[-1] > df['sma50'].iloc[-1]
        logging.info(f"Market Regime is {'BULLISH' if is_bullish else 'BEARISH'}.")
        return is_bullish
    except Exception as e:
        logging.error(f"Error in market regime check: {e}"); return True

## --- لوحات المفاتيح والأوامر --- ##
main_menu_keyboard = [["📊 الإحصائيات", "ℹ️ مساعدة"], ["🔍 فحص يدوي", "⚙️ الإعدادات"]]
settings_menu_keyboard = [["📈 تغيير الاستراتيجية", "🔧 تعديل المعايير"], ["🔙 القائمة الرئيسية"]]
strategy_menu_keyboard = [["🚀 الزخم والاندفاع", "🔄 الارتداد من الدعم"], ["🔙 قائمة الإعدادات"]]

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
    await update.message.reply_text(f"لتعديل معيار، أرسل رسالة بالصيغة:\n`اسم_المعيار = قيمة_جديدة`\n\n*المعايير القابلة للتعديل:*\n{params_list}", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardMarkup([["🔙 قائمة الإعدادات"]], resize_keyboard=True))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*مساعدة البوت*\n`🔍 فحص يدوي` - يفحص السوق إذا كانت الظروف مواتية.\n`📊 الإحصائيات` - يعرض أداء التوصيات.\n`ℹ️ مساعدة` - يعرض هذه الرسالة.", parse_mode=ParseMode.MARKDOWN)

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👍 حسناً! جاري التحقق من حالة السوق أولاً...")
    await perform_scan(context)
    await update.message.reply_text("✅ اكتمل الفحص.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(PERFORMANCE_FILE): await update.message.reply_text("لم يتم تسجيل أي توصيات بعد."); return
    try:
        df = pd.read_csv(PERFORMANCE_FILE)
        if df.empty: await update.message.reply_text("ملف الإحصائيات فارغ."); return
        total, active, successful, failed = len(df), len(df[df['status'] == 'نشطة']), len(df[df['status'] == 'ناجحة']), len(df[df['status'] == 'فاشلة'])
        closed_trades = successful + failed
        win_rate = (successful / closed_trades * 100) if closed_trades > 0 else 0
        stats_message = f"""*📊 إحصائيات أداء التوصيات*\n\n- *إجمالي التوصيات:* `{total}`\n- *النشطة حالياً:* `{active}`\n- *الناجحة:* `{successful}` ✅\n- *الفاشلة:* `{failed}` ❌\n- *معدل النجاح (للصفقات المغلقة):* `{win_rate:.2f}%`"""
        await update.message.reply_text(stats_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: await update.message.reply_text(f"حدث خطأ: {e}")

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 الإحصائيات": await stats_command(update, context)
    elif text == "ℹ️ مساعدة": await help_command(update, context)
    elif text == "🔍 فحص يدوي": await manual_scan_command(update, context)
    elif text == "⚙️ الإعدادات": await show_settings_menu(update, context)
    elif text == "📈 تغيير الاستراتيجية": await show_strategy_menu(update, context)
    elif text == "🔧 تعديل المعايير": await show_set_parameter_instructions(update, context)
    elif text == "🔙 القائمة الرئيسية": await start_command(update, context)
    elif text == "🚀 الزخم والاندفاع":
        bot_data["settings"]["active_strategy"] = "momentum_breakout"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الزخم والاندفاع`.")
        await show_settings_menu(update, context)
    elif text == "🔄 الارتداد من الدعم":
        bot_data["settings"]["active_strategy"] = "mean_reversion"
        save_settings()
        await update.message.reply_text("✅ تم تفعيل استراتيجية `الارتداد من الدعم`.")
        await show_settings_menu(update, context)
    elif text == "🔙 قائمة الإعدادات":
        await show_settings_menu(update, context)
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

async def post_init(application: Application):
    """دالة تعمل بعد تهيئة البوت وقبل بدء التشغيل."""
    await asyncio.sleep(5) # فترة إحماء للشبكة
    await initialize_exchanges()
    if not bot_data["exchanges"]:
        logging.critical("CRITICAL: Failed to connect to any exchange. Bot cannot run.")
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="❌ فشل البوت في الاتصال بأي منصة. يرجى التحقق من السجل.")
        application.stop()
        return

    application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
    application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20)
    exchange_names = ", ".join([ex.capitalize() for ex in bot_data["exchanges"].keys()])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🚀 *بوت التداول الاحترافي جاهز للعمل!*\n- *المنصات:* `{exchange_names}`\n- *الاستراتيجية:* `{bot_data['settings']['active_strategy']}`",
        parse_mode=ParseMode.MARKDOWN
    )
    logging.info("Bot initialization complete and jobs scheduled.")

async def post_shutdown(application: Application):
    """دالة تعمل عند إيقاف البوت لضمان الإغلاق النظيف."""
    logging.info("Closing all exchange connections...")
    for exchange in bot_data["exchanges"].values():
        await exchange.close()
    logging.info("Connections closed successfully.")

def main():
    """الدالة الرئيسية المنظمة للتشغيل."""
    print("🚀 Starting Professional Trading Bot...")
    load_settings()
    
    application = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler))
    
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.critical(f"Bot stopped due to a critical error in __main__: {e}")

