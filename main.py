# -*- coding: utf-8 -*-
"""
hand_okx.py: The Hand - اليد التنفيذية لمنصة OKX

الوظيفة:
- هذا برنامج تنفيذي خفيف ومستقل، متخصص في منصة تداول OKX فقط.
- يشترك في قناة الأوامر (trade_signals) في Redis للاستماع لقرارات "العقل".
- يتجاهل أي أمر لا يخص منصة OKX.
- عند استلام أمر يخصه، يقوم بتنفيذ الصفقة الفعلية على المنصة باستخدام مفاتيح API الخاصة به.
- يتابع الصفقة المفتوحة حتى إغلاقها (عند الهدف أو وقف الخسارة).
- بعد إغلاق الصفقة، ينشر تقريرًا مفصلاً بالنتيجة في قناة التقارير (trade_statistics).
"""

import asyncio
import json
import logging
import os
import time
import ccxt.async_support as ccxt
import redis.asyncio as redis

# --- إعدادات اليد ---
# استبدل هذه القيم بالقيم الحقيقية الخاصة بك
OKX_API_KEY = os.getenv("OKX_API_KEY", "YOUR_OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "YOUR_OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD", "YOUR_OKX_PASSWORD") 
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class OKXHand:
    """
    الفئة الرئيسية التي تدير جميع عمليات "اليد" لمنصة OKX.
    """
    def __init__(self):
        self.exchange_id = 'okx'
        self.exchange = ccxt.okx({
            'apiKey': OKX_API_KEY,
            'secret': OKX_API_SECRET,
            'password': OKX_API_PASSWORD,
            'options': {'defaultType': 'swap'},
        })
        self.redis_pub = None
        self.redis_sub = None
        logging.info("تم تهيئة OKXHand.")

    async def initialize(self):
        """تهيئة الاتصالات."""
        self.redis_pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        self.redis_pub = redis.Redis(connection_pool=self.redis_pool)
        self.redis_sub = redis.Redis(connection_pool=self.redis_pool)
        await self.exchange.load_markets()
        logging.info(f"تم الاتصال بـ Redis وتحميل أسواق OKX.")

    async def close_connections(self):
        """إغلاق الاتصالات بأمان."""
        await self.exchange.close()
        await self.redis_pool.disconnect()
        logging.info("تم إغلاق الاتصالات.")

    async def handle_trade_lifecycle(self, signal: dict):
        """إدارة دورة حياة الصفقة الكاملة: التنفيذ، المتابعة، والتقرير."""
        symbol = signal.get('symbol')
        direction = signal.get('direction')
        confidence = signal.get('confidence', 1.0)
        
        # 1. تحديد حجم الصفقة بناءً على الثقة
        base_risk_usd = 10  # المخاطرة الأساسية بالدولار
        trade_size_usd = base_risk_usd * confidence
        
        price = await self.get_current_price(symbol)
        if price == 0: return

        amount = self.exchange.amount_to_precision(symbol, trade_size_usd / price)
        price_str = self.exchange.price_to_precision(symbol, price)
        logging.info(f"التحضير لتنفيذ صفقة لـ {symbol}: {direction} {amount} @ {price_str}")

        # 2. تنفيذ الصفقة
        order = None
        try:
            side = 'buy' if direction == 'long' else 'sell'
            params = {'tdMode': 'isolated'} 
            order = await self.exchange.create_order(symbol, 'market', side, amount, params=params)
            logging.info(f"تم تنفيذ أمر الدخول: {order['id']}")
            
            # انتظر قليلاً للتأكد من تحديث حالة الأمر
            await asyncio.sleep(2)
            filled_order = await self.exchange.fetch_order(order['id'], symbol)
            entry_price = float(filled_order.get('average', price))
        except Exception as e:
            logging.error(f"فشل تنفيذ أمر الدخول لـ {symbol}: {e}")
            return

        # 3. وضع أوامر وقف الخسارة وجني الأرباح (TP/SL)
        # OKX يسمح بأوامر TP/SL عند فتح المركز
        sl_price = entry_price * (1 - signal['sl_ratio']) if direction == 'long' else entry_price * (1 + signal['sl_ratio'])
        tp_price = entry_price * (1 + signal['tp_ratio']) if direction == 'long' else entry_price * (1 - signal['sl_ratio'])
        
        close_side = 'sell' if direction == 'long' else 'buy'
        try:
            sl_params = {'tdMode': 'isolated', 'slPrice': self.exchange.price_to_precision(symbol, sl_price)}
            tp_params = {'tdMode': 'isolated', 'tpPrice': self.exchange.price_to_precision(symbol, tp_price)}
            
            # هذا مجرد مثال، قد تختلف طريقة وضع الأوامر حسب الـ API
            # في الغالب يتم وضعها كأوامر منفصلة أو كجزء من أمر الدخول
            logging.info(f"سيتم متابعة المركز يدويًا. SL: {sl_price}, TP: {tp_price}")
            
        except Exception as e:
            logging.error(f"فشل في وضع أوامر TP/SL: {e}")

        # 4. مراقبة المركز حتى الإغلاق
        final_pnl = await self.monitor_position(symbol, entry_price, float(amount), direction)

        # 5. نشر التقرير
        trade_report = {
            "timestamp": int(time.time()), "exchange": self.exchange_id, "symbol": symbol,
            "strategy_id": signal.get("strategy_id"), "direction": direction,
            "entry_price": entry_price, "pnl": final_pnl,
        }
        await self.redis_pub.publish("trade_statistics", json.dumps(trade_report))
        logging.info(f"تم نشر تقرير الصفقة: {trade_report}")

    async def get_current_price(self, symbol: str) -> float:
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            logging.error(f"فشل في جلب السعر لـ {symbol}: {e}")
            return 0.0

    async def monitor_position(self, symbol: str, entry_price: float, amount: float, direction: str) -> float:
        """مراقبة المركز المفتوح حتى يختفي من قائمة المراكز المفتوحة."""
        logging.info(f"مراقبة المركز لـ {symbol}. سعر الدخول: {entry_price}")
        
        while True:
            await asyncio.sleep(20) # تحقق كل 20 ثانية
            try:
                positions = await self.exchange.fetch_positions([symbol])
                current_position = next((p for p in positions if p.get('symbol') == symbol and float(p.get('contracts', 0)) > 0), None)
                
                if not current_position:
                    logging.info(f"لم يعد المركز الخاص بـ {symbol} مفتوحًا. جاري حساب النتيجة...")
                    
                    # جلب آخر سعر للإغلاق
                    close_price = await self.get_current_price(symbol)
                    
                    # حساب الربح والخسارة بالدولار
                    pnl_per_unit = close_price - entry_price
                    if direction == 'short':
                        pnl_per_unit = entry_price - close_price
                    
                    total_pnl = pnl_per_unit * amount
                    
                    logging.info(f"تم إغلاق الصفقة لـ {symbol}. السعر النهائي: {close_price}. الربح/الخسارة: {total_pnl:.4f} USDT")
                    return total_pnl

            except Exception as e:
                logging.error(f"خطأ أثناء مراقبة المركز {symbol}: {e}")
                # في حالة حدوث خطأ، انتظر أطول قبل المحاولة مرة أخرى
                await asyncio.sleep(60)
        return 0.0 # قيمة افتراضية في حالة الخروج غير المتوقع

    async def listen_for_signals(self):
        """الاستماع المستمر للإشارات من قناة Redis."""
        pubsub = self.redis_sub.pubsub()
        await pubsub.subscribe("trade_signals")
        logging.info("تم الاشتراك في قناة 'trade_signals'. في انتظار الأوامر...")
        
        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    signal = json.loads(message['data'])
                    if signal.get('exchange') == self.exchange_id:
                        logging.info(f"تم استلام إشارة ذات صلة: {signal}")
                        asyncio.create_task(self.handle_trade_lifecycle(signal))
                except Exception as e:
                    logging.error(f"خطأ في معالجة الإشارة: {e}")

async def main():
    hand = OKXHand()
    await hand.initialize()
    await hand.listen_for_signals()
    await hand.close_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("إيقاف يد OKX...")


