import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")

INTERVAL_SECONDS     = 180      # цикл анализа: каждые 3 минуты
COOLDOWN_SECONDS     = 15 * 60  # антиспам для BUY/SELL: 15 минут
FLAT_REPORT_SECONDS  = 30 * 60  # отчёт о флете: раз в 30 минут

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

URL_OKX_4H  = "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=4H&limit=200"
URL_OKX_15M = "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=15m&limit=200"

# Храним время последних отправок и предыдущее состояние тренда
last_signal_time  = {"BUY": 0, "SELL": 0}
last_flat_report  = 0
last_trend_state  = None   # "BULL" / "BEAR" / "FLAT"

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
            print(f"❌ Telegram: {r.text}", flush=True)
        else:
            print("✅ Сообщение отправлено в Telegram", flush=True)
    except Exception as e:
        print(f"❌ Ошибка сети Telegram: {e}", flush=True)

# === OKX ===
def get_data_from_okx(url, tf_label):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"⚠️ OKX {tf_label} HTTP {response.status_code}", flush=True)
            return None
        data = response.json()
        if data.get("code") != "0" or "data" not in data:
            print(f"⚠️ OKX {tf_label} error: {data.get('msg', '?')}", flush=True)
            return None
        rows = data["data"]
        if not rows:
            print(f"⚠️ OKX {tf_label}: пустой список", flush=True)
            return None
        rows.reverse()
        opens   = [float(x[1]) for x in rows]
        highs   = [float(x[2]) for x in rows]
        lows    = [float(x[3]) for x in rows]
        closes  = [float(x[4]) for x in rows]
        volumes = [float(x[5]) for x in rows]
        print(f"✅ OKX {tf_label}: получено {len(closes)} свечей", flush=True)
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"⚠️ OKX {tf_label} exception: {e}", flush=True)
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
    if value >= 70:  return f"RSI {tf} = {value:.1f} → ⚠️ перекупленность"
    elif value <= 30: return f"RSI {tf} = {value:.1f} → ✅ перепроданность"
    elif value > 50:  return f"RSI {tf} = {value:.1f} → 📈 бычий"
    else:            return f"RSI {tf} = {value:.1f} → 📉 медвежий"

def describe_cmf(value, tf):
    if value > 0.05:   return f"CMF {tf} = {value:+.3f} → 🐋 сильный приток"
    elif value > 0.01: return f"CMF {tf} = {value:+.3f} → 🐋 умеренный приток"
    elif value < -0.05: return f"CMF {tf} = {value:+.3f} → 🐋 сильный отток"
    elif value < -0.01: return f"CMF {tf} = {value:+.3f} → 🐋 умеренный отток"
    else:              return f"CMF {tf} = {value:+.3f} → 🐋 нейтрально"

# === ОПРЕДЕЛЕНИЕ РЫНОЧНОГО СОСТОЯНИЯ ===
def detect_market_state(rsi_4h, cmf_4h):
    """Определяет тренд/флет по старшему таймфрейму."""
    if rsi_4h > 55 and cmf_4h > 0.01:
        return "BULL", "📈 Бычий тренд"
    if rsi_4h < 45 and cmf_4h < -0.01:
        return "BEAR", "📉 Медвежий тренд"
    return "FLAT", "➡️ Флет / боковик"

# === ЛОГИКА СИГНАЛОВ ===
def build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price):
    state_code, state_text = detect_market_state(rsi_4h, cmf_4h)

    buy_condition  = rsi_4h > 48 and rsi_15m < 33 and cmf_15m > 0.01
    sell_condition = rsi_4h < 52 and rsi_15m > 67 and cmf_15m < -0.01

    block = (
        f"\n💵 *Цена BTC:* ${price:.2f}\n"
        f"📊 *Состояние 4Н:* {state_text}\n"
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
        return "BUY", state_code, (
            "🟢 *РЕКОМЕНДАЦИЯ: ПОКУПАТЬ (BUY)*\n" + block +
            "\n🎯 *RSI 4Н бычий + RSI 15М перепродан + CMF заходит*"
        )

    if sell_condition:
        return "SELL", state_code, (
            "🔴 *РЕКОМЕНДАЦИЯ: ПРОДАВАТЬ (SELL)*\n" + block +
            "\n🎯 *RSI 4Н медвежий + RSI 15М перекуплен + CMF выходит*"
        )

    return "WAIT", state_code, "⏸ Ждать\n" + block

# === ОТЧЁТ О ФЛЕТЕ ===
def send_flat_report(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price):
    global last_flat_report
    now = time.time()
    if now - last_flat_report < FLAT_REPORT_SECONDS:
        return

    _, state_text = detect_market_state(rsi_4h, cmf_4h)

    text = (
        "💤 *Рынок во флете — сигналов нет*\n\n"
        f"💵 *Цена BTC:* ${price:.2f}\n"
        f"📊 *Состояние 4Н:* {state_text}\n"
        f"\n━━━━━━━━━━━━━━━\n"
        f"*📊 RSI (14)*\n"
        f"• {describe_rsi(rsi_15m, '15М')}\n"
        f"• {describe_rsi(rsi_4h,  '4Н')}\n"
        f"\n*🐋 ОБЪЁМ ЧАЙКИНА CMF (20)*\n"
        f"• {describe_cmf(cmf_15m, '15М')}\n"
        f"• {describe_cmf(cmf_4h,  '4Н')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"\n🤖 *Бот работает, жду чёткого сигнала.*\n"
        f"⏱ _Следующий отчёт через 30 минут._"
    )
    send_telegram(text)
    last_flat_report = now

# === УВЕДОМЛЕНИЕ О СМЕНЕ ТРЕНДА ===
def check_trend_change(state_code):
    global last_trend_state
    if last_trend_state is None:
        last_trend_state = state_code
        return

    if state_code != last_trend_state:
        if state_code == "BULL":
            send_telegram("🔄 *СМЕНА ТРЕНДА:* медвежий ➡️ *бычий*\n\n"
                          "Старший ТФ (4Н) разворачивается вверх. Ждём откат для BUY.")
        elif state_code == "BEAR":
            send_telegram("🔄 *СМЕНА ТРЕНДА:* бычий ➡️ *медвежий*\n\n"
                          "Старший ТФ (4Н) разворачивается вниз. Ждём отскок для SELL.")
        elif state_code == "FLAT":
            send_telegram("🔄 *СМЕНА ТРЕНДА:* рынок ушёл в *флет*\n\n"
                          "Чёткого направления нет. Ждём выхода из боковика.")
        last_trend_state = state_code

# === ГЛАВНЫЙ ЦИКЛ ===
def main_analysis():
    print("▶️ main_analysis() начался", flush=True)

    data_4h  = get_data_from_okx(URL_OKX_4H,  "4H")
    data_15m = get_data_from_okx(URL_OKX_15M, "15M")

    if not data_4h or not data_15m:
        print("⏳ Нет данных с OKX", flush=True)
        return

    rsi_4h  = calc_rsi(data_4h[3])
    rsi_15m = calc_rsi(data_15m[3])
    cmf_4h  = calc_cmf(data_4h[1], data_4h[2], data_4h[3], data_4h[4])
    cmf_15m = calc_cmf(data_15m[1], data_15m[2], data_15m[3], data_15m[4])
    price   = data_15m[3][-1]

    decision, state_code, text = build_recommendation(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price)

    print(
        f"[{datetime.now():%H:%M:%S}] BTC ${price:.1f} | "
        f"RSI15 {rsi_15m:.1f} | RSI4H {rsi_4h:.1f} | "
        f"CMF15 {cmf_15m:+.3f} | CMF4H {cmf_4h:+.3f} | "
        f"{state_code} | → {decision}",
        flush=True
    )

    # 1. Проверяем смену тренда
    check_trend_change(state_code)

    # 2. Сигналы BUY/SELL (с антиспамом)
    if decision in ("BUY", "SELL"):
        now = time.time()
        if now - last_signal_time[decision] < COOLDOWN_SECONDS:
            print(f"⏱ {decision} недавно — пропуск", flush=True)
        else:
            send_telegram(text)
            last_signal_time[decision] = now
        return

    # 3. Если WAIT и состояние FLAT — шлём отчёт о флете раз в 30 минут
    if decision == "WAIT" and state_code == "FLAT":
        send_flat_report(rsi_4h, cmf_4h, rsi_15m, cmf_15m, price)

# === СТАРТ ===
if __name__ == "__main__":
    print("🚀 Бот Торгаш запущен на Render...", flush=True)
    send_telegram(
        "🚀 *Бот Торгаш запущен в облаке Render!*\n\n"
        "🔧 *Работаю по RSI + CMF на 15М и 4Н.*\n\n"
        "📩 *Что буду присылать:*\n"
        "• 🟢 BUY / 🔴 SELL — когда оба индикатора совпадут\n"
        "• 💤 Отчёт о флете — раз в 30 минут, если рынок стоит\n"
        "• 🔄 Смену тренда — когда старший ТФ разворачивается\n\n"
        "Проверяю рынок каждые 3 минуты. Работаю 24/7."
    )

    print("🧵 Запускаю Flask-поток...", flush=True)
    threading.Thread(target=run_flask, daemon=True).start()

    print("🔁 Вхожу в бесконечный цикл анализа...", flush=True)
    while True:
        try:
            main_analysis()
        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}", flush=True)
        time.sleep(INTERVAL_SECONDS)
