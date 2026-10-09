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
FLAT_REPORT_SECONDS  = 60 * 60  # отчёт раз в час

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Binance API (Render его не блокирует, если использовать VPN на телефоне для загрузки, но на Render — прямой доступ)
# Binance доступен с Render без VPN (Render использует американские IP)
BINANCE_1H  = "https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1h&limit=500"
BINANCE_1D  = "https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1d&limit=300"

# === ПАРАМЕТРЫ СТРАТЕГИИ ===
RSI_TREND_BULL = 50      # RSI 1D должен быть выше этого
RSI_ENTRY_BUY  = 35      # RSI 1H должен быть ниже этого
CMF_BUY        = 0.01    # CMF 1H должен быть выше этого
EMA_PERIOD     = 200     # EMA на 1D

last_signal_time = {"BUY": 0}
last_flat_report = 0
last_trend_state = None
last_ema_state   = None  # "above" / "below"

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
            print("✅ Сообщение отправлено в Telegram", flush=True)
    except Exception as e:
        print(f"❌ Ошибка сети Telegram: {e}", flush=True)


# === ЗАГРУЗКА ДАННЫХ ===
def fetch_binance(url, label):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ Binance {label} HTTP {r.status_code}", flush=True)
            return None
        rows = r.json()
        if not rows:
            return None
        opens   = [float(x[1]) for x in rows]
        highs   = [float(x[2]) for x in rows]
        lows    = [float(x[3]) for x in rows]
        closes  = [float(x[4]) for x in rows]
        volumes = [float(x[5]) for x in rows]
        print(f"✅ Binance {label}: получено {len(closes)} свечей", flush=True)
        return opens, highs, lows, closes, volumes
    except Exception as e:
        print(f"⚠️ Binance {label} exception: {e}", flush=True)
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


# === ОПИСАНИЯ ===
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


# === ЛОГИКА СИГНАЛА ===
def check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    """Проверяет все условия для BUY."""
    if ema_200 is None:
        return False, "EMA 200 не рассчитана"

    conditions = {
        "RSI 1D > 50":  rsi_1d > RSI_TREND_BULL,
        "RSI 1H < 35":  rsi_1h < RSI_ENTRY_BUY,
        "CMF 1H > 0.01": cmf_1h > CMF_BUY,
        "Цена > EMA 200": price > ema_200,
    }

    if all(conditions.values()):
        return True, "все условия выполнены"

    failed = [k for k, v in conditions.items() if not v]
    return False, f"не выполнено: {', '.join(failed)}"


# === СМЕНА EMA-СОСТОЯНИЯ ===
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
                f"🚀 *ETH ПРОБИЛ EMA 200 (1D) СНИЗУ ВВЕРХ*\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"✅ *Бычий тренд активирован* — бот начнёт искать BUY-сигналы.\n"
                f"📈 _Именно в таких условиях стратегия даёт +200%+._"
            )
        else:
            send_telegram(
                f"⚠️ *ETH УПАЛ НИЖЕ EMA 200 (1D)*\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"🛑 *Бычий тренд сломан* — бот приостанавливает BUY-сигналы.\n"
                f"💤 _Жду возврата выше EMA 200._"
            )
        last_ema_state = current


# === ОТЧЁТ О ФЛЕТЕ ===
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
        f"\n🤖 _Бот работает. Жду сигнала._\n"
        f"⏱ _Следующий отчёт через час._"
    )
    send_telegram(text)
    last_flat_report = now


# === ГЛАВНЫЙ ЦИКЛ ===
def main_analysis():
    print("▶️ main_analysis() начался", flush=True)

    data_1h = fetch_binance(BINANCE_1H, "1H")
    data_1d = fetch_binance(BINANCE_1D, "1D")
    if not data_1h or not data_1d:
        print("⏳ Нет данных", flush=True)
        return

    rsi_1h  = calc_rsi(data_1h[3])
    cmf_1h  = calc_cmf(data_1h[1], data_1h[2], data_1h[3], data_1h[4])
    rsi_1d  = calc_rsi(data_1d[3])
    ema_200 = calc_ema(data_1d[3], EMA_PERIOD)
    price   = data_1h[3][-1]

    if ema_200 is None:
        print("⚠️ EMA 200 не рассчитана (мало данных)", flush=True)
        return

    above_ema = "ВЫШЕ" if price > ema_200 else "НИЖЕ"

    print(
        f"[{datetime.now():%H:%M:%S}] ETH ${price:.2f} | "
        f"RSI1H {rsi_1h:.1f} | RSI1D {rsi_1d:.1f} | "
        f"CMF1H {cmf_1h:+.3f} | EMA200 ${ema_200:.2f} ({above_ema})",
        flush=True
    )

    # Проверка смены тренда по EMA 200
    check_ema_state(price, ema_200)

    # Проверка сигнала
    is_signal, reason = check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200)

    if is_signal:
        now = time.time()
        if now - last_signal_time["BUY"] < COOLDOWN_SECONDS:
            print(f"⏱ BUY недавно — пропуск", flush=True)
            return

        target = price * 1.02  # цель +2% за 48ч
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
            f"\n*📈 EMA 200 (1D):* ${ema_200:.2f} ✅ цена выше\n"
            f"━━━━━━━━━━━━━━━\n"
            f"\n🎯 *ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ:*\n"
            f"• RSI 1D > 50 — старший тренд бычий\n"
            f"• RSI 1H < 35 — краткосрочная перепроданность\n"
            f"• CMF 1H > 0.01 — капитал заходит\n"
            f"• Цена > EMA 200 — глобальный тренд вверх\n\n"
            f"📊 _Winrate стратегии: 59.5%_"
        )
        send_telegram(text)
        last_signal_time["BUY"] = now
        return

    # Отчёт о флете (раз в час)
    send_flat_report(rsi_1h, rsi_1d, cmf_1h, price, ema_200)


# === СТАРТ ===
if __name__ == "__main__":
    print("🚀 ETH-бот запущен на Render...", flush=True)
    send_telegram(
        "🚀 *ETH-бот запущен!*\n\n"
        "🔧 *Стратегия:*\n"
        "• Монета: ETH/USDT\n"
        "• Индикаторы: RSI + CMF + EMA 200\n"
        "• Таймфреймы: 1H + 1D\n"
        "• Сигналы: только BUY\n"
        "• Горизонт: 48 часов\n\n"
        "📊 *Результаты бэктеста (3.4 года):*\n"
        "• Winrate: 59.5%\n"
        "• Net прибыль: +232%\n"
        "• Все годы в плюсе ✅\n\n"
        "🎯 *Когда бот даёт сигнал:*\n"
        "• ETH выше EMA 200 (1D) — бычий тренд\n"
        "• RSI 1D > 50 — старший тренд вверх\n"
        "• RSI 1H < 35 — краткосрочная перепроданность\n"
        "• CMF 1H > 0.01 — приток капитала\n\n"
        "💡 _Бот автоматически приостановит сигналы, если ETH упадёт ниже EMA 200._"
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
