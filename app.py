import os
import time
import threading
from datetime import datetime
import requests
from flask import Flask

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_НОВЫЙ_ТОКЕН")
CHAT_ID   = os.environ.get("CHAT_ID", "465503608")
INTERVAL_SECONDS = 180
COOLDOWN_SECONDS = 15 * 60

URL_TELEGRAM  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
URL_BYBIT_4H  = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=240&limit=200"
URL_BYBIT_15M = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=15&limit=200"

last_signal_time = {"BUY": 0, "SELL": 0}

# === FLASK (для Render) ===
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# === ВЕСЬ ТВОЙ КОД БОТА (без изменений) ===
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

# ... [ВСТАВЬ СЮДА ВСЕ ФУНКЦИИ: get_bybit_data, calc_rsi, calc_cmf, describe_rsi, describe_cmf, build_recommendation, main_analysis] ...
# (Скопируй их из предыдущего рабочего кода без изменений)

# === ЗАПУСК ===
if __name__ == "__main__":
    print("🚀 Робот 'Торгаш' запущен на Render...")
    send_telegram("🚀 *Бот Торгаш запущен в облаке Render!*\n\nТеперь я работаю 24/7, независимо от телефона.")

    # Запускаем Flask в отдельном потоке, чтобы Render видел открытый порт
    threading.Thread(target=run_flask, daemon=True).start()

    while True:
        try:
            main_analysis()
        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}")
        time.sleep(INTERVAL_SECONDS)