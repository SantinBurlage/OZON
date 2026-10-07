import os
import sys
import asyncio
import datetime
import logging
import re
import html
import zipfile
import urllib.request
import subprocess
import socket
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from playwright.async_api import async_playwright, Browser, BrowserContext

BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
CHECK_INTERVAL_SECONDS = 15
MAX_HISTORY_PER_USER = 100
PROXY_SERVER = "http://127.0.0.1:10809"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

browser: Optional[Browser] = None
context: Optional[BrowserContext] = None
browser_lock = asyncio.Semaphore(1)

user_tracked_urls: Dict[int, List[str]] = {}
item_last_status: Dict[Tuple[int, str], Optional[bool]] = {}
item_last_error: Dict[Tuple[int, str], Optional[str]] = {}
user_attempts_history: Dict[int, List[dict]] = {}
monitoring_tasks: Dict[int, asyncio.Task] = {}
xray_process = None
xray_error_log = "Запуск еще не производился"

def setup_and_start_xray():
    global xray_process, xray_error_log
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config.json")
    xray_url = "https://github.com/XTLS/Xray-core/releases/download/v1.8.23/Xray-linux-64.zip"
    zip_path = os.path.join(base_dir, "xray.zip")
    extract_dir = os.path.join(base_dir, "xray_bin")
    executable = os.path.join(extract_dir, "xray")

    try:
        if not os.path.exists(config_path):
            xray_error_log = f"Файл {config_path} не найден!"
            logging.error(xray_error_log)
            return None

        if not os.path.exists(executable):
            logging.info("Скачивание Xray...")
            urllib.request.urlretrieve(xray_url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            os.chmod(executable, 0o755)
            if os.path.exists(zip_path):
                os.remove(zip_path)

        # Пишем логи в файл, чтобы не блокировать процесс
        log_file = open(os.path.join(base_dir, "xray.log"), "w")
        logging.info("Запуск Xray-core...")
        xray_process = subprocess.Popen(
            [executable, "run", "-c", config_path],
            stdout=log_file,
            stderr=subprocess.STDOUT
        )
        return xray_process
    except Exception as exc:
        xray_error_log = f"Исключение: {str(exc)}"
        logging.error(xray_error_log)
        return None

def get_xray_status() -> Tuple[str, str]:
    global xray_process
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(base_dir, "xray.log")
    
    if xray_process is None:
        return "🔴 Не запущен", xray_error_log
        
    ret = xray_process.poll()
    log_data = "Лог пуст"
    try:
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                lines = f.readlines()
                log_data = "".join(lines[-15:])
    except:
        pass
        
    if ret is None:
        return "🟢 Активен", log_data
    return "🔴 Остановлен", log_data

async def get_browser_context() -> BrowserContext:
    global browser, context
    if browser is None or not browser.is_connected():
        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": PROXY_SERVER},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
        )
    if context is None:
        context = await browser.new_context(user_agent=USER_AGENT)
    return context

@dp.message(Command("start"))
async def cmd_start(message: Message):
    status, log_info = get_xray_status()
    await message.answer(
        f"👋 <b>Ozon Монитор</b>\nТуннель: <b>{status}</b>\n\nКоманды:\n/diag — 🕵️ Запустить расследование\n/ip — Внешний IP\n/check — Проверить ссылки",
        parse_mode="HTML"
    )

@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    wait_msg = await message.answer("🕵️ Начинаю расследование...\n1️⃣ Пингую сервер\n2️⃣ Тестирую прокси\n3️⃣ Читаю дебаг-логи")
    report = "📊 <b>Результаты расследования:</b>\n\n"
    
    # 1. Пинг TCP (блокирует ли нас сервер?)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        result = sock.connect_ex(('138.124.254.23', 443))
        if result == 0:
            report += "✅ <b>TCP 443:</b> Доступ открыт (Сервер нас видит)\n"
        else:
            report += f"❌ <b>TCP 443:</b> Заблокировано! (Railway в черном списке, код {result})\n"
        sock.close()
    except Exception as e:
        report += f"❌ <b>TCP 443:</b> Ошибка ({e})\n"
        
    # 2. Быстрый HTTP тест (проблема в браузере или ядре?)
    try:
        proxy_handler = urllib.request.ProxyHandler({'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'})
        opener = urllib.request.build_opener(proxy_handler)
        resp = opener.open("https://api.ipify.org?format=json", timeout=8)
        report += f"✅ <b>HTTP-Proxy:</b> Успешно! IP: <code>{resp.read().decode('utf-8')[:20]}</code>\n"
    except Exception as e:
        safe_err = str(e).split(":")[-1].strip()[:50]
        report += f"❌ <b>HTTP-Proxy:</b> Ошибка (<code>{safe_err}</code>)\n"
        
    # 3. Достаем улики
    _, log_info = get_xray_status()
    report += f"\n🔍 <b>Последние логи Xray:</b>\n<code>{html.escape(log_info)}</code>"
    
    await wait_msg.edit_text(report, parse_mode="HTML")

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    ctx = await get_browser_context()
    page = await ctx.new_page()
    try:
        await page.goto("https://api.ipify.org?format=json", wait_until="commit", timeout=15000)
        ip = await page.inner_text("body")
        await message.answer(f"🌐 Внешний IP: {ip}")
    except Exception as e:
        await message.answer(f"Ошибка: {html.escape(str(e))}")
    finally:
        await page.close()

if __name__ == "__main__":
    setup_and_start_xray()
    
    async def main():
        await asyncio.sleep(4)
        await dp.start_polling(bot, drop_pending_updates=True)

    asyncio.run(main())
