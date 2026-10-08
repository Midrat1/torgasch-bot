import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")
INTERVAL_SECONDS = 180
COOLDOWN_SECONDS = 15 * 60

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Bybit API v5
URL_BYBIT_4H  = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=240&limit=200"
URL_BYBIT_15M = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=15&limit=200"

# Binance API (fallback)
URL_BINANCE_4H  = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=200"
URL_BINANCE_15M = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=200"

last_signal_time = {"BUY": 0, "SELL": 0}

# === FLASK для Render ===
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# === TELEGRAM ===
def send_telegram(text_message):
    payload = {"chat_id": CHAT_ID, "text": text_message, "parse_mode": "Markdown"}
    try:
        r = requests.post(URL_TELEGRAM, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"❌ Telegram: {r.text}")
        else:
            print("✅ Сообщение отправлено в Telegram")
    except Exception as e:
        print(f"❌ Ошибка сети Telegram: {e}")

# === ПОЛУЧЕНИЕ ДАННЫХ: СНАЧАЛА BYBIT, ПОТОМ BINANCE ===
def get_data_from_bybit(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=6)
        if response.status_code != 200:
            print(f"⚠️ Bybit HTTP {response.status_code}")
            return None
        data = response.json()
        if data.get('retCode') != 0 or 'result' not in data:
            print(f"⚠️ Bybit error: {data.get('retMsg', '?')}")
            return None
        rows = data['result']['list']
        if not rows:
            return None
        rows.reverse()
        opens   = [float(x[1]) for x in rows]
        highs   = [float(x[2]) for x in rows]
        lows    = [float(x[3]) for x in rows]
        closes  = [float(x[4]) for x in rows]
        volumes = [float(x[5]) for x in rows]
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"⚠️ Bybit exception: {e}")
        return None


def get_data_from_binance(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Binance HTTP {response.status_code}")
            return None
        rows = response.json()
        if not rows:
            return None
        # Binance: [openTime, open, high, low, close, volume, closeTime, ...]
        opens   = [float(x[1]) for x in rows]
        highs   = [float(x[2]) for x in rows]
        lows    = [float(x[3]) for x in rows]
        closes  = [float(x[4]) for x in rows]
        volumes = [float(x[5]) for x in rows]
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"⚠️ Binance exception: {e}")
        return None


def get_market_data(url_bybit, url_binance, tf_label):
    """Пытается Bybit, если не вышло — Binance."""
    print(f"🔄 Запрос данных {tf_label} с Bybit...")
    data = get_data_from_bybit(url_bybit)
    if data:
        print(f"✅ {tf_label}: получены данные с Bybit")
        return data

    print(f"🔄 Bybit недоступен, пробую Binance...")
    data = get_data_from_binance(url_binance)
    if data:
        print(f"✅ {tf_label}: получены данные с Binance")
        return data

    print(f"❌ {tf_label}: ни Bybit, ни Binance не ответили")
    return None

# === RSI ===
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(abs(change) if change < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# === CMF ===
def calc_cmf(highs, lows, closes, volumes, period=20):
    if len(closes) < period + 1:
        return 0.0
    mf_values = []
    for i in range(len(closes)):
        denom = highs[i] - lows[i]
        mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / denom if denom != 0 else 0.0
        mf_values.append(mfm * volumes[i])
    mf_sum  = sum(mf_values[-period:])
    vol_sum = sum(volumes[-period:])
    return mf_sum / vol_sum if vol_sum != 0 else 0.0

# === ОПИСАНИЯ ===
def describe_rsi(value, tf):
    if value >= 70: return f"RSI {tf} = {value:.1f} → ⚠️ перекупленность"
    elif value <= 30: return f"RSI {tf} = {value:.1f} → ✅ перепроданность"
    elif value > 50: return f"RSI {tf} = {value:.1f} → 📈 бычий"
    else: return f"RSI {tf} = {value:.1f} → 📉 медвежий"

def describe_cmf(value, tf):
    if value > 0.05: return f"CMF {tf} = {value:+.3f} → 🐋 сильный приток"
    elif value > 0.01: return f"CMF {tf} = {value:+.3f} → 🐋 умеренный приток"
    elif value < -0.05: return f"CMF {tf} = {value:+.3f} → 🐋 сильный отток"
    elif value < -0.01: return f"CMF {tf} = {value:+.3f} → 🐋 умеренный отток"
    else: return f"CMF {tf} = {value:+.3f} → 🐋 нейтрально"

# === ЛОГИКА ===
def build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price):
    if rsi_4h > 50 and cmf_4h > 0:
        trend = "📈 РАСТУЩИЙ"
    elif rsi_4h < 50 and cmf_4h < 0:
        trend = "📉 ПАДАЮЩИЙ"
    else:
        trend = "➡️ ФЛЭТ"

    buy_condition  = rsi_4h > 48 and rsi_15m < 33 and cmf_15m > 0.01
    sell_condition = rsi_4h < 52 and rsi_15m > 67 and cmf_15m < -0.01

    block = (
        f"\n💵 *Цена BTC:* ${price:.2f}\n"
        f"📊 *Тренд 4Н:* {trend}\n"
        f"\n━━━━━━━━━━━━━━━\n"
        f"*📊 RSI (14)*\n"
        f"• {describe_rsi(rsi_15m, '15М')}\n"
        f"• {describe_rsi(rsi_4h,  '4Н')}\n"
        f"\n*🐋 ОБЪЁМ ЧАЙКИНА CMF (20)*\n"
        f"• {describe_cmf(cmf_15m, '15М')}\n"
        f"• {describe_cmf(cmf_4h,  '4Н')}\n"
        f"━━━━━━━━━━━━━━━\n"
    )

    if buy_condition:
        return "BUY", ("🟢 *РЕКОМЕНДАЦИЯ: ПОКУПАТЬ (BUY)*\n" + block +
                       "\n🎯 *RSI 4Н бычий + RSI 15М перепродан + CMF заходит*")

    if sell_condition:
        return "SELL", ("🔴 *РЕКОМЕНДАЦИЯ: ПРОДАВАТЬ (SELL)*\n" + block +
                        "\n🎯 *RSI 4Н медвежий + RSI 15М перекуплен + CMF выходит*")

    return "WAIT", "⏸ Ждать\n" + block

# === ГЛАВНЫЙ ЦИКЛ ===
def main_analysis():
    data_4h  = get_market_data(URL_BYBIT_4H,  URL_BINANCE_4H,  "4H")
    data_15m = get_market_data(URL_BYBIT_15M, URL_BINANCE_15M, "15M")

    if not data_4h or not data_15m:
        print("⏳ Нет данных ни с Bybit, ни с Binance")
        return

    rsi_4h  = calc_rsi(data_4h[3])
    rsi_15m = calc_rsi(data_15m[3])
    cmf_4h  = calc_cmf(data_4h[1], data_4h[2], data_4h[3], data_4h[4])
    cmf_15m = calc_cmf(data_15m[1], data_15m[2], data_15m[3], data_15m[4])
    price   = data_15m[3][-1]

    decision, text = build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price)

    print(f"[{datetime.now():%H:%M:%S}] BTC ${price:.1f} | "
          f"RSI15 {rsi_15m:.1f} | RSI4H {rsi_4h:.1f} | "
          f"CMF15 {cmf_15m:+.3f} | → {decision}")

    if decision == "WAIT":
        return

    now = time.time()
    if now - last_signal_time[decision] < COOLDOWN_SECONDS:
        print(f"⏱ {decision} недавно — пропуск")
        return

    send_telegram(text)
    last_signal_time[decision] = now

# === СТАРТ ===
if __name__ == "__main__":
    print("🚀 Бот Торгаш запущен на Render...")
    send_telegram("🚀 *Бот Торгаш запущен в облаке Render!*\n\n"
                  "Работаю 24/7. Источники данных: Bybit + Binance (резерв).")
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        try:
            main_analysis()
        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}")
        time.sleep(INTERVAL_SECONDS)
