import os
import time
import json
import logging
import threading
from datetime import datetime
import requests

# ============================================================
# 1. ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ShooterLive")

# ============================================================
# 2. НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "-1001182337455")
CHANNEL_LINK = "t.me/shooter_eth_signals"
BOT_LINK = "@Torkasch_bot"

INTERVAL_SECONDS        = 180
MIN_HOURS_BETWEEN_TRADES = 0
FLAT_REPORT_SECONDS     = 4 * 60 * 60
CHANNEL_POST_SECONDS    = 4 * 60 * 60
WEEKLY_REPORT_SECONDS   = 7 * 24 * 60 * 60

TRADE_MAX_HOURS         = 48
TRADE_COMMISSION_PCT    = 0.2
TRADE_TAKE_PROFIT_PCT   = 0.02
TRADE_STOP_LOSS_PCT     = 0.02

RSI_TREND_BULL = 50
RSI_ENTRY_BUY  = 35
CMF_BUY        = 0.01
EMA_PERIOD     = 200
EMA_FILTER_PCT = 0.005

URL_TELEGRAM = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
OKX_1H = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1H&limit=500"
OKX_1D = "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT&bar=1D&limit=300"
STATE_FILE = "bot_state.json"

last_signal_time     = {"BUY": 0}
last_flat_report     = 0
last_channel_post    = 0
last_weekly_report   = 0
last_ema_state       = None
last_successful_analysis = "ещё не было"

active_trades = []
closed_stats = {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "history": []}

# ============================================================
# 3. FLASK
# ============================================================
try:
    from flask import Flask, jsonify
    app = Flask(__name__)

    @app.route('/')
    @app.route('/health')
    def health():
        total = closed_stats["total"]
        winrate = (closed_stats["wins"] / total * 100) if total > 0 else 0.0
        return jsonify({
            "status": "ok", "bot": "Shooter ETH",
            "last_analysis": last_successful_analysis,
            "active_trades": len(active_trades),
            "winrate": f"{winrate:.1f}%",
            "total_pnl": f"{closed_stats['total_pnl']:+.2f}%"
        }), 200

    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app.run(host="0.0.0.0", port=port)
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    log.info("ℹ️ Flask не найден — без health-check")

# ============================================================
# 4. СОСТОЯНИЕ
# ============================================================
def save_state():
    try:
        state = {
            "active_trades": active_trades, "closed_stats": closed_stats,
            "last_signal_time": last_signal_time, "last_ema_state": last_ema_state,
            "last_flat_report": last_flat_report, "last_channel_post": last_channel_post,
            "last_weekly_report": last_weekly_report,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as e:
        log.error(f"Ошибка сохранения: {e}")

def load_state():
    global active_trades, closed_stats, last_signal_time, last_ema_state
    global last_flat_report, last_channel_post, last_weekly_report
    if not os.path.exists(STATE_FILE):
        log.info("Файл состояния не найден — старт с нуля")
        return
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        active_trades      = state.get("active_trades", [])
        closed_stats       = state.get("closed_stats", closed_stats)
        last_signal_time   = state.get("last_signal_time", {"BUY": 0})
        last_ema_state     = state.get("last_ema_state", None)
        last_flat_report   = state.get("last_flat_report", 0)
        last_channel_post  = state.get("last_channel_post", 0)
        last_weekly_report = state.get("last_weekly_report", 0)
        log.info(f"✅ Состояние: {len(active_trades)} актив., {closed_stats['total']} закрытых")
    except Exception as e:
        log.error(f"Ошибка чтения состояния: {e}")

# ============================================================
# 5. TELEGRAM & OKX
# ============================================================
def send_telegram(text, target=None):
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN не задан")
        return False
    chat = target or CHAT_ID
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(URL_TELEGRAM, json=payload, timeout=15)
        if r.status_code != 200:
            log.error(f"Telegram ({chat}): {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"Ошибка Telegram: {e}")
        return False

def send_to_channel(text):
    return send_telegram(text, target=CHANNEL_ID)

def fetch_okx(url, label):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200 or r.json().get("code") != "0":
            return None
        rows = r.json()["data"]
        if not rows:
            return None
        rows.reverse()
        return ([float(x[1]) for x in rows], [float(x[2]) for x in rows],
                [float(x[3]) for x in rows], [float(x[4]) for x in rows],
                [float(x[5]) for x in rows])
    except Exception as e:
        log.warning(f"OKX {label} error: {e}")
        return None

# ============================================================
# 6. ИНДИКАТОРЫ
# ============================================================
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(abs(ch) if ch < 0 else 0.0)
    ag, al = sum(gains[:period])/period, sum(losses[:period])/period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))

def calc_cmf(highs, lows, closes, volumes, period=20):
    if len(closes) < period: return 0.0
    mf = [(((closes[i] - lows[i]) - (highs[i] - closes[i])) / (highs[i] - lows[i]) if (highs[i] - lows[i]) else 0.0) * volumes[i] for i in range(len(closes))]
    vs = sum(volumes[-period:])
    return sum(mf[-period:]) / vs if vs else 0.0

def calc_ema(closes, period=200):
    if len(closes) < period: return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
    return ema

def get_winrate_line_ru():
    total = closed_stats["total"]
    if total == 0: return "📊 Winrate: пока нет сделок"
    wr = closed_stats["wins"] / total * 100
    return f"📊 Winrate: {wr:.1f}% ({closed_stats['wins']}/{total}) | P&L: {closed_stats['total_pnl']:+.2f}%"

def get_winrate_line_en():
    total = closed_stats["total"]
    if total == 0: return "📊 Winrate: no closed trades yet"
    wr = closed_stats["wins"] / total * 100
    return f"📊 Winrate: {wr:.1f}% ({closed_stats['wins']}/{total}) | P&L: {closed_stats['total_pnl']:+.2f}%"

# ============================================================
# 7. ТРЕКИНГ СДЕЛОК (закрытие через 48ч)
# ============================================================
def check_active_trades(current_price):
    global active_trades
    now = time.time()
    still_open, state_changed = [], False

    for trade in active_trades:
        elapsed_hours = (now - trade["time"]) / 3600
        entry = trade["entry"]

        if elapsed_hours >= TRADE_MAX_HOURS:
            pnl_net = ((current_price - entry) / entry * 100) - TRADE_COMMISSION_PCT
            closed_stats["total"] += 1
            closed_stats["total_pnl"] += pnl_net

            if pnl_net > 0:
                closed_stats["wins"] += 1
                emoji, label_ru, label_en = "✅", "В ПЛЮС", "IN PROFIT"
            else:
                closed_stats["losses"] += 1
                emoji, label_ru, label_en = "❌", "В МИНУС", "IN LOSS"

            closed_stats["history"].append({
                "entry": entry, "exit": current_price,
                "pnl": pnl_net, "time": trade["time"]
            })

            total = closed_stats["total"]
            winrate = closed_stats["wins"] / total * 100
            entry_time = datetime.fromtimestamp(trade["time"]).strftime("%d.%m %H:%M")

            # Двуязычное сообщение
            text = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА {label_ru}</b> / <b>TRADE CLOSED {label_en}</b>\n\n"
                    f"🇷🇺 RU:\n"
                    f"🟢 BUY ETH\n"
                    f"📅 Открыта: {entry_time}\n"
                    f"💵 Вход: ${entry:.2f}\n"
                    f"💵 Выход: ${current_price:.2f}\n"
                    f"📊 P&L: {pnl_net:+.2f}%\n"
                    f"⏱ Удержание: 48ч\n\n"
                    f"🇬🇧 EN:\n"
                    f"🟢 BUY ETH\n"
                    f"📅 Opened: {entry_time}\n"
                    f"💵 Entry: ${entry:.2f}\n"
                    f"💵 Exit: ${current_price:.2f}\n"
                    f"📊 P&L: {pnl_net:+.2f}%\n"
                    f"⏱ Holding: 48h\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 <b>СТАТИСТИКА / STATS:</b>\n"
                    f"• Всего / Total: {total}\n"
                    f"• Winrate: {winrate:.1f}%\n"
                    f"• P&L: {closed_stats['total_pnl']:+.2f}%\n\n"
                    f"📢 {CHANNEL_LINK}")

            send_telegram(text)
            send_to_channel(text)
            state_changed = True
        else:
            still_open.append(trade)

    active_trades = still_open
    if state_changed:
        save_state()

def check_ema_state(price, ema_200):
    global last_ema_state
    if ema_200 is None: return
    upper, lower = ema_200 * (1 + EMA_FILTER_PCT), ema_200 * (1 - EMA_FILTER_PCT)
    current = "above" if price > upper else ("below" if price < lower else None)

    if current and current != last_ema_state:
        if current == "above":
            msg = (f"🚀 <b>ETH ПРОБИЛ EMA 200 ВВЕРХ</b> / <b>ETH BROKE ABOVE EMA 200</b>\n\n"
                   f"💵 ${price:.2f} | 📊 EMA: ${ema_200:.2f}\n"
                   f"✅ Бычий тренд активирован. / Bullish trend activated.\n\n"
                   f"🤖 {BOT_LINK}")
        else:
            msg = (f"⚠️ <b>ETH УПАЛ НИЖЕ EMA 200</b> / <b>ETH FELL BELOW EMA 200</b>\n\n"
                   f"💵 ${price:.2f} | 📊 EMA: ${ema_200:.2f}\n"
                   f"🛑 BUY-сигналы приостановлены. / BUY signals paused.\n\n"
                   f"🤖 {BOT_LINK}")
        send_telegram(msg)
        send_to_channel(msg)
        last_ema_state = current
        save_state()

def check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
    if ema_200 is None: return False
    return (rsi_1d > RSI_TREND_BULL) and (rsi_1h < RSI_ENTRY_BUY) and (cmf_1h > CMF_BUY) and (price > ema_200)

# ============================================================
# 8. ГЛАВНЫЙ ЦИКЛ
# ============================================================
def main_analysis():
    global last_successful_analysis, last_flat_report, last_channel_post
    data_1h, data_1d = fetch_okx(OKX_1H, "1H"), fetch_okx(OKX_1D, "1D")
    if not data_1h or not data_1d:
        log.warning("Нет данных от OKX — пропуск")
        return

    rsi_1h = calc_rsi(data_1h[3])
    cmf_1h = calc_cmf(data_1h[1], data_1h[2], data_1h[3], data_1h[4])
    rsi_1d = calc_rsi(data_1d[3])
    ema_200 = calc_ema(data_1d[3], EMA_PERIOD)
    price = data_1h[3][-1]

    if ema_200 is None: return

    log.info(f"ETH ${price:.2f} | RSI1H {rsi_1h:.1f} | RSI1D {rsi_1d:.1f} | CMF1H {cmf_1h:+.3f} | EMA200 ${ema_200:.2f}")

    check_ema_state(price, ema_200)
    check_active_trades(price)

    if check_signal(rsi_1h, rsi_1d, cmf_1h, price, ema_200):
        now = time.time()

        target = price * (1 + TRADE_TAKE_PROFIT_PCT)
        stop = price * (1 - TRADE_STOP_LOSS_PCT)

        text = (f"🟢 <b>РЕКОМЕНДАЦИЯ: ПОКУПАТЬ ETH</b> / <b>RECOMMENDATION: BUY ETH</b>\n\n"
                f"🇷🇺 <b>RU:</b>\n"
                f"💵 Вход: ${price:.2f}\n"
                f"🎯 Тейк: ${target:.2f} (+2%)\n"
                f"⛔ Стоп: ${stop:.2f} (−2%)\n"
                f"⏱ Горизонт: 48ч\n"
                f"📊 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
                f"🐋 CMF 1H: {cmf_1h:+.3f}\n"
                f"📈 EMA 200: ${ema_200:.2f} ✅\n\n"
                f"🇬🇧 <b>EN:</b>\n"
                f"💵 Entry: ${price:.2f}\n"
                f"🎯 TP: ${target:.2f} (+2%)\n"
                f"⛔ SL: ${stop:.2f} (−2%)\n"
                f"⏱ Horizon: 48h\n"
                f"📊 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
                f"🐋 CMF 1H: {cmf_1h:+.3f}\n"
                f"📈 EMA 200: ${ema_200:.2f} ✅\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🎯 <b>ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ / ALL CONDITIONS MET</b>\n"
                f"{get_winrate_line_ru()}\n\n"
                f"📢 {CHANNEL_LINK}")

        send_telegram(text)
        send_to_channel(text)

        active_trades.append({"entry": price, "time": now, "type": "BUY"})
        log.info(f"📌 Сделка открыта: ${price:.2f}")
        last_signal_time["BUY"] = now
        save_state()
        return

    now = time.time()

    if now - last_flat_report > FLAT_REPORT_SECONDS:
        send_telegram(
            f"💤 <b>ETH — сигналов нет</b> / <b>ETH — no signals</b>\n\n"
            f"🇷🇺 RU:\n"
            f"💵 ${price:.2f}\n"
            f"📊 EMA 200: ${ema_200:.2f}\n"
            f"📊 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
            f"🐋 CMF 1H: {cmf_1h:+.3f}\n\n"
            f"🇬🇧 EN:\n"
            f"💵 ${price:.2f}\n"
            f"📊 EMA 200: ${ema_200:.2f}\n"
            f"📊 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
            f"🐋 CMF 1H: {cmf_1h:+.3f}\n\n"
            f"{get_winrate_line_ru()}\n\n"
            f"📢 {CHANNEL_LINK}"
        )
        last_flat_report = now

    if now - last_channel_post > CHANNEL_POST_SECONDS:
        send_to_channel(
            f"📊 <b>ETH — обзор рынка / Market Overview</b>\n\n"
            f"🇷🇺 RU:\n"
            f"💵 Цена: ${price:.2f}\n"
            f"📈 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
            f"🐋 CMF 1H: {cmf_1h:+.3f}\n"
            f"📊 EMA 200: ${ema_200:.2f}\n\n"
            f"🇬🇧 EN:\n"
            f"💵 Price: ${price:.2f}\n"
            f"📈 RSI 1H: {rsi_1h:.1f} | RSI 1D: {rsi_1d:.1f}\n"
            f"🐋 CMF 1H: {cmf_1h:+.3f}\n"
            f"📊 EMA 200: ${ema_200:.2f}\n\n"
            f"🎯 Сигналов нет. Бот ждёт условий. / No signals. Bot waiting.\n\n"
            f"🤖 {BOT_LINK}"
        )
        last_channel_post = now

    last_successful_analysis = datetime.now().strftime("%H:%M:%S")

# ============================================================
# 9. ТОЧКА ВХОДА
# ============================================================
if __name__ == "__main__":
    if not BOT_TOKEN:
        log.warning("⚠️ BOT_TOKEN не задан!")

    log.info("🚀 Shooter запущен")
    load_state()

    if FLASK_AVAILABLE:
        threading.Thread(target=run_flask, daemon=True).start()
        log.info("🌐 Flask health-check запущен")

    log.info("🔁 Цикл анализа запущен (интервал 180с)")
    while True:
        try:
            main_analysis()
        except Exception as e:
            log.error(f"Ошибка в цикле: {e}", exc_info=True)
        time.sleep(INTERVAL_SECONDS)
