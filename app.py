import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")
INTERVAL_SECONDS = 180
COOLDOWN_SECONDS = 15 * 60

URL_TELEGRAM  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
URL_BYBIT_4H  = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=240&limit=200"
URL_BYBIT_15M = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=15&limit=200"

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

# === ДАННЫЕ BYBIT ===
def get_bybit_data(url_address):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url_address, headers=headers, timeout=12)
        if response.status_code != 200:
            print(f"⚠️ Bybit статус {response.status_code}")
            return None
        data_json = response.json()
        if data_json.get('retCode') != 0 or 'result' not in data_json:
            print(f"⚠️ Bybit ошибка: {data_json.get('retMsg', 'Неизвестно')}")
            return None
        list_data = data_json['result']['list']
        if not list_data:
            return None
        list_data.reverse()
        opens   = [float(x[1]) for x in list_data]
        highs   = [float(x[2]) for x in list_data]
        lows    = [float(x[3]) for x in list_data]
        closes  = [float(x[4]) for x in list_data]
        volumes = [float(x[5]) for x in list_data]
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"❌ Ошибка обработки Bybit: {e}")
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

# === CMF (объём Чайкина) ===
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

# === ОПИСАНИЯ ИНДИКАТОРОВ ===
def describe_rsi(value, timeframe):
    if value >= 70:
        return f"RSI {timeframe} = {value:.1f} → ⚠️ перекупленность"
    elif value <= 30:
        return f"RSI {timeframe} = {value:.1f} → ✅ перепроданность"
    elif value > 50:
        return f"RSI {timeframe} = {value:.1f} → 📈 бычий настрой"
    else:
        return f"RSI {timeframe} = {value:.1f} → 📉 медвежий настрой"

def describe_cmf(value, timeframe):
    if value > 0.05:
        return f"CMF {timeframe} = {value:+.3f} → 🐋 сильный приток"
    elif value > 0.01:
        return f"CMF {timeframe} = {value:+.3f} → 🐋 умеренный приток"
    elif value < -0.05:
        return f"CMF {timeframe} = {value:+.3f} → 🐋 сильный отток"
    elif value < -0.01:
        return f"CMF {timeframe} = {value:+.3f} → 🐋 умеренный отток"
    else:
        return f"CMF {timeframe} = {value:+.3f} → 🐋 нейтрально"

# === ЛОГИКА РЕКОМЕНДАЦИЙ ===
def build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price):
    if rsi_4h > 50 and cmf_4h > 0:
        trend = "📈 РАСТУЩИЙ (бычий)"
    elif rsi_4h < 50 and cmf_4h < 0:
        trend = "📉 ПАДАЮЩИЙ (медвежий)"
    else:
        trend = "➡️ ФЛЭТ"

    buy_condition  = rsi_4h > 48 and rsi_15m < 33 and cmf_15m > 0.01
    sell_condition = rsi_4h < 52 and rsi_15m > 67 and cmf_15m < -0.01

    block = (
        f"\n💵 *Цена BTC:* ${price:.2f}\n"
        f"📊 *Тренд 4Н:* {trend}\n"
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"*📊 RSI (14)*\n"
        f"• {describe_rsi(rsi_15m, '15М')}\n"
        f"• {describe_rsi(rsi_4h,  '4Н')}\n"
        f"\n*🐋 ОБЪЁМ ЧАЙКИНА CMF (20)*\n"
        f"• {describe_cmf(cmf_15m, '15М')}\n"
        f"• {describe_cmf(cmf_4h,  '4Н')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    if buy_condition:
        return "BUY", (
            "🟢 *РЕКОМЕНДАЦИЯ: ПОКУПАТЬ (BUY)*\n"
            + block +
            "\n🎯 *ПОЧЕМУ:*\n"
            "• RSI 4Н бычий — старший тренд вверх\n"
            "• RSI 15М < 33 — перепроданность\n"
            "• CMF 15М > +0.01 — капитал заходит"
        )

    if sell_condition:
        return "SELL", (
            "🔴 *РЕКОМЕНДАЦИЯ: ПРОДАВАТЬ (SELL)*\n"
            + block +
            "\n🎯 *ПОЧЕМУ:*\n"
            "• RSI 4Н медвежий — старший тренд вниз\n"
            "• RSI 15М > 67 — перекупленность\n"
            "• CMF 15М < -0.01 — капитал выходит"
        )

    return "WAIT", "⏸ Ждать\n" + block

# === ГЛАВНЫЙ ЦИКЛ ===
def main_analysis():
    data_4h  = get_bybit_data(URL_BYBIT_4H)
    data_15m = get_bybit_data(URL_BYBIT_15M)
    if not data_4h or not data_15m:
        print("⏳ Нет данных Bybit")
        return

    rsi_4h  = calc_rsi(data_4h[3])
    rsi_15m = calc_rsi(data_15m[3])
    cmf_4h  = calc_cmf(data_4h[1], data_4h[2], data_4h[3], data_4h[4])
    cmf_15m = calc_cmf(data_15m[1], data_15m[2], data_15m[3], data_15m[4])
    price   = data_15m[3][-1]

    decision, text = build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price)

    print(f"[{datetime.now():%H:%M:%S}] BTC ${price:.1f} | RSI15 {rsi_15m:.1f} | "
          f"CMF15 {cmf_15m:+.3f} | → {decision}")

    if decision == "WAIT":
        return

    now = time.time()
    if now - last_signal_time[decision] < COOLDOWN_SECONDS:
        print(f"⏱ Сигнал {decision} недавно — пропуск")
        return

    send_telegram(text)
    last_signal_time[decision] = now

# === СТАРТ ===
if __name__ == "__main__":
    print("🚀 Бот Торгаш запущен на Render...")
    send_telegram("🚀 *Бот Торгаш запущен в облаке Render!*\n\nРаботаю 24/7.")
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        try:
            main_analysis()
        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}")
        time.sleep(INTERVAL_SECONDS)
