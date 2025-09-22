# -*- coding: utf-8 -*-
"""
brain.py: The Brain - العقل المركزي لنظام التداول (نسخة بواجهة أزرار تفاعلية)

الوظيفة:
- هذا هو الخادم المركزي الوحيد المسؤول عن التحليل واتخاذ القرار.
- يحتوي على واجهة تحكم تفاعلية كاملة عبر تليجرام باستخدام الأزرار (InlineKeyboardMarkup).
- بقية الوظائف الأساسية كما هي: تحليل، إدارة أداء، ونشر إشارات التداول.
"""

import asyncio
import json
import logging
import os
from typing import List
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import redis.asyncio as redis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# --- إعدادات النظام ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class PerformanceTracker:
    # ... (هذا الجزء لم يتغير، تم إخفاؤه للاختصار)
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
            self.stats[strategy_id] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "profit_factor": 0, "win_rate": 0, "all_pnls": []}
        
        s = self.stats[strategy_id]
        s["trades"] += 1
        pnl = trade_report.get("pnl", 0)
        s["total_pnl"] += pnl
        if pnl > 0: s["wins"] += 1
        else: s["losses"] += 1
            
        s['all_pnls'].append(pnl)

        total_profit = sum(p for p in s['all_pnls'] if p > 0)
        total_loss = abs(sum(p for p in s['all_pnls'] if p < 0))

        s["profit_factor"] = total_profit / total_loss if total_loss > 0 else float('inf')
        s["win_rate"] = (s["wins"] / s["trades"]) * 100
        logging.info(f"Updated performance for strategy '{strategy_id}': {s}")

    def get_strategy_confidence(self, strategy_id: str) -> float:
        if strategy_id not in self.stats or self.stats[strategy_id]["trades"] < 5: return 1.0
        s = self.stats[strategy_id]
        win_rate_score = s["win_rate"] / 100
        profit_factor_score = min(s["profit_factor"], 4) / 4
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
    الفئة الرئيسية التي تدير جميع عمليات "العقل" مع واجهة أزرار تفاعلية.
    """
    def __init__(self):
        self.exchanges_config = {'binance': {'mode': 'auto'}, 'okx': {'mode': 'auto'}, 'bybit': {'mode': 'manual'}}
        self.symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
        self.timeframe = '5m'
        
        self.scanners = {'rsi_divergence': self.rsi_divergence_scanner, 'momentum_breakout': self.momentum_breakout_scanner}
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
        for ex_id in self.exchanges_config.keys(): self.exchanges[ex_id] = getattr(ccxt, ex_id)()
        self.redis_pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        self.redis_pub = redis.Redis(connection_pool=self.redis_pool)
        self.redis_sub = redis.Redis(connection_pool=self.redis_pool)
        
        self.telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.setup_telegram_handlers()
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        await self.telegram_app.updater.start_polling()
        logging.info("Telegram bot with interactive buttons is online.")
        await self.send_telegram_message("🤖 **عقل التداول** متصل وجاهز للعمل.\nاضغط /start لعرض لوحة التحكم.")

    def setup_telegram_handlers(self):
        """إعداد معالجات الأوامر والأزرار والرسائل."""
        self.telegram_app.add_handler(CommandHandler("start", self.tg_show_main_menu))
        self.telegram_app.add_handler(CallbackQueryHandler(self.tg_button_handler))
        self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.tg_receive_param_value))

    # --- بناء لوحات الأزرار ---
    def build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("⚙️ حالة النظام", callback_data='status')],
            [InlineKeyboardButton("📡 إدارة الماسحات", callback_data='manage_scanners')],
            [InlineKeyboardButton("📊 تقرير الأداء", callback_data='performance')],
        ]
        return InlineKeyboardMarkup(keyboard)

    def build_scanners_menu_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = []
        for name, status in self.scanners_status.items():
            icon = "🟢" if status else "🔴"
            keyboard.append([
                InlineKeyboardButton(f"{icon} {name}", callback_data=f'toggle_scanner_{name}'),
                InlineKeyboardButton("🔧 تعديل", callback_data=f'edit_params_{name}')
            ])
        keyboard.append([InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data='main_menu')])
        return InlineKeyboardMarkup(keyboard)

    def build_params_menu_keyboard(self, scanner_name: str) -> InlineKeyboardMarkup:
        keyboard = []
        params = self.scanner_params.get(scanner_name, {})
        for key, value in params.items():
            keyboard.append([InlineKeyboardButton(f"{key}: {value}", callback_data=f'set_param_{scanner_name}_{key}')])
        keyboard.append([InlineKeyboardButton("🔙 رجوع للماسحات", callback_data='manage_scanners')])
        return InlineKeyboardMarkup(keyboard)

    # --- معالجات التليجرام ---
    async def tg_show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "🤖 **أهلاً بك في لوحة تحكم عقل التداول!**\n\nاختر أحد الخيارات من القائمة أدناه:"
        keyboard = self.build_main_menu_keyboard()
        if update.callback_query:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard, parse_mode='Markdown')
        else:
            await update.message.reply_text(text=text, reply_markup=keyboard, parse_mode='Markdown')

    async def tg_button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == 'main_menu':
            await self.tg_show_main_menu(update, context)
        
        elif data == 'status':
            status_msg = "⚙️ **الحالة الحالية للنظام** ⚙️\n\n"
            for ex_id, config in self.exchanges_config.items():
                mode_map = {'auto': 'تلقائي', 'manual': 'يدوي', 'off': 'متوقف'}
                status_msg += f"- منصة **{ex_id.upper()}**: الوضع = **{mode_map.get(config['mode'])}**\n"
            await query.edit_message_text(text=status_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data='main_menu')]]), parse_mode='Markdown')

        elif data == 'performance':
            report = self.performance_tracker.get_performance_report()
            await query.edit_message_text(text=report, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data='main_menu')]]), parse_mode='Markdown')

        elif data == 'manage_scanners':
            text = "📡 **إدارة الماسحات (الاستراتيجيات)**\n\nاضغط على اسم الماسح لتفعيله أو تعطيله، أو اضغط 'تعديل' لتغيير معاييره."
            keyboard = self.build_scanners_menu_keyboard()
            await query.edit_message_text(text=text, reply_markup=keyboard)
        
        elif data.startswith('toggle_scanner_'):
            scanner_name = data.replace('toggle_scanner_', '')
            self.scanners_status[scanner_name] = not self.scanners_status[scanner_name]
            keyboard = self.build_scanners_menu_keyboard()
            await query.edit_message_reply_markup(reply_markup=keyboard)

        elif data.startswith('edit_params_'):
            scanner_name = data.replace('edit_params_', '')
            text = f"🔧 **تعديل معايير الماسح: {scanner_name}**\n\nاضغط على أي معيار لتغيير قيمته."
            keyboard = self.build_params_menu_keyboard(scanner_name)
            await query.edit_message_text(text=text, reply_markup=keyboard)

        elif data.startswith('set_param_'):
            _, scanner_name, param_key = data.split('_', 2)
            context.user_data['awaiting_param'] = {'scanner': scanner_name, 'param': param_key}
            await query.message.reply_text(f"يرجى إرسال القيمة الجديدة للمعيار `{param_key}` للماسح `{scanner_name}`:", parse_mode='Markdown')

    async def tg_receive_param_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if 'awaiting_param' in context.user_data:
            param_info = context.user_data['awaiting_param']
            scanner = param_info['scanner']
            param = param_info['param']
            new_value = update.message.text
            
            try:
                original_type = type(self.scanner_params[scanner][param])
                self.scanner_params[scanner][param] = original_type(new_value)
                await update.message.reply_text(f"✅ تم تحديث `{param}` إلى `{new_value}` بنجاح.")
                del context.user_data['awaiting_param']
                # عرض قائمة المعايير المحدثة
                text = f"🔧 **تعديل معايير الماسح: {scanner}**\n\nتم التحديث بنجاح."
                keyboard = self.build_params_menu_keyboard(scanner)
                await update.message.reply_text(text=text, reply_markup=keyboard)
            except (ValueError, KeyError) as e:
                await update.message.reply_text(f"خطأ: قيمة غير صالحة. يرجى إدخال رقم صحيح. الخطأ: {e}")
                del context.user_data['awaiting_param']

    # ... (بقية الأجزاء مثل الماسحات وحلقة العمل الرئيسية لم تتغير)
    async def close_connections(self, *args):
        for ex in self.exchanges.values(): await ex.close()
        await self.redis_pool.disconnect()
        if self.telegram_app and self.telegram_app.updater:
            await self.telegram_app.updater.stop()
            await self.telegram_app.stop()
        logging.info("All connections closed gracefully.")

    async def send_telegram_message(self, message: str):
        await self.telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')

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

    async def run_scanners(self):
        while True:
            logging.info("Starting new scan cycle...")
            for ex_id in self.exchanges.keys():
                if self.exchanges_config.get(ex_id, {}).get('mode', 'off') == 'off': continue
                for symbol in self.symbols:
                    df = await self.fetch_ohlcv(ex_id, symbol, self.timeframe)
                    if df.empty: continue
                    for name, func in self.scanners.items():
                        if self.scanners_status.get(name):
                            params = self.scanner_params.get(name, {})
                            signal = func(df.copy(), symbol, params)
                            if signal: await self.process_signal(ex_id, signal)
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
    try:
        await asyncio.gather(brain.run_scanners(), brain.listen_for_statistics())
    finally:
        await brain.close_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Brain shutting down...")


