import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "-1001182337455")
CHANNEL_LINK = "t.me/shooter_eth_signals"
BOT_LINK = "@Torkasch_bot"

INTERVAL_SECONDS        = 180
COOLDOWN_SECONDS        = 15 * 60
FLAT_REPORT_SECONDS     = 4 * 60 * 60
CHANNEL_POST_SECONDS    = 4 * 60 * 60
WEEKLY_REPORT_SECONDS   = 7 * 24 * 60 * 60

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

OKX_1H = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1H&limit=500"
OKX_1D = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1D&limit=300"

RSI_TREND_BULL = 50
RSI_ENTRY_BUY  = 35
CMF_BUY        = 0.01
EMA_PERIOD     = 200

last_signal_time     = {"BUY": 0}
last_flat_report     = 0
last_channel_post    = 0
last_weekly_report   = 0
last_ema_state       = None

active_trades = []
closed_stats = {
    "total": 0, "wins": 0, "losses": 0,
    "total_pnl": 0.0, "history": []
}


app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# === TELEGRAM (HTML вместо Markdown) ===
def send_telegram(text, target=None):
    chat = target or CHAT_ID
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(URL_TELEGRAM, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"❌ Telegram ({chat}): {r.text[:200]}", flush=True)
        else:
            print(f"✅ Отправлено в {chat}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}", flush=True)
        return False


def send_to_channel(text):
    return send_telegram(text, target=CHANNEL_ID)


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
            print(f"⚠️ OKX {label} error", flush=True)
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


def describe_cmf(value):
    if value > 0.05:    return f"CMF 1H = {value:+.3f} → 🐋 сильный приток"
    elif value > 0.01:  return f"CMF 1H = {value:+.3f} → 🐋 умеренный приток"
    elif value < -0.05: return f"CMF 1H = {value:+.3f} → 🐋 сильный отток"
    elif value < -0.01: return f"CMF 1H = {value:+.3f} → 🐋 умеренный отток"
    else:               return f"CMF 1H = {value:+.3f} → 🐋 нейтрально"


def check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    if ema_200 is None:
        return False, "EMA 200 не рассчитана"
    conditions = {
        "RSI 1D > 50":    rsi_1d > RSI_TREND_BULL,
        "RSI 1H < 35":    rsi_1h < RSI_ENTRY_BUY,
        "CMF 1H > 0.01":  cmf_1h > CMF_BUY,
        "Цена > EMA 200": price > ema_200,
    }
    if all(conditions.values()):
        return True, "все условия"
    failed = [k for k, v in conditions.items() if not v]
    return False, f"нет: {', '.join(failed)}"


def check_active_trades(current_price):
    global active_trades
    now = time.time()
    still_open = []

    for trade in active_trades:
        elapsed_hours = (now - trade["time"]) / 3600

        if elapsed_hours >= 48:
            entry = trade["entry"]
            pnl_pct = (current_price - entry) / entry * 100
            pnl_net = pnl_pct - 0.2

            closed_stats["total"] += 1
            closed_stats["total_pnl"] += pnl_net
            if pnl_net > 0:
                closed_stats["wins"] += 1
                emoji, label = "✅", "В ПЛЮС"
            else:
                closed_stats["losses"] += 1
                emoji, label = "❌", "В МИНУС"

            closed_stats["history"].append({
                "entry": entry, "exit": current_price,
                "pnl": pnl_net, "time": trade["time"]
            })

            total = closed_stats["total"]
            winrate = closed_stats["wins"] / total * 100 if total else 0

            entry_time = datetime.fromtimestamp(trade["time"]).strftime("%d.%m %H:%M")
            exit_time = datetime.now().strftime("%d.%m %H:%M")

            text = (
                f"{emoji} <b>СДЕЛКА ЗАКРЫТА {label}</b>\n\n"
                f"🟢 BUY ETH\n"
                f"📅 Открыта: {entry_time}\n"
                f"📅 Закрыта: {exit_time}\n\n"
                f"💵 Вход: ${entry:.2f}\n"
                f"💵 Выход: ${current_price:.2f}\n"
                f"📊 P&L: {pnl_net:+.2f}%\n"
                f"⏱ Удержание: 48ч\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📊 <b>ОБЩАЯ СТАТИСТИКА:</b>\n"
                f"• Всего: {total}\n"
                f"• Прибыльных: {closed_stats['wins']}\n"
                f"• Убыточных: {closed_stats['losses']}\n"
                f"• Winrate: {winrate:.1f}%\n"
                f"• Общий P&L: {closed_stats['total_pnl']:+.2f}%\n\n"
                f"📢 Канал: {CHANNEL_LINK}"
            )
            send_telegram(text)

            channel_text = (
                f"{emoji} <b>Сделка закрыта {label}</b>\n\n"
                f"🟢 BUY ETH: ${entry:.2f} → ${current_price:.2f}\n"
                f"📊 P&L: {pnl_net:+.2f}%\n\n"
                f"📈 Всего сделок: {total} | Winrate: {winrate:.1f}%\n\n"
                f"🤖 Личные сигналы: {BOT_LINK}"
            )
            send_to_channel(channel_text)
        else:
            still_open.append(trade)

    active_trades = still_open


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
            msg = (
                f"🚀 <b>ETH ПРОБИЛ EMA 200 (1D) ВВЕРХ</b>\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"✅ <b>Бычий тренд активирован.</b>\n\n"
                f"🤖 Бот: {BOT_LINK}"
            )
        else:
            msg = (
                f"⚠️ <b>ETH УПАЛ НИЖЕ EMA 200 (1D)</b>\n\n"
                f"💵 Цена: ${price:.2f}\n"
                f"📊 EMA 200: ${ema_200:.2f}\n\n"
                f"🛑 <b>BUY-сигналы приостановлены.</b>\n\n"
                f"🤖 Бот: {BOT_LINK}"
            )
        send_telegram(msg)
        send_to_channel(msg)
        last_ema_state = current


def send_flat_report(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    global last_flat_report
    now = time.time()
    if now - last_flat_report < FLAT_REPORT_SECONDS:
        return
    above_ema = "✅ выше" if ema_200 and price > ema_200 else "❌ ниже"
    ema_str = f"${ema_200:.2f}" if ema_200 else "?"
    text = (
        "💤 <b>ETH — сигналов нет</b>\n\n"
        f"💵 <b>Цена ETH:</b> ${price:.2f}\n"
        f"📊 <b>EMA 200 (1D):</b> {ema_str} ({above_ema})\n"
        f"\n━━━━━━━━━━━━━━━\n"
        f"<b>📊 RSI (14)</b>\n"
        f"• {describe_rsi(rsi_1h, '1H')}\n"
        f"• {describe_rsi(rsi_1d, '1D')}\n"
        f"\n<b>🐋 CMF (20)</b>\n"
        f"• {describe_cmf(cmf_1h)}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"\n📢 Канал: {CHANNEL_LINK}"
    )
    send_telegram(text)
    last_flat_report = now


def send_channel_update(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    global last_channel_post
    now = time.time()
    if now - last_channel_post < CHANNEL_POST_SECONDS:
        return

    above = "✅" if ema_200 and price > ema_200 else "❌"
    ema_str = f"${ema_200:.2f}" if ema_200 else "?"

    if rsi_1d > 55 and cmf_1h > 0.01:
        mood = "🟢 Бычий настрой"
    elif rsi_1d < 45 and cmf_1h < -0.01:
        mood = "🔴 Медвежий настрой"
    else:
        mood = "⚪️ Нейтральный"

    text = (
        f"📊 <b>ETH — обзор рынка</b>\n"
        f"{datetime.now().strftime('%d.%m %H:%M')}\n\n"
        f"💵 <b>Цена:</b> ${price:.2f}\n"
        f"📊 <b>EMA 200 (1D):</b> {ema_str} {above}\n\n"
        f"<b>📈 RSI (14):</b>\n"
        f"• 1H: {rsi_1h:.1f}\n"
        f"• 1D: {rsi_1d:.1f}\n\n"
        f"<b>🐋 CMF (20) 1H:</b> {cmf_1h:+.3f}\n\n"
        f"<b>Настроение:</b> {mood}\n\n"
        f"🎯 Сигналов нет. Бот ждёт условий.\n\n"
        f"🤖 Личные сигналы: {BOT_LINK}"
    )
    send_to_channel(text)
    last_channel_post = now


def send_weekly_report():
    global last_weekly_report
    now = time.time()
    if now - last_weekly_report < WEEKLY_REPORT_SECONDS:
        return

    total = closed_stats["total"]
    if total == 0:
        text = (
            "📊 <b>Недельная сводка</b>\n\n"
            "За неделю сигналов не было.\n"
            "Бот продолжает ждать условий.\n\n"
            f"🤖 Бот: {BOT_LINK}"
        )
    else:
        winrate = closed_stats["wins"] / total * 100
        text = (
            f"📊 <b>Недельная сводка</b>\n\n"
            f"• Сделок: {total}\n"
            f"• Прибыльных: {closed_stats['wins']}\n"
            f"• Убыточных: {closed_stats['losses']}\n"
            f"• Winrate: {winrate:.1f}%\n"
            f"• Общий P&L: {closed_stats['total_pnl']:+.2f}%\n\n"
            f"🤖 Бот: {BOT_LINK}"
        )
    send_to_channel(text)
    send_telegram(text)
    last_weekly_report = now


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
    check_active_trades(price)

    is_signal, reason = check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200)

    if is_signal:
        now = time.time()
        if now - last_signal_time["BUY"] < COOLDOWN_SECONDS:
            print(f"⏱ BUY недавно — пропуск", flush=True)
            return

        target = price * 1.02
        text = (
            f"🟢 <b>РЕКОМЕНДАЦИЯ: ПОКУПАТЬ ETH</b>\n\n"
            f"💵 <b>Цена входа:</b> ${price:.2f}\n"
            f"🎯 <b>Цель (48ч):</b> ${target:.2f} (+2%)\n"
            f"⛔ <b>Стоп-лосс:</b> ${price * 0.98:.2f} (−2%)\n"
            f"⏱ <b>Горизонт:</b> 48 часов\n"
            f"📊 <b>Размер:</b> 5% от депозита\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>📊 RSI (14)</b>\n"
            f"• {describe_rsi(rsi_1h, '1H')}\n"
            f"• {describe_rsi(rsi_1d, '1D')}\n"
            f"\n<b>🐋 CMF (20)</b>\n"
            f"• {describe_cmf(cmf_1h)}\n"
            f"\n<b>📈 EMA 200 (1D):</b> ${ema_200:.2f} ✅\n"
            f"━━━━━━━━━━━━━━━\n"
            f"\n🎯 <b>ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ</b>\n"
            f"📊 Winrate: 59.5%\n\n"
            f"📢 Канал: {CHANNEL_LINK}"
        )
        send_telegram(text)
        send_to_channel(text)

        active_trades.append({
            "entry": price,
            "time": now,
            "type": "BUY"
        })
        print(f"📌 Сделка добавлена: ${price:.2f}", flush=True)
        last_signal_time["BUY"] = now
        return

    send_flat_report(rsi_1h, rsi_1d, cmf_1h, price, ema_200)
    send_channel_update(rsi_1h, rsi_1d, cmf_1h, price, ema_200)
    send_weekly_report()


if __name__ == "__main__":
    print("🚀 Shooter запущен на Render...", flush=True)
    send_telegram(
        "🎯 <b>Shooter запущен!</b>\n\n"
        "🔧 <b>Стратегия:</b> ETH / RSI + CMF + EMA 200\n"
        "📊 <b>Winrate:</b> 59.5% (3.4 года)\n"
        "📈 <b>Результат:</b> +232%\n\n"
        "📩 <b>Что присылаю:</b>\n"
        "• 🟢 BUY-сигналы с правилами\n"
        "• ✅ Результат каждой сделки\n"
        "• 💤 Отчёты о рынке раз в 4 часа\n"
        "• 🔄 Уведомления о смене тренда\n\n"
        f"📢 Канал: {CHANNEL_LINK}"
    )

    print(f"📢 Канал: {CHANNEL_ID}", flush=True)
    send_to_channel(
        "🎯 <b>Добро пожаловать в Shooter!</b>\n\n"
        "Это канал автоматического торгового бота на ETH.\n\n"
        "📊 <b>Что здесь будет:</b>\n"
        "• Обзор рынка ETH каждые 4 часа\n"
        "• Сигналы BUY с правилами\n"
        "• Результаты сделок\n"
        "• Статистика Winrate и P&L\n\n"
        "📈 <b>Стратегия бота:</b>\n"
        "• RSI + CMF + EMA 200\n"
        "• Winrate 59.5% на 3.4 годах\n"
        "• +232% за период бэктеста\n\n"
        f"🤖 Личные сигналы от бота: {BOT_LINK}\n\n"
        "⚠️ Не финансовая рекомендация. Торговля связана с риском."
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
