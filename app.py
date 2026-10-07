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
    xray_url = "https://github.com/XTLS/Xray-core/releases/download/v1.8.23/Xray-linux-64.zip"
    zip_path = "xray.zip"
    extract_dir = "./xray_bin"
    executable = os.path.join(extract_dir, "xray")

    try:
        if not os.path.exists(executable):
            logging.info("Скачивание Xray-core...")
            urllib.request.urlretrieve(xray_url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            os.chmod(executable, 0o755)
            if os.path.exists(zip_path):
                os.remove(zip_path)
            logging.info("Xray установлен.")

        # Тестовая валидация конфига
        test_run = subprocess.run([executable, "run", "-test", "-c", "config.json"], capture_output=True, text=True)
        if test_run.returncode != 0:
            xray_error_log = f"Ошибка валидации config.json:\n{test_run.stderr or test_run.stdout}"
            logging.error(xray_error_log)
            return None

        logging.info("Запуск Xray-core...")
        xray_process = subprocess.Popen(
            [executable, "run", "-c", "config.json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return xray_process
    except Exception as exc:
        xray_error_log = f"Исключение при запуске: {str(exc)}"
        logging.error(xray_error_log)
        return None

def get_xray_status() -> Tuple[str, str]:
    global xray_process, xray_error_log
    if xray_process is None:
        return "🔴 Не запущен", xray_error_log
    ret = xray_process.poll()
    if ret is None:
        return "🟢 Активен", "Работает в штатном режиме"
    err = xray_process.stderr.read() if xray_process.stderr else ""
    out = xray_process.stdout.read() if xray_process.stdout else ""
    details = err or out or f"Процесс завершился с кодом {ret}"
    return "🔴 Остановлен", details

def record_attempt(chat_id: int, url: str, is_available: Optional[bool], stock: str, err_msg: Optional[str]):
    if chat_id not in user_attempts_history:
        user_attempts_history[chat_id] = []
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    user_attempts_history[chat_id].append({
        "time": now.strftime("%H:%M:%S"),
        "url": url,
        "is_available": is_available,
        "stock": stock,
        "error": err_msg
    })
    if len(user_attempts_history[chat_id]) > MAX_HISTORY_PER_USER:
        user_attempts_history[chat_id].pop(0)

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
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )

    if context is None:
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)
    return context

async def check_current_ip() -> str:
    ctx = await get_browser_context()
    page = await ctx.new_page()
    try:
        await page.goto("https://api.ipify.org?format=json", wait_until="commit", timeout=25000)
        await asyncio.sleep(2)
        return await page.inner_text("body")
    except Exception as e:
        safe_err = html.escape(str(e))
        return f"Ошибка: {safe_err}"
    finally:
        await page.close()

async def check_product(url: str) -> Tuple[Optional[bool], str, Optional[bytes], Optional[str]]:
    async with browser_lock:
        ctx = await get_browser_context()
        page = await ctx.new_page()
        try:
            response = await page.goto(url, wait_until="commit", timeout=30000)
            await asyncio.sleep(4)

            content = await page.content()

            is_captcha = bool(re.search(r"проверка безопасности|подтвердите,\s*что\s*вы\s*не\s*робот|captcha|cloudflare|access denied", content, re.IGNORECASE))
            if is_captcha:
                screenshot = await page.screenshot(type="jpeg", quality=75)
                return None, "Капча", screenshot, "Ozon запросил подтверждение"

            if response and response.status in (403, 429, 500, 502, 503):
                screenshot = await page.screenshot(type="jpeg", quality=75)
                return None, "HTTP ошибка", screenshot, f"Код HTTP {response.status}"

            is_out_of_stock = bool(re.search(r"товар закончился|узнать о поступлении", content, re.IGNORECASE))
            has_buy_button = bool(re.search(r"в корзину|купить в 1 клик|>купить<", content, re.IGNORECASE))

            screenshot = await page.screenshot(type="jpeg", quality=75)

            if not has_buy_button and not is_out_of_stock:
                return None, "Сбой структуры", screenshot, "Элементы товара не найдены"

            is_available = has_buy_button and not is_out_of_stock
            match = re.search(r"осталось\s+(\d+)\s*шт", content, re.IGNORECASE)
            stock_text = f"{match.group(1)} шт." if match else "много / без лимита"
            return is_available, stock_text, screenshot, None

        except Exception as exc:
            safe_err = html.escape(str(exc)[:100])
            return None, "Таймаут/Сбой", None, f"Ошибка: {safe_err}"
        finally:
            await page.close()

async def monitoring_loop(chat_id: int):
    while True:
        try:
            urls = user_tracked_urls.get(chat_id, [])
            for url in list(urls):
                is_available, stock, screenshot, err_msg = await check_product(url)
                last_state = item_last_status.get((chat_id, url))
                last_err = item_last_error.get((chat_id, url))

                record_attempt(chat_id, url, is_available, stock, err_msg)

                if err_msg is not None:
                    if last_err != err_msg:
                        item_last_error[(chat_id, url)] = err_msg
                        item_last_status[(chat_id, url)] = None
                        caption = f"⚠️ <b>Ошибка проверки!</b>\nПричина: <code>{err_msg}</code>\n🔗 {url}"
                        if screenshot:
                            await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(screenshot, filename="err.jpg"), caption=caption, parse_mode="HTML")
                        else:
                            await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
                    continue

                if last_err is not None:
                    item_last_error[(chat_id, url)] = None
                    await bot.send_message(chat_id=chat_id, text=f"🟢 <b>Связь восстановлена!</b>\n{url}", parse_mode="HTML", disable_web_page_preview=True)

                if is_available is True and last_state is not True:
                    caption = f"🚨 <b>ТОВАР В НАЛИЧИИ!</b> 🚨\n\n📦 <b>Остаток:</b> {stock}\n🔗 {url}"
                    if screenshot:
                        await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(screenshot, filename="stock.jpg"), caption=caption, parse_mode="HTML")
                    else:
                        await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")

                item_last_status[(chat_id, url)] = is_available
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.error(f"Сбой мониторинга: {exc}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    if chat_id not in user_tracked_urls:
        user_tracked_urls[chat_id] = []
    if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
        monitoring_tasks[chat_id] = asyncio.create_task(monitoring_loop(chat_id))

    status, log_info = get_xray_status()
    safe_log = html.escape(log_info[-500:])

    await message.answer(
        f"👋 <b>Ozon Монитор активен на Railway!</b>\n"
        f"🛡️ Туннель Xray: <b>{status}</b>\n"
        f"🔍 <b>Лог ядра:</b>\n<code>{safe_log}</code>\n\n"
        f"Команды:\n"
        f"🌐 <code>/ip</code> — внешний IP туннеля\n"
        f"🔍 <code>/check</code> — разовая проверка\n"
        f"📜 <code>/history</code> — журнал\n"
        f"📋 <code>/list</code> — отслеживаемые\n"
        f"🗑️ <code>/clear</code> — очистить список",
        parse_mode="HTML"
    )

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    wait_msg = await message.answer("🔄 Запрашиваю внешний IP...")
    ip_info = await check_current_ip()
    await wait_msg.edit_text(f"🌐 <b>Внешний IP туннеля:</b>\n<code>{ip_info}</code>", parse_mode="HTML")

@dp.message(Command("check"))
async def cmd_check(message: Message):
    chat_id = message.chat.id
    urls = user_tracked_urls.get(chat_id, [])
    if not urls:
        await message.answer("📭 Список товаров пуст. Отправьте ссылку на товар.")
        return

    wait_msg = await message.answer("🔍 Проверяю карточки товаров...")
    try:
        for idx, url in enumerate(urls, 1):
            is_avail, stock, screen, err_msg = await check_product(url)
            item_last_status[(chat_id, url)] = is_avail
            item_last_error[(chat_id, url)] = err_msg
            record_attempt(chat_id, url, is_avail, stock, err_msg)

            status_str = f"⚠️ Ошибка ({err_msg})" if err_msg else ("🟢 В наличии" if is_avail else "🔴 Нет в наличии")
            caption = f"Товар #{idx}\nСтатус: <b>{status_str}</b>\n📦 Остаток: <b>{stock}</b>\n🔗 {url}"
            if screen:
                await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(screen, filename=f"check_{idx}.jpg"), caption=caption, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
    finally:
        try:
            await wait_msg.delete()
        except Exception:
            pass

@dp.message(Command("list"))
async def cmd_list(message: Message):
    chat_id = message.chat.id
    urls = user_tracked_urls.get(chat_id, [])
    if not urls:
        await message.answer("📭 Список пуст.")
        return
    text = "📋 <b>Отслеживаемые товары:</b>\n\n"
    for idx, u in enumerate(urls, 1):
        st = item_last_status.get((chat_id, u))
        st_icon = "🟢" if st is True else ("🔴" if st is False else "⚪")
        text += f"{idx}. {st_icon} {u}\n"
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    chat_id = message.chat.id
    user_tracked_urls[chat_id] = []
    await message.answer("🗑️ Список товаров очищен.")

@dp.message(Command("history"))
async def cmd_history(message: Message):
    chat_id = message.chat.id
    history = user_attempts_history.get(chat_id, [])
    if not history:
        await message.answer("📭 Журнал пуст.")
        return

    text = "📜 <b>Последние проверки:</b>\n\n"
    for h in history[-8:]:
        icon = "⚠️" if h["error"] else ("🟢" if h["is_available"] else "🔴")
        info = f"({h['error']})" if h["error"] else f"[{h['stock']}]"
        text += f"• <code>{h['time']}</code> {icon} {info}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text.regexp(r"https?://(?:www\.)?ozon\.ru/\S+"))
async def direct_url_message(message: Message):
    match = re.search(r"https?://(?:www\.)?ozon\.ru/\S+", message.text)
    if match:
        url = match.group(0)
        chat_id = message.chat.id
        if chat_id not in user_tracked_urls:
            user_tracked_urls[chat_id] = []
        if url in user_tracked_urls[chat_id]:
            await message.answer("ℹ️ Этот товар уже есть в списке.")
            return
        user_tracked_urls[chat_id].append(url)
        if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
            monitoring_tasks[chat_id] = asyncio.create_task(monitoring_loop(chat_id))
        await message.answer(f"🎯 <b>Товар взят на контроль!</b>\n{url}", parse_mode="HTML", disable_web_page_preview=True)

if __name__ == "__main__":
    setup_and_start_xray()

    async def main():
        await asyncio.sleep(4)
        print("Бот запущен, ожидаю сообщения...")
        await dp.start_polling(bot, drop_pending_updates=True)

    asyncio.run(main())
