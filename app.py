import os
import sys
import asyncio
import datetime
import logging
import re
import html
import time
import json
import random
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from playwright.async_api import async_playwright, Browser, BrowserContext

# ==========================================
# ⚙️ НАСТРОЙКИ БОТА И ПРОКСИ
# ==========================================
BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
CHECK_INTERVAL_SECONDS = 30
DB_FILE = "database.json"

# Твой прокси от Proxy6
PROXY_TYPE = "http"       
PROXY_IP = "45.81.78.52"             
PROXY_PORT = "8000"           
PROXY_USER = "8BNMZ4"           
PROXY_PASS = "auBcMS"           

# ==========================================
# 🎨 ПРЕМИУМ ЭМОДЗИ (CUSTOM EMOJI)
# Чтобы узнать ID нужного смайлика, просто отправь его боту в чат!
# Затем скопируй цифры и замени их здесь:
# ==========================================
E_OK = '<tg-emoji emoji-id="5422894178558352613">🟢</tg-emoji>'
E_NO = '<tg-emoji emoji-id="5422965610055276326">🔴</tg-emoji>'
E_WARN = '<tg-emoji emoji-id="5422839254332029514">⚠️</tg-emoji>'
E_BOX = '<tg-emoji emoji-id="5422904589257083437">📦</tg-emoji>'
E_MONEY = '<tg-emoji emoji-id="5423164916368481358">💰</tg-emoji>'
E_SALE = '<tg-emoji emoji-id="5422839254332029514">📉</tg-emoji>'
E_LINK = '<tg-emoji emoji-id="5422839254332029514">🔗</tg-emoji>'
E_STATS = '<tg-emoji emoji-id="5422839254332029514">📊</tg-emoji>'
E_LIST = '<tg-emoji emoji-id="5422839254332029514">📋</tg-emoji>'
E_TRASH = '<tg-emoji emoji-id="5422839254332029514">🗑</tg-emoji>'
E_SEARCH = '<tg-emoji emoji-id="5422839254332029514">🔍</tg-emoji>'
E_REFRESH = '<tg-emoji emoji-id="5422839254332029514">🔄</tg-emoji>'
E_SHIELD = '<tg-emoji emoji-id="5422839254332029514">🛡</tg-emoji>'
E_CLOCK = '<tg-emoji emoji-id="5422839254332029514">⏱</tg-emoji>'
E_GLOBE = '<tg-emoji emoji-id="5422839254332029514">🌐</tg-emoji>'
E_CHECK = '<tg-emoji emoji-id="5422839254332029514">✅</tg-emoji>'
E_TARGET = '<tg-emoji emoji-id="5422839254332029514">🎯</tg-emoji>'
E_INFO = '<tg-emoji emoji-id="5422839254332029514">ℹ️</tg-emoji>'
# ==========================================

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

START_TIME = time.time()
TOTAL_CHECKS_COUNT = 0
user_tracked_items: Dict[int, List[dict]] = {}

# --- Работа с Базой Данных (JSON) ---
def load_db():
    global user_tracked_items
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                user_tracked_items = {int(k): v for k, v in loaded.items()}
            logging.info("База данных успешно загружена.")
        except Exception as e:
            logging.error(f"Ошибка чтения БД: {e}")

def save_db():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(user_tracked_items, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения БД: {e}")

def extract_price(price_str: str) -> int:
    digits = re.sub(r"\D", "", price_str)
    return int(digits) if digits else 0

# --- Подключение Playwright ---
async def get_browser_context() -> BrowserContext:
    global browser, context
    if browser is None or not browser.is_connected():
        p = await async_playwright().start()
        
        proxy_conf = {
            "server": f"{PROXY_TYPE}://{PROXY_IP}:{PROXY_PORT}",
            "username": PROXY_USER,
            "password": PROXY_PASS
        }
        
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_conf,
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
            window.chrome = { runtime: {}, app: {} };
        """)
    return context

# --- Парсинг страницы ---
async def inspect_ozon_page(url: str) -> Tuple[Optional[bool], str, str, str, Optional[bytes], Optional[str]]:
    global TOTAL_CHECKS_COUNT
    TOTAL_CHECKS_COUNT += 1

    async with browser_lock:
        ctx = await get_browser_context()
        page = await ctx.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await page.mouse.wheel(0, random.randint(300, 700))
            await asyncio.sleep(random.uniform(1.5, 3.0))
            content = await page.content()

            if re.search(r"проверка безопасности|captcha|access denied", content, re.IGNORECASE):
                screen = await page.screenshot(type="jpeg", quality=75)
                return None, "Неизвестно", "—", "Капча", screen, "Блокировка / Капча Ozon"

            if response and response.status in (403, 429, 500, 502, 503):
                screen = await page.screenshot(type="jpeg", quality=75)
                return None, "Неизвестно", "—", "Сбой HTTP", screen, f"Код ответа {response.status}"

            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", content, re.IGNORECASE)
            item_name = title_match.group(1).strip() if title_match else "Товар Ozon"

            price_match = re.search(r"([\d\s ]+)\s*₽", content)
            price_text = f"{price_match.group(1).strip()} ₽" if price_match else "Не определена"

            is_out_of_stock = bool(re.search(r"товар закончился|узнать о поступлении|нет в наличии", content, re.IGNORECASE))
            has_buy_button = bool(re.search(r"в корзину|купить в 1 клик|>купить<", content, re.IGNORECASE))

            stock_limit_match = re.search(r"осталось\s+(\d+)\s*шт", content, re.IGNORECASE)
            stock_info = f"{stock_limit_match.group(1)} шт." if stock_limit_match else "Достаточно"

            screen = await page.screenshot(type="jpeg", quality=80)

            if not has_buy_button and not is_out_of_stock:
                return None, item_name, price_text, "Неизвестно", screen, "Кнопка покупки не распознана"

            is_available = has_buy_button and not is_out_of_stock
            return is_available, item_name, price_text, stock_info, screen, None

        except Exception as exc:
            err = html.escape(str(exc)[:90])
            return None, "Ошибка", "—", "—", None, f"Таймаут: {err}"
        finally:
            await page.close()

def make_product_keyboard(url: str, item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"check_now:{item_id}"),
            InlineKeyboardButton(text="🔗 Купить", url=url)
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_item:{item_id}")
        ]
    ])

# --- Фоновый мониторинг ---
async def monitoring_worker(chat_id: int):
    while True:
        try:
            items = user_tracked_items.get(chat_id, [])
            for item in list(items):
                is_avail, name, price, stock, screen, err = await inspect_ozon_page(item["url"])
                old_status = item.get("status")
                old_price = item.get("price")

                item["name"] = name
                item["price"] = price
                item["stock"] = stock
                item["status"] = is_avail
                save_db()

                if err is not None:
                    if item.get("last_error") != err:
                        item["last_error"] = err
                        caption = f"{E_WARN} <b>Сбой проверки:</b>\n{E_BOX} {name}\n{E_WARN} <code>{err}</code>"
                        kb = make_product_keyboard(item["url"], item["id"])
                        if screen:
                            await bot.send_photo(chat_id, BufferedInputFile(screen, filename="err.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
                    continue

                item["last_error"] = None
                kb = make_product_keyboard(item["url"], item["id"])
                
                if is_avail is True and old_status is not True:
                    caption = (
                        f"{E_OK} <b>ТОВАР В НАЛИЧИИ!</b> {E_OK}\n\n"
                        f"{E_BOX} <b>{name}</b>\n"
                        f"{E_MONEY} Цена: <b>{price}</b>\n"
                        f"{E_STATS} Остаток: {stock}"
                    )
                    if screen:
                        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="in.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
                    else:
                        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

                elif is_avail is True and old_price and old_price != "Не определена" and old_price != price:
                    old_p_val = extract_price(old_price)
                    new_p_val = extract_price(price)
                    
                    if new_p_val < old_p_val and new_p_val > 0:
                        diff = old_p_val - new_p_val
                        caption = (
                            f"{E_SALE} <b>СНИЖЕНИЕ ЦЕНЫ!</b>\n\n"
                            f"{E_BOX} <b>{name}</b>\n"
                            f"{E_MONEY} Было: {old_price} ➔ <b>Стало: {price}</b>\n"
                            f"{E_SALE} <b>Выгода: {diff} ₽</b>"
                        )
                        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="sale.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)

                await asyncio.sleep(random.uniform(3.0, 5.0))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.error(f"Worker err: {exc}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS + random.uniform(1.0, 10.0))

# --- Команды ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    if chat_id not in user_tracked_items:
        user_tracked_items[chat_id] = []
        save_db()
        
    if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
        monitoring_tasks[chat_id] = asyncio.create_task(monitoring_worker(chat_id))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мой список", callback_data="show_list_btn"), InlineKeyboardButton(text="📊 Статус", callback_data="show_stats_btn")]
    ])

    await message.answer(
        f"{E_OK} <b>Ozon Ultimate Edition</b>\n"
        f"{E_SHIELD} Прокси подключен: <code>{PROXY_IP}</code>\n\n"
        f"Отправь ссылку на товар, чтобы начать отслеживание.\n"
        f"<i>P.S. Чтобы узнать ID любого премиум-эмодзи, просто отправь его мне!</i>",
        parse_mode="HTML",
        reply_markup=kb
    )

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    wait_msg = await message.answer(f"{E_REFRESH} Запрашиваю сетевой IP...")
    ctx = await get_browser_context()
    page = await ctx.new_page()
    try:
        await page.goto("https://api.ipify.org?format=json", wait_until="commit", timeout=25000)
        await asyncio.sleep(1)
        ip_data = await page.inner_text("body")
        await wait_msg.edit_text(f"{E_GLOBE} <b>Выходной IP:</b>\n<code>{ip_data}</code>", parse_mode="HTML")
    except Exception as exc:
        await wait_msg.edit_text(f"{E_NO} <b>Ошибка:</b> {html.escape(str(exc))}", parse_mode="HTML")
    finally:
        await page.close()

@dp.message(Command("list"))
async def cmd_list(message: Message):
    chat_id = message.chat.id
    items = user_tracked_items.get(chat_id, [])
    if not items:
        await message.answer(f"{E_BOX} Ваш список пуст. Отправьте ссылку!")
        return

    text = f"{E_LIST} <b>Ваши товары:</b>\n\n"
    for idx, item in enumerate(items, 1):
        st = item.get("status")
        icon = E_OK if st is True else (E_NO if st is False else "⚪")
        price = item.get("price", "—")
        text += f"{idx}. {icon} <b>{item.get('name', 'Загрузка...')}</b>\n"
        text += f"{E_MONEY} Цена: {price} | {E_LINK} <a href='{item['url']}'>Ссылка</a>\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Сканировать все сейчас", callback_data="check_all_btn")],
        [InlineKeyboardButton(text="🗑 Очистить список", callback_data="clear_all_btn")]
    ])
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)

@dp.message(F.text.regexp(r"https?://(?:www\.)?ozon\.ru/\S+"))
async def handle_url(message: Message):
    url = re.search(r"https?://(?:www\.)?ozon\.ru/\S+", message.text).group(0)
    chat_id = message.chat.id

    if chat_id not in user_tracked_items:
        user_tracked_items[chat_id] = []

    if any(item["url"] == url for item in user_tracked_items[chat_id]):
        await message.answer(f"{E_INFO} Этот товар уже есть в базе.")
        return

    item_id = int(time.time() * 1000) % 1000000
    new_item = {
        "id": item_id, "url": url, "name": "Анализирую...",
        "price": "—", "status": None, "stock": "—", "last_error": None
    }
    user_tracked_items[chat_id].append(new_item)
    save_db()

    if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
        monitoring_tasks[chat_id] = asyncio.create_task(monitoring_worker(chat_id))

    wait_msg = await message.answer(f"{E_TARGET} <b>Добавлено!</b> Делаю первый снимок...")
    
    is_avail, name, price, stock, screen, err = await inspect_ozon_page(url)
    new_item["name"] = name
    new_item["price"] = price
    new_item["status"] = is_avail
    new_item["stock"] = stock
    save_db()

    status_str = f"{E_WARN} Ошибка ({err})" if err else (f"{E_OK} В наличии" if is_avail else f"{E_NO} Закончился")
    caption = (
        f"{E_CHECK} <b>Успешно добавлено</b>\n\n"
        f"{E_BOX} <b>{name}</b>\n"
        f"Статус: <b>{status_str}</b>\n"
        f"{E_MONEY} Цена: <b>{price}</b>\n"
        f"{E_STATS} Остаток: <b>{stock}</b>"
    )
    kb = make_product_keyboard(url, item_id)
    try: await wait_msg.delete()
    except: pass

    if screen:
        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="added.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

# --- Шпион для получения ID премиум-эмодзи ---
@dp.message(F.text)
async def catch_premium_emoji(message: Message):
    if message.entities:
        for ent in message.entities:
            if ent.type == "custom_emoji":
                await message.answer(
                    f"🆔 <b>ID этого смайлика:</b> <code>{ent.custom_emoji_id}</code>\n\n"
                    f"Скопируй эти цифры и вставь их в блок настроек <b>app.py</b>, "
                    f"оставив сам тег <code>&lt;tg-emoji&gt;</code> нетронутым.",
                    parse_mode="HTML"
                )
                return
    await message.answer(f"{E_INFO} Отправь мне ссылку на Ozon или любой премиум-смайлик, чтобы я выдал его ID.")

# --- Обработчики кнопок ---
@dp.callback_query(F.data.startswith("check_now:"))
async def callback_check_now(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    items = user_tracked_items.get(chat_id, [])
    target = next((it for it in items if it["id"] == item_id), None)

    if not target:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await callback.answer("Проверяю...")
    is_avail, name, price, stock, screen, err = await inspect_ozon_page(target["url"])
    target["name"], target["price"], target["status"], target["stock"] = name, price, is_avail, stock
    save_db()

    status_str = f"{E_WARN} Ошибка ({err})" if err else (f"{E_OK} В наличии" if is_avail else f"{E_NO} Нет")
    caption = (
        f"{E_REFRESH} <b>Обновлено:</b>\n"
        f"{E_BOX} <b>{name}</b>\n"
        f"Статус: {status_str} | {E_MONEY} {price} | {E_STATS} {stock}"
    )
    kb = make_product_keyboard(target["url"], item_id)
    if screen:
        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="rech.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("delete_item:"))
async def callback_delete_item(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    user_tracked_items[chat_id] = [it for it in user_tracked_items.get(chat_id, []) if it["id"] != item_id]
    save_db()
    await callback.answer("Удалено", show_alert=True)
    try: await callback.message.delete()
    except: pass

@dp.callback_query(F.data == "show_list_btn")
async def callback_list(callback: CallbackQuery):
    await callback.answer()
    await cmd_list(callback.message)

@dp.callback_query(F.data == "show_stats_btn")
async def callback_stats(callback: CallbackQuery):
    uptime_str = str(datetime.timedelta(seconds=int(time.time() - START_TIME)))
    tracked_count = len(user_tracked_items.get(callback.message.chat.id, []))
    text = (
        f"{E_STATS} <b>Панель управления:</b>\n\n"
        f"{E_CLOCK} Аптайм: <code>{uptime_str}</code>\n"
        f"{E_BOX} Ваших товаров: <code>{tracked_count}</code>\n"
        f"{E_REFRESH} Всего проверок: <code>{TOTAL_CHECKS_COUNT}</code>\n"
        f"{E_SHIELD} IP Прокси: <code>{PROXY_IP}</code>"
    )
    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "clear_all_btn")
async def callback_clear_all(callback: CallbackQuery):
    user_tracked_items[callback.message.chat.id] = []
    save_db()
    await callback.answer("Очищено", show_alert=True)
    await callback.message.edit_text(f"{E_TRASH} Список пуст.")

@dp.callback_query(F.data == "check_all_btn")
async def callback_check_all(callback: CallbackQuery):
    await callback.answer("Запускаю фоновый обход. Результаты придут в чат.", show_alert=True)

if __name__ == "__main__":
    async def main():
        load_db()
        print("Бот готов к работе!")
        await dp.start_polling(bot, drop_pending_updates=True)
    asyncio.run(main())
