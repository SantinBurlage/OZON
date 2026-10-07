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
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from playwright.async_api import async_playwright, Browser, BrowserContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
CHECK_INTERVAL_SECONDS = 15
PROXY_SERVER = "http://127.0.0.1:10809"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

browser: Optional[Browser] = None
context: Optional[BrowserContext] = None
browser_lock = asyncio.Semaphore(1)

user_tracked_urls: Dict[int, List[str]] = {}
item_last_status: Dict[Tuple[int, str], Optional[bool]] = {}
xray_process = None

# --- ЗАПУСК XRAY ---
def setup_and_start_xray():
    global xray_process
    xray_url = "https://github.com/XTLS/Xray-core/releases/download/v1.8.23/Xray-linux-64.zip"
    zip_path = "xray.zip"
    extract_dir = "./xray_bin"
    executable = os.path.join(extract_dir, "xray")

    if not os.path.exists(executable):
        logging.info("Скачивание Xray-core...")
        urllib.request.urlretrieve(xray_url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        os.chmod(executable, 0o755)
        os.remove(zip_path)
        logging.info("Xray установлен.")

    logging.info("Запуск Xray-core...")
    xray_process = subprocess.Popen([executable, "run", "-c", "config.json"])
    return xray_process

# --- PLAYWRIGHT И ПРОВЕРКА ---
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
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
    return context

async def check_current_ip() -> str:
    ctx = await get_browser_context()
    page = await ctx.new_page()
    try:
        await page.goto("https://api.ipify.org?format=json", timeout=15000)
        return await page.inner_text("body")
    except Exception as e:
        return f"Ошибка: {html.escape(str(e))}"
    finally:
        await page.close()

async def check_product(url: str) -> Tuple[Optional[bool], str, Optional[bytes], Optional[str]]:
    async with browser_lock:
        ctx = await get_browser_context()
        page = await ctx.new_page()
        try:
            response = await page.goto(url, wait_until="commit", timeout=25000)
            await asyncio.sleep(4)
            content = await page.content()

            if bool(re.search(r"проверка безопасности|cloudflare|access denied", content, re.IGNORECASE)):
                return None, "Капча", await page.screenshot(type="jpeg", quality=75), "Ozon заблокировал запрос"

            if response and response.status in (403, 429):
                return None, f"HTTP {response.status}", await page.screenshot(type="jpeg", quality=75), f"Блокировка ({response.status})"

            is_out = bool(re.search(r"товар закончился|узнать о поступлении", content, re.IGNORECASE))
            has_buy = bool(re.search(r"в корзину|купить в 1 клик", content, re.IGNORECASE))

            if not has_buy and not is_out:
                return None, "Сбой", await page.screenshot(type="jpeg", quality=75), "Элементы не найдены"

            is_avail = has_buy and not is_out
            match = re.search(r"осталось\s+(\d+)\s*шт", content, re.IGNORECASE)
            stock = f"{match.group(1)} шт." if match else "Много"
            return is_avail, stock, await page.screenshot(type="jpeg", quality=75), None

        except Exception as e:
            return None, "Таймаут", None, f"Сбой: {str(e)[:50]}"
        finally:
            await page.close()

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    if chat_id not in user_tracked_urls:
        user_tracked_urls[chat_id] = []
    
    status = "🟢 Работает" if xray_process and xray_process.poll() is None else "🔴 Упал"
    await message.answer(f"👋 <b>Бот запущен на Railway!</b>\nСтатус Xray: {status}\n\n/ip — Проверить внешний IP\n/check — Проверить ссылки", parse_mode="HTML")

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    wait_msg = await message.answer("🔄 Запрашиваю IP через прокси...")
    ip_info = await check_current_ip()
    await wait_msg.edit_text(f"🌐 <b>Внешний IP:</b>\n<code>{ip_info}</code>", parse_mode="HTML")

@dp.message(Command("check"))
async def cmd_check(message: Message):
    chat_id = message.chat.id
    urls = user_tracked_urls.get(chat_id, [])
    if not urls:
        await message.answer("Список пуст. Отправь ссылку на Ozon.")
        return

    wait_msg = await message.answer("🔍 Проверяю...")
    for idx, url in enumerate(urls, 1):
        is_avail, stock, screen, err = await check_product(url)
        status_str = f"⚠️ Ошибка: {err}" if err else ("🟢 В наличии" if is_avail else "🔴 Нет")
        cap = f"Товар #{idx}\nСтатус: <b>{status_str}</b>\nОстаток: {stock}\n🔗 {url}"
        if screen:
            await bot.send_photo(chat_id, BufferedInputFile(screen, filename=f"oz_{idx}.jpg"), caption=cap, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, cap, parse_mode="HTML")
    await wait_msg.delete()

@dp.message(F.text.regexp(r"https?://(?:www\.)?ozon\.ru/\S+"))
async def add_url(message: Message):
    url = re.search(r"https?://(?:www\.)?ozon\.ru/\S+", message.text).group(0)
    chat_id = message.chat.id
    if chat_id not in user_tracked_urls:
        user_tracked_urls[chat_id] = []
    if url not in user_tracked_urls[chat_id]:
        user_tracked_urls[chat_id].append(url)
    await message.answer(f"🎯 <b>Добавлено:</b>\n{url}", parse_mode="HTML", disable_web_page_preview=True)

async def main():
    setup_and_start_xray()
    await asyncio.sleep(3) # Ждем поднятия туннеля
    print("Бот запущен, ожидаю сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
