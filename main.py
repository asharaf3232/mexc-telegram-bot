# -*- coding: utf-8 -*-
"""
brain.py: The Brain - العقل المركزي لنظام التداول (نسخة مطورة)

الوظيفة:
- هذا هو الخادم المركزي الوحيد المسؤول عن التحليل واتخاذ القرار.
- يتصل بمنصات تداول متعددة لجلب بيانات السوق في الوقت الفعلي.
- يطبق مجموعة واسعة من استراتيجيات التحليل (الماسحات) لاكتشاف فرص التداول.
- يستخدم "الذكاء التكيفي" لتحليل أداء الاستراتيجيات وتعديل السلوك بناءً على النتائج.
- ينشر قرارات التداول النهائية كرسائل JSON موحدة إلى وسيط الرسائل (Redis).
- يحتوي على واجهة تحكم تفاعلية كاملة عبر تليجرام باللغة العربية.
"""

import asyncio
import json
import logging
import os
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import redis.asyncio as redis
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- إعدادات النظام ---
# استبدل هذه القيم بالقيم الحقيقية الخاصة بك
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# إعداد تسجيل الأحداث (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class PerformanceTracker:
    """
    فئة لتتبع أداء الاستراتيجيات وتطبيق الذكاء التكيفي.
    """
    def __init__(self):
        self.stats = {}
        logging.info("PerformanceTracker initialized.")

    async def update_from_report(self, trade_report: dict):
        strategy_id = trade_report.get("strategy_id")
        if not strategy_id: return

        if strategy_id not in self.stats:
            self.stats[strategy_id] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "profit_factor": 0, "win_rate": 0}
        
        s = self.stats[strategy_id]
        s["trades"] += 1
        pnl = trade_report.get("pnl", 0)
        s["total_pnl"] += pnl
        if pnl > 0: s["wins"] += 1
        else: s["losses"] += 1
            
        all_pnls = self.stats[strategy_id].get('all_pnls', [])
        all_pnls.append(pnl)
        self.stats[strategy_id]['all_pnls'] = all_pnls

        total_profit = sum(p for p in all_pnls if p > 0)
        total_loss = abs(sum(p for p in all_pnls if p < 0))

        s["profit_factor"] = total_profit / total_loss if total_loss > 0 else float('inf')
        s["win_rate"] = (s["wins"] / s["trades"]) * 100
        logging.info(f"Updated performance for strategy '{strategy_id}': {s}")

    def get_strategy_confidence(self, strategy_id: str) -> float:
        if strategy_id not in self.stats or self.stats[strategy_id]["trades"] < 10: return 1.0
        s = self.stats[strategy_id]
        win_rate_score = s["win_rate"] / 100
        profit_factor_score = min(s["profit_factor"], 5) / 5
        confidence = 0.5 + (0.7 * win_rate_score + 0.3 * profit_factor_score)
        return round(confidence, 2)

    def get_performance_report(self) -> str:
        if not self.stats: return "لا توجد بيانات أداء متاحة بعد."
        report = "📊 **تقرير أداء الاستراتيجيات** 📊\n\n"
        sorted_stats = sorted(self.stats.items(), key=lambda item: item[1]['total_pnl'], reverse=True)
        for strategy_id, s in sorted_stats:
            report += f"🔹 **{strategy_id}**:\n"
            report += f"   - عدد الصفقات: {s['trades']}\n"
            report += f"   - نسبة النجاح: {s['win_rate']:.2f}%\n"
            report += f"   - عامل الربح: {s['profit_factor']:.2f}\n"
            report += f"   - إجمالي الربح/الخسارة: {s['total_pnl']:.4f}\n"
            report += f"   - معامل الثقة: {self.get_strategy_confidence(strategy_id)}\n\n"
        return report

class Brain:
    """
    الفئة الرئيسية التي تدير جميع عمليات "العقل" مع واجهة تحكم متقدمة.
    """
    def __init__(self):
        self.exchanges_config = {
            'binance': {'mode': 'auto'}, 
            'okx': {'mode': 'auto'},
            'bybit': {'mode': 'manual'},
        }
        self.symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
        self.timeframe = '5m'
        
        # --- إدارة الماسحات والمعايير ---
        self.scanners = {
            'rsi_divergence': self.rsi_divergence_scanner,
            'momentum_breakout': self.momentum_breakout_scanner,
        }
        self.scanners_status = {name: True for name in self.scanners.keys()}
        self.scanner_params = {
            'rsi_divergence': {'sl_ratio': 0.02, 'tp_ratio': 0.04, 'rsi_length': 14, 'rsi_oversold': 35},
            'momentum_breakout': {'sl_ratio': 0.03, 'tp_ratio': 0.06, 'donchian_period': 20}
        }
        
        self.exchanges = {}
        self.performance_tracker = PerformanceTracker()
        self.redis_pub = None
        self.telegram_app = None

    async def initialize(self):
        for ex_id in self.exchanges_config.keys():
            exchange_class = getattr(ccxt, ex_id)
            self.exchanges[ex_id] = exchange_class()
        
        self.redis_pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        self.redis_pub = redis.Redis(connection_pool=self.redis_pool)
        self.redis_sub = redis.Redis(connection_pool=self.redis_pool)
        
        self.telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.setup_telegram_handlers()
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        await self.telegram_app.updater.start_polling()
        logging.info("Telegram bot initialized and started.")
        await self.send_telegram_message("🤖 **عقل التداول** متصل وجاهز للعمل.\nاستخدم /help لعرض قائمة الأوامر.")

    def setup_telegram_handlers(self):
        """إعداد جميع أوامر التليجرام باللغة العربية."""
        handlers = {
            "start": self.tg_start, "help": self.tg_start,
            "status": self.tg_status,
            "set_mode": self.tg_set_mode,
            "performance": self.tg_performance,
            "scanners": self.tg_scanners_status,
            "toggle_scanner": self.tg_toggle_scanner,
            "params": self.tg_show_params,
            "set_param": self.tg_set_param,
        }
        for command, handler_func in handlers.items():
            self.telegram_app.add_handler(CommandHandler(command, handler_func))

    async def close_connections(self):
        for ex in self.exchanges.values(): await ex.close()
        await self.redis_pool.disconnect()
        await self.telegram_app.updater.stop()
        await self.telegram_app.stop()
        logging.info("All connections closed gracefully.")

    async def send_telegram_message(self, message: str):
        await self.telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')

    # --- أوامر تليجرام (باللغة العربية) ---
    async def tg_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "🤖 **أهلاً بك في لوحة تحكم عقل التداول!**\n\n"
            "**الأوامر المتاحة:**\n"
            "/status - عرض الحالة الحالية للمنصات.\n"
            "/set_mode `<exchange>` `<mode>` - لضبط وضع التنفيذ (auto, manual, off).\n"
            "/performance - لعرض تقرير أداء الاستراتيجيات.\n\n"
            "**إدارة الماسحات:**\n"
            "/scanners - عرض حالة جميع الماسحات (مفعل/معطل).\n"
            "/toggle_scanner `<scanner_name>` - لتفعيل أو تعطيل ماسح معين.\n\n"
            "**إدارة المعايير:**\n"
            "/params `<scanner_name>` - عرض معايير ماسح معين.\n"
            "/set_param `<scanner_name>` `<param>` `<value>` - لتعديل قيمة معيار."
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def tg_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status_msg = "⚙️ **الحالة الحالية للنظام** ⚙️\n\n"
        for ex_id, config in self.exchanges_config.items():
            mode_map = {'auto': 'تلقائي', 'manual': 'يدوي', 'off': 'متوقف'}
            status_msg += f"- منصة **{ex_id.upper()}**: الوضع = **{mode_map.get(config['mode'])}**\n"
        await update.message.reply_text(status_msg, parse_mode='Markdown')

    async def tg_set_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            _, ex_id, mode = update.message.text.split()
            ex_id, mode = ex_id.lower(), mode.lower()
            if ex_id in self.exchanges_config and mode in ['auto', 'manual', 'off']:
                self.exchanges_config[ex_id]['mode'] = mode
                await update.message.reply_text(f"✅ تم تغيير وضع منصة {ex_id.upper()} إلى {mode.upper()}.")
            else:
                await update.message.reply_text("خطأ: اسم المنصة أو الوضع غير صحيح.\nمثال: `/set_mode okx auto`")
        except (ValueError, IndexError):
            await update.message.reply_text("صيغة الأمر غير صحيحة.\nاستخدم: `/set_mode <exchange> <mode>`")
    
    async def tg_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(self.performance_tracker.get_performance_report(), parse_mode='Markdown')

    async def tg_scanners_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status_msg = "📡 **حالة الماسحات (الاستراتيجيات)** 📡\n\n"
        for name, status in self.scanners_status.items():
            icon = "🟢" if status else "🔴"
            status_text = "مفعل" if status else "معطل"
            status_msg += f"{icon} **{name}**: {status_text}\n"
        await update.message.reply_text(status_msg, parse_mode='Markdown')

    async def tg_toggle_scanner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            scanner_name = context.args[0].lower()
            if scanner_name in self.scanners_status:
                self.scanners_status[scanner_name] = not self.scanners_status[scanner_name]
                status_text = "تفعيله" if self.scanners_status[scanner_name] else "تعطيله"
                await update.message.reply_text(f"✅ تم {status_text} بنجاح للماسح: `{scanner_name}`.")
            else:
                await update.message.reply_text(f"لم يتم العثور على ماسح بالاسم: `{scanner_name}`.")
        except (ValueError, IndexError):
            await update.message.reply_text("صيغة الأمر غير صحيحة.\nاستخدم: `/toggle_scanner <scanner_name>`")

    async def tg_show_params(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            scanner_name = context.args[0].lower()
            if scanner_name in self.scanner_params:
                params = self.scanner_params[scanner_name]
                params_msg = f"🔧 **معايير الماسح: {scanner_name}** 🔧\n\n"
                for key, value in params.items():
                    params_msg += f"- `{key}` = `{value}`\n"
                await update.message.reply_text(params_msg, parse_mode='Markdown')
            else:
                await update.message.reply_text(f"لم يتم العثور على ماسح بالاسم: `{scanner_name}`.")
        except (ValueError, IndexError):
            await update.message.reply_text("صيغة الأمر غير صحيحة.\nاستخدم: `/params <scanner_name>`")

    async def tg_set_param(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            scanner_name, param, value = context.args[0].lower(), context.args[1].lower(), context.args[2]
            if scanner_name not in self.scanner_params or param not in self.scanner_params[scanner_name]:
                await update.message.reply_text("خطأ: اسم الماسح أو المعيار غير صحيح.")
                return
            
            # محاولة تحويل القيمة إلى رقم عشري أو صحيح
            try:
                original_type = type(self.scanner_params[scanner_name][param])
                self.scanner_params[scanner_name][param] = original_type(value)
                await update.message.reply_text(f"✅ تم تحديث المعيار `{param}` للماسح `{scanner_name}` إلى القيمة `{value}`.")
            except ValueError:
                await update.message.reply_text("خطأ: القيمة المدخلة يجب أن تكون رقمًا صالحًا.")
        except (ValueError, IndexError):
            await update.message.reply_text("صيغة الأمر غير صحيحة.\nاستخدم: `/set_param <scanner> <param> <value>`")


    # --- منطق الماسحات (تستخدم المعايير الديناميكية) ---
    async def fetch_ohlcv(self, exchange_id: str, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        try:
            exchange = self.exchanges[exchange_id]
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logging.error(f"Error fetching OHLCV for {symbol} from {exchange_id}: {e}")
            return pd.DataFrame()

    def rsi_divergence_scanner(self, df: pd.DataFrame, symbol: str, params: dict) -> dict:
        if len(df) < 20: return None
        df['rsi'] = ta.rsi(df['close'], length=params.get('rsi_length', 14))
        
        last_low, prev_low = df['low'].iloc[-1], df['low'].iloc[-10:-2].min()
        last_rsi, prev_rsi = df['rsi'].iloc[-1], df['rsi'].iloc[-10:-2].min()

        if last_low < prev_low and last_rsi > prev_rsi and last_rsi < params.get('rsi_oversold', 35):
            return {"strategy_id": "rsi_divergence", "symbol": symbol, "direction": "long", "entry": df['close'].iloc[-1],
                    "sl_ratio": params.get('sl_ratio'), "tp_ratio": params.get('tp_ratio')}
        return None

    def momentum_breakout_scanner(self, df: pd.DataFrame, symbol: str, params: dict) -> dict:
        period = params.get('donchian_period', 20)
        if len(df) < period + 1: return None
        df['donchian_h'] = df['high'].rolling(window=period).max()
        
        if df['close'].iloc[-1] > df['donchian_h'].iloc[-2]:
            return {"strategy_id": "momentum_breakout", "symbol": symbol, "direction": "long", "entry": df['close'].iloc[-1],
                    "sl_ratio": params.get('sl_ratio'), "tp_ratio": params.get('tp_ratio')}
        return None

    # --- حلقة العمل الرئيسية ---
    async def run_scanners(self):
        while True:
            logging.info("Starting new scan cycle...")
            for ex_id, exchange in self.exchanges.items():
                if self.exchanges_config[ex_id]['mode'] == 'off': continue
                
                for symbol in self.symbols:
                    df = await self.fetch_ohlcv(ex_id, symbol, self.timeframe)
                    if df.empty: continue
                    
                    for name, func in self.scanners.items():
                        if self.scanners_status.get(name): # التحقق إذا كان الماسح مفعل
                            params = self.scanner_params.get(name, {})
                            signal = func(df.copy(), symbol, params)
                            if signal:
                                await self.process_signal(ex_id, signal)
            
            await asyncio.sleep(60)

    async def process_signal(self, exchange_id: str, signal: dict):
        mode = self.exchanges_config[exchange_id]['mode']
        confidence = self.performance_tracker.get_strategy_confidence(signal['strategy_id'])
        
        signal.update({'exchange': exchange_id, 'confidence': confidence})
        logging.info(f"Signal found: {signal}")

        if mode == 'auto':
            await self.redis_pub.publish("trade_signals", json.dumps(signal))
            await self.send_telegram_message(f"🚀 **أمر تلقائي قيد التنفيذ**\n- المنصة: {exchange_id.upper()}\n- الإشارة: `{json.dumps(signal)}`")
        elif mode == 'manual':
            rec = (f"🔔 **توصية تداول يدوية** 🔔\n\n"
                   f"**المنصة**: {exchange_id.upper()}\n**العملة**: {signal['symbol']}\n"
                   f"**الاتجاه**: {'شراء' if signal['direction'] == 'long' else 'بيع'}\n"
                   f"**الاستراتيجية**: {signal['strategy_id']}\n**الثقة**: {signal['confidence']}\n"
                   f"**سعر الدخول التقريبي**: {signal['entry']}\n"
                   f"**وقف الخسارة**: ~{signal['entry'] * (1 - signal['sl_ratio'])}\n"
                   f"**جني الأرباح**: ~{signal['entry'] * (1 + signal['tp_ratio'])}")
            await self.send_telegram_message(rec)

    async def listen_for_statistics(self):
        pubsub = self.redis_sub.pubsub()
        await pubsub.subscribe("trade_statistics")
        logging.info("Subscribed to 'trade_statistics' channel.")
        
        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    report = json.loads(message['data'])
                    logging.info(f"Received trade report: {report}")
                    await self.performance_tracker.update_from_report(report)
                    
                    pnl = report.get('pnl', 0)
                    result_icon = "✅" if pnl > 0 else "❌"
                    report_msg = (f"{result_icon} **تم إغلاق صفقة والإبلاغ عنها**\n\n"
                                  f"**المنصة**: {report.get('exchange', 'N/A').upper()}\n"
                                  f"**العملة**: {report.get('symbol', 'N/A')}\n"
                                  f"**الاستراتيجية**: {report.get('strategy_id', 'N/A')}\n"
                                  f"**الربح/الخسارة**: {pnl:.4f} USDT")
                    await self.send_telegram_message(report_msg)
                except Exception as e:
                    logging.error(f"Error processing trade report: {e}")

async def main():
    brain = Brain()
    await brain.initialize()
    await asyncio.gather(brain.run_scanners(), brain.listen_for_statistics())
    await brain.close_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Brain shutting down...")


