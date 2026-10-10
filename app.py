import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")

INTERVAL_SECONDS     = 180
COOLDOWN_SECONDS     = 15 * 60
FLAT_REPORT_SECONDS  = 60 * 60

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# OKX — работает с Render без блокировок
OKX_1H = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1H&limit=500"
OKX_1D = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1D&limit=300"

# === ПАРАМЕТРЫ СТРАТЕГИИ ===
RSI_TREND_BULL = 50
RSI_ENTRY_BUY  = 35
CMF_BUY        = 0.01
EMA_PERIOD     = 200

last_signal_time = {"BUY": 0}
last_flat_report = 0
last_ema_state   = None


# === FLASK ===
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
            print("✅ Сообщение отправлено", flush=True)
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}", flush=True)


# === OKX ===
def fetch_okx(url, label):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ OKX {label} HTTP {r.status_code}", flush=True)
            return None
        data = r.json()
        if data.get("code") != "0" or "data" not in data:
            print(f"⚠️ OKX {label} error: {data.get('msg', '?')}", flush=True)
            return None
        rows = data["data"]
        if not rows:
            return None
        rows.reverse()
        opens   = [float(x[1]) for x in rows]
        highs   = [float(x[2]) for x in rows]
        lows    = [float(x[3]) for x in rows]
        closes  = [float(x[4]) for x in rows]
        volumes = [float(x[5]) for x in rows]
        print(f"✅ OKX {label}: получено {len(closes)} свечей", flush=True)
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"⚠️ OKX {label} exception: {e}", flush=True)
        return None


# === ИНДИКАТОРЫ ===
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(abs(ch) if ch < 0 else 0.0)
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))


def calc_cmf(highs, lows, closes, volumes, period=20):
    if len(closes) < period + 1:
        return 0.0
    mf_values = []
    for i in range(len(closes)):
        d = highs[i] - lows[i]
        mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / d if d else 0.0
        mf_values.append(mfm * volumes[i])
    mf_sum = sum(mf_values[-period:])
    vol_sum = sum(volumes[-period:])
    return mf_sum / vol_sum if vol_sum else 0.0


def calc_ema(closes, period=200):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
    return ema


def describe_rsi(value, tf):
    if value >= 70:   return f"RSI {tf} = {value:.1f} → ⚠️ перекупленность"
    elif value <= 30: return f"RSI {tf} = {value:.1f} → ✅ перепроданность"
    elif value > 50:  return f"RSI {tf} = {value:.1f} → 📈 бычий"
    else:             return f"RSI {tf} = {value:.1f} → 📉 медвежий"


def describe_cmf(value, tf):
    if value > 0.05:    return f"CMF {tf} = {value:+.3f} → 🐋 сильный приток"
    elif value > 0.01:  return f"CMF {tf} = {value:+.3f} → 🐋 умеренный приток"
    elif value < -0.05: return f"CMF {tf} = {value:+.3f} → 🐋 сильный отток"
    elif value < -0.01: return f"CMF {tf} = {value:+.3f} → 🐋 умеренный отток"
    else:               return f"CMF {tf} = {value:+.3f} → 🐋 нейтрально"


def check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    if ema_200 is None:
        return False, "EMA 200 не рассчитана"
    conditions = {
        "RSI 1D > 50":  rsi_1d > RSI_TREND_BULL,
        "RSI 1H < 35":  rsi_1h < RSI_ENTRY_BUY,
        "CMF 1H > 0.01": cmf_1h > CMF_BUY,
        "Цена > EMA 200": price > ema_200,
    }
    if all(conditions.values()):
        return True, "все условия"
    failed = [k for k, v in conditions.items() if not v]
    return False, f"нет: {', '.join(failed)}"


def check_ema_state(price, ema_200):
    global last_ema_state
    if ema_200 is None:
        return
    current = "above" if price > ema_200 else "below"
    if last_ema_state is None:
        last_ema_state = current
        return
    if current != last_ema_state:
        if current == "above":
            send_telegram(
                f"🚀 *ETH ПРОБИЛ EMA 200 (1D) ВВЕРХ*\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"✅ *Бычий тренд активирован.*"
            )
        else:
            send_telegram(
                f"⚠️ *ETH УПАЛ НИЖЕ EMA 200 (1D)*\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"🛑 *BUY-сигналы приостановлены.*"
            )
        last_ema_state = current


def send_flat_report(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    global last_flat_report
    now = time.time()
    if now - last_flat_report < FLAT_REPORT_SECONDS:
        return
    above_ema = "✅ выше" if ema_200 and price > ema_200 else "❌ ниже"
    ema_str = f"${ema_200:.2f}" if ema_200 else "?"
    text = (
        "💤 *ETH — сигналов нет*\n\n"
        f"💵 *Цена ETH:* ${price:.2f}\n"
        f"📊 *EMA 200 (1D):* {ema_str} ({above_ema})\n"
        f"\n━━━━━━━━━━━━━━━\n"
        f"*📊 RSI (14)*\n"
        f"• {describe_rsi(rsi_1h, '1H')}\n"
        f"• {describe_rsi(rsi_1d, '1D')}\n"
        f"\n*🐋 CMF (20)*\n"
        f"• {describe_cmf(cmf_1h, '1H')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"\n🤖 _Следующий отчёт через час._"
    )
    send_telegram(text)
    last_flat_report = now


# === ГЛАВНЫЙ ЦИКЛ ===
def main_analysis():
    print("▶️ main_analysis()", flush=True)

    data_1h = fetch_okx(OKX_1H, "1H")
    data_1d = fetch_okx(OKX_1D, "1D")
    if not data_1h or not data_1d:
        print("⏳ Нет данных", flush=True)
        return

    rsi_1h  = calc_rsi(data_1h[3])
    cmf_1h  = calc_cmf(data_1h[1], data_1h[2], data_1h[3], data_1h[4])
    rsi_1d  = calc_rsi(data_1d[3])
    ema_200 = calc_ema(data_1d[3], EMA_PERIOD)
    price   = data_1h[3][-1]

    if ema_200 is None:
        print("⚠️ EMA 200 не рассчитана", flush=True)
        return

    above_ema = "ВЫШЕ" if price > ema_200 else "НИЖЕ"
    print(
        f"[{datetime.now():%H:%M:%S}] ETH ${price:.2f} | "
        f"RSI1H {rsi_1h:.1f} | RSI1D {rsi_1d:.1f} | "
        f"CMF1H {cmf_1h:+.3f} | EMA200 ${ema_200:.2f} ({above_ema})",
        flush=True
    )

    check_ema_state(price, ema_200)

    is_signal, reason = check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200)

    if is_signal:
        now = time.time()
        if now - last_signal_time["BUY"] < COOLDOWN_SECONDS:
            print(f"⏱ BUY недавно — пропуск", flush=True)
            return

        target = price * 1.02
        text = (
            f"🟢 *РЕКОМЕНДАЦИЯ: ПОКУПАТЬ ETH*\n\n"
            f"💵 *Цена входа:* ${price:.2f}\n"
            f"🎯 *Цель (48ч):* ${target:.2f} (+2%)\n"
            f"⏱ *Горизонт:* 48 часов\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"*📊 RSI (14)*\n"
            f"• {describe_rsi(rsi_1h, '1H')}\n"
            f"• {describe_rsi(rsi_1d, '1D')}\n"
            f"\n*🐋 CMF (20)*\n"
            f"• {describe_cmf(cmf_1h, '1H')}\n"
            f"\n*📈 EMA 200 (1D):* ${ema_200:.2f} ✅\n"
            f"━━━━━━━━━━━━━━━\n"
            f"\n🎯 *ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ:*\n"
            f"• RSI 1D > 50 — старший тренд бычий\n"
            f"• RSI 1H < 35 — перепроданность\n"
            f"• CMF 1H > 0.01 — капитал заходит\n"
            f"• Цена > EMA 200 — глобальный тренд вверх\n\n"
            f"📊 _Winrate: 59.5%_"
        )
        send_telegram(text)
        last_signal_time["BUY"] = now
        return

    send_flat_report(rsi_1h, rsi_1d, cmf_1h, price, ema_200)


# === СТАРТ ===
if __name__ == "__main__":
    print("🚀 ETH-бот запущен на Render...", flush=True)
    send_telegram(
        "🎯 *Shooter: ETH-бот запущен!*\n\n"
        "🔧 *Стратегия:*\n"
        "• Монета: ETH/USDT\n"
        "• Индикаторы: RSI + CMF + EMA 200\n"
        "• Таймфреймы: 1H + 1D\n"
        "• Сигналы: только BUY\n"
        "• Горизонт: 48 часов\n\n"
        "📊 *Бэктест (3.4 года):* Winrate 59.5%, +232%\n"
        "✅ *Все годы в плюсе*"
    )
    print("🧵 Flask-поток...", flush=True)
    threading.Thread(target=run_flask, daemon=True).start()
    print("🔁 Цикл анализа...", flush=True)
    while True:
        try:
            main_analysis()
        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}", flush=True)
        time.sleep(INTERVAL_SECONDS)
