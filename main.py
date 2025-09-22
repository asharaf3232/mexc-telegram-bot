# -*- coding: utf-8 -*-
"""
brain.py: The Brain - العقل المركزي لنظام التداول

الوظيفة:
- هذا هو الخادم المركزي الوحيد المسؤول عن التحليل واتخاذ القرار.
- يتصل بمنصات تداول متعددة لجلب بيانات السوق في الوقت الفعلي.
- يطبق مجموعة واسعة من استراتيجيات التحليل (الماسحات) لاكتشاف فرص التداول.
- يستخدم "الذكاء التكيفي" لتحليل أداء الاستراتيجيات وتعديل السلوك بناءً على النتائج.
- ينشر قرارات التداول النهائية كرسائل JSON موحدة إلى وسيط الرسائل (Redis).
- لا يقوم بأي تداول فعلي ولا يحتفظ بأي مفاتيح API للتداول.
"""

import asyncio
import json
import logging
import os
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import redis.asyncio as redis
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- إعدادات النظام ---
# استبدل هذه القيم بالقيم الحقيقية الخاصة بك
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# إعداد تسجيل الأحداث (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- الفئات الأساسية ---

class PerformanceTracker:
    """
    فئة لتتبع أداء الاستراتيجيات وتطبيق الذكاء التكيفي.
    """
    def __init__(self):
        # هيكل بيانات لتخزين إحصائيات كل استراتيجية
        self.stats = {}
        logging.info("PerformanceTracker initialized.")

    async def update_from_report(self, trade_report: dict):
        """
        تحديث الإحصائيات بناءً على تقرير صفقة من "اليد".
        """
        strategy_id = trade_report.get("strategy_id")
        if not strategy_id:
            return

        if strategy_id not in self.stats:
            self.stats[strategy_id] = {
                "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
                "profit_factor": 0, "win_rate": 0
            }
        
        s = self.stats[strategy_id]
        s["trades"] += 1
        s["total_pnl"] += trade_report.get("pnl", 0)
        
        if trade_report.get("pnl", 0) > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
            
        total_profit = sum(r['pnl'] for r in [trade_report] if r.get('pnl', 0) > 0)
        total_loss = abs(sum(r['pnl'] for r in [trade_report] if r.get('pnl', 0) < 0))

        if total_loss > 0:
            s["profit_factor"] = total_profit / total_loss
        else:
            s["profit_factor"] = float('inf') # ربح لانهائي إذا لم تكن هناك خسائر
        
        s["win_rate"] = (s["wins"] / s["trades"]) * 100
        
        logging.info(f"Updated performance for strategy '{strategy_id}': {s}")

    def get_strategy_confidence(self, strategy_id: str) -> float:
        """
        حساب "ثقة" الاستراتيجية بناءً على أدائها.
        تتراوح النتيجة بين 0.5 (ضعيفة) و 1.5 (قوية).
        """
        if strategy_id not in self.stats or self.stats[strategy_id]["trades"] < 10:
            return 1.0  # ثقة افتراضية حتى يتم جمع بيانات كافية

        s = self.stats[strategy_id]
        # معادلة بسيطة تأخذ في الاعتبار معدل النجاح وعامل الربح
        win_rate_score = s["win_rate"] / 100  # 0 to 1
        profit_factor_score = min(s["profit_factor"], 5) / 5  # 0 to 1
        
        # نعطي وزنًا أكبر لمعدل النجاح
        confidence = 0.5 + (0.7 * win_rate_score + 0.3 * profit_factor_score)
        return round(confidence, 2)

    def get_performance_report(self) -> str:
        """
        إنشاء تقرير نصي عن أداء جميع الاستراتيجيات.
        """
        if not self.stats:
            return "No performance data available yet."
            
        report = "📊 **Strategy Performance Report** 📊\n\n"
        for strategy_id, s in self.stats.items():
            report += f"🔹 **{strategy_id}**:\n"
            report += f"   - Trades: {s['trades']}\n"
            report += f"   - Win Rate: {s['win_rate']:.2f}%\n"
            report += f"   - Profit Factor: {s['profit_factor']:.2f}\n"
            report += f"   - Total PnL: {s['total_pnl']:.4f}\n"
            report += f"   - Confidence: {self.get_strategy_confidence(strategy_id)}\n\n"
        
        return report

class Brain:
    """
    الفئة الرئيسية التي تدير جميع عمليات "العقل".
    """
    def __init__(self):
        self.exchanges_config = {
            'binance': {'enabled': True, 'mode': 'auto'}, # auto | manual | off
            'okx': {'enabled': True, 'mode': 'auto'},
            'bybit': {'enabled': True, 'mode': 'manual'},
        }
        self.symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
        self.timeframe = '5m'
        
        self.exchanges = {}
        self.performance_tracker = PerformanceTracker()
        self.redis_pub = None
        self.telegram_app = None

    async def initialize(self):
        """
        تهيئة جميع الاتصالات والمكونات.
        """
        # تهيئة اتصالات المنصات
        for ex_id in self.exchanges_config.keys():
            if self.exchanges_config[ex_id]['enabled']:
                exchange_class = getattr(ccxt, ex_id)
                self.exchanges[ex_id] = exchange_class()
                logging.info(f"Initialized exchange: {ex_id}")

        # تهيئة اتصال Redis
        self.redis_pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0)
        self.redis_pub = redis.Redis(connection_pool=self.redis_pool)
        self.redis_sub = redis.Redis(connection_pool=self.redis_pool)
        logging.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")

        # تهيئة بوت تليجرام
        self.telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.telegram_app.add_handler(CommandHandler("start", self.tg_start))
        self.telegram_app.add_handler(CommandHandler("status", self.tg_status))
        self.telegram_app.add_handler(CommandHandler("set_mode", self.tg_set_mode))
        self.telegram_app.add_handler(CommandHandler("performance", self.tg_performance))
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        await self.telegram_app.updater.start_polling()
        logging.info("Telegram bot initialized and started.")
        await self.send_telegram_message("🤖 Brain system is online and operational.")

    async def close_connections(self):
        """
        إغلاق جميع الاتصالات بأمان عند إيقاف التشغيل.
        """
        for ex in self.exchanges.values():
            await ex.close()
        await self.redis_pool.disconnect()
        await self.telegram_app.updater.stop()
        await self.telegram_app.stop()
        logging.info("All connections closed gracefully.")

    async def send_telegram_message(self, message: str):
        """
        إرسال رسالة إلى قناة تليجرام.
        """
        await self.telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')

    # --- أوامر تليجرام ---
    async def tg_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Welcome to the Trading Brain! Use /status to see the current configuration.")

    async def tg_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status_msg = "Current System Status:\n\n"
        for ex_id, config in self.exchanges_config.items():
            status_msg += f"- {ex_id.upper()}: Enabled={config['enabled']}, Mode={config['mode'].upper()}\n"
        await update.message.reply_text(status_msg)
        
    async def tg_set_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            _, ex_id, mode = update.message.text.split()
            ex_id = ex_id.lower()
            mode = mode.lower()
            if ex_id in self.exchanges_config and mode in ['auto', 'manual', 'off']:
                self.exchanges_config[ex_id]['mode'] = mode
                await update.message.reply_text(f"Mode for {ex_id.upper()} set to {mode.upper()}.")
                logging.info(f"Mode for {ex_id.upper()} set to {mode.upper()} via Telegram.")
            else:
                await update.message.reply_text("Invalid exchange or mode. Usage: /set_mode <exchange> <auto|manual|off>")
        except ValueError:
            await update.message.reply_text("Invalid command format. Usage: /set_mode <exchange> <auto|manual|off>")

    async def tg_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        report = self.performance_tracker.get_performance_report()
        await update.message.reply_text(report, parse_mode='Markdown')

    # --- منطق الماسحات والاستراتيجيات ---
    async def fetch_ohlcv(self, exchange_id: str, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """
        جلب بيانات الشموع اليابانية من المنصة.
        """
        try:
            exchange = self.exchanges[exchange_id]
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logging.error(f"Error fetching OHLCV for {symbol} from {exchange_id}: {e}")
            return pd.DataFrame()

    def rsi_divergence_scanner(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        ماسح انحراف مؤشر القوة النسبية (RSI Divergence).
        """
        if len(df) < 20: return None
        df['rsi'] = ta.rsi(df['close'], length=14)
        
        # منطق مبسط للبحث عن انحراف إيجابي (Bullish Divergence)
        last_low = df['low'].iloc[-1]
        prev_low = df['low'].iloc[-10:-2].min()
        last_rsi = df['rsi'].iloc[-1]
        prev_rsi = df['rsi'].iloc[-10:-2].min()

        if last_low < prev_low and last_rsi > prev_rsi and last_rsi < 35:
            return {
                "strategy_id": "rsi_divergence_bullish",
                "symbol": symbol,
                "direction": "long",
                "entry": df['close'].iloc[-1],
                "sl_ratio": 0.02, # وقف خسارة 2%
                "tp_ratio": 0.04, # هدف ربح 4%
            }
        return None

    def momentum_breakout_scanner(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        ماسح اختراق الزخم (Momentum Breakout).
        """
        if len(df) < 50: return None
        df['donchian_h'] = ta.donchian(df['high'], df['low'], lower_length=20, upper_length=20)['DCU_20_20']
        
        last_close = df['close'].iloc[-1]
        prev_high = df['donchian_h'].iloc[-2]
        
        if last_close > prev_high:
            return {
                "strategy_id": "momentum_breakout_long",
                "symbol": symbol,
                "direction": "long",
                "entry": last_close,
                "sl_ratio": 0.03,
                "tp_ratio": 0.06,
            }
        return None
    
    # --- حلقة العمل الرئيسية ---
    async def run_scanners(self):
        """
        الحلقة الرئيسية التي تقوم بتشغيل الماسحات بشكل دوري.
        """
        scanners = [self.rsi_divergence_scanner, self.momentum_breakout_scanner]
        
        while True:
            logging.info("Starting new scan cycle...")
            for ex_id, exchange in self.exchanges.items():
                if not self.exchanges_config[ex_id]['enabled']:
                    continue
                    
                for symbol in self.symbols:
                    df = await self.fetch_ohlcv(ex_id, symbol, self.timeframe)
                    if df.empty:
                        continue
                        
                    for scanner_func in scanners:
                        signal = scanner_func(df, symbol)
                        if signal:
                            await self.process_signal(ex_id, signal)
            
            await asyncio.sleep(60) # الانتظار لمدة 60 ثانية قبل الدورة التالية

    async def process_signal(self, exchange_id: str, signal: dict):
        """
        معالجة الإشارة المكتشفة بناءً على وضع التنفيذ.
        """
        mode = self.exchanges_config[exchange_id]['mode']
        confidence = self.performance_tracker.get_strategy_confidence(signal['strategy_id'])
        
        # إضافة البيانات الوصفية إلى الإشارة
        signal['exchange'] = exchange_id
        signal['confidence'] = confidence

        logging.info(f"Signal found on {exchange_id.upper()} for {signal['symbol']} via {signal['strategy_id']} with confidence {confidence}.")

        if mode == 'auto':
            # النشر إلى Redis
            await self.redis_pub.publish("trade_signals", json.dumps(signal))
            logging.info(f"Published signal to Redis: {signal}")
            await self.send_telegram_message(f"🚀 **AUTO-TRADE EXECUTED**\n- Exchange: {exchange_id.upper()}\n- Signal: `{json.dumps(signal)}`")
        
        elif mode == 'manual':
            # إرسال توصية إلى تليجرام
            recommendation = (f"🔔 **MANUAL TRADE RECOMMENDATION** 🔔\n\n"
                              f"**Exchange**: {exchange_id.upper()}\n"
                              f"**Symbol**: {signal['symbol']}\n"
                              f"**Direction**: {signal['direction'].upper()}\n"
                              f"**Strategy**: {signal['strategy_id']}\n"
                              f"**Confidence**: {signal['confidence']}\n"
                              f"**Entry Price**: ~{signal['entry']}\n"
                              f"**Stop Loss**: ~{signal['entry'] * (1 - signal['sl_ratio'])}\n"
                              f"**Take Profit**: ~{signal['entry'] * (1 + signal['tp_ratio'])}")
            await self.send_telegram_message(recommendation)
            logging.info(f"Sent manual recommendation to Telegram for signal: {signal}")
        
        # وضع 'off' لا يفعل شيئًا

    async def listen_for_statistics(self):
        """
        الاستماع لتقارير نتائج الصفقات من "الأيدي".
        """
        pubsub = self.redis_sub.pubsub()
        await pubsub.subscribe("trade_statistics")
        logging.info("Subscribed to 'trade_statistics' channel.")
        
        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    trade_report = json.loads(message['data'])
                    logging.info(f"Received trade report: {trade_report}")
                    await self.performance_tracker.update_from_report(trade_report)
                    
                    report_msg = (f"✅ **Trade Closed & Reported**\n\n"
                                  f"**Exchange**: {trade_report.get('exchange', 'N/A').upper()}\n"
                                  f"**Symbol**: {trade_report.get('symbol', 'N/A')}\n"
                                  f"**Strategy**: {trade_report.get('strategy_id', 'N/A')}\n"
                                  f"**PnL**: {trade_report.get('pnl', 0):.4f} USDT\n"
                                  f"**Result**: {'WIN' if trade_report.get('pnl', 0) > 0 else 'LOSS'}")
                    await self.send_telegram_message(report_msg)
                except json.JSONDecodeError:
                    logging.error("Failed to decode trade report JSON.")
                except Exception as e:
                    logging.error(f"Error processing trade report: {e}")

async def main():
    brain = Brain()
    await brain.initialize()
    
    # تشغيل المهام بشكل متزامن
    scanner_task = asyncio.create_task(brain.run_scanners())
    stats_listener_task = asyncio.create_task(brain.listen_for_statistics())
    
    try:
        await asyncio.gather(scanner_task, stats_listener_task)
    except asyncio.CancelledError:
        logging.info("Main tasks cancelled.")
    finally:
        await brain.close_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Brain shutting down...")

