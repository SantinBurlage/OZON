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
from playwright.async_api import async_playwright, BrowserContext
from playwright_stealth import stealth_async

# ==========================================
# ⚙️ НАСТРОЙКИ БОТА И ПРОКСИ
# ==========================================
BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
CHECK_INTERVAL_SECONDS = 600
DB_FILE = "database.json"
USER_DATA_DIR = "browser_profile"

# ВЫБОР РЕЖИМА: True = мобильная версия (m.ozon.ru), False = полная десктопная версия (www.ozon.ru)
USE_MOBILE_VERSION = False 

# Твой прокси от Proxy6
PROXY_TYPE = "http"       
PROXY_IP = "45.81.78.52"             
PROXY_PORT = "8000"           
PROXY_USER = "8BNMZ4"           
PROXY_PASS = "auBcMS"           
# ==========================================

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

persistent_context: Optional[BrowserContext] = None
browser_lock = asyncio.Semaphore(1)

START_TIME = time.time()
TOTAL_CHECKS_COUNT = 0
user_tracked_items: Dict[int, List[dict]] = {}
monitoring_tasks: Dict[int, asyncio.Task] = {}

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

def prepare_url(url: str) -> str:
    if USE_MOBILE_VERSION:
        return url.replace("www.ozon.ru", "m.ozon.ru").replace("ozon.ru", "m.ozon.ru")
    else:
        return url.replace("m.ozon.ru", "www.ozon.ru")

async def get_persistent_context() -> BrowserContext:
    global persistent_context
    if persistent_context is None:
        p = await async_playwright().start()
        
        proxy_conf = {
            "server": f"{PROXY_TYPE}://{PROXY_IP}:{PROXY_PORT}",
            "username": PROXY_USER,
            "password": PROXY_PASS
        }
        
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        
        ua = MOBILE_UA if USE_MOBILE_VERSION else DESKTOP_UA
        vp = {"width": 390, "height": 844} if USE_MOBILE_VERSION else {"width": 1920, "height": 1080}
        
        persistent_context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=True,
            proxy=proxy_conf,
            user_agent=ua,
            viewport=vp,
            device_scale_factor=3 if USE_MOBILE_VERSION else 1,
            is_mobile=USE_MOBILE_VERSION,
            has_touch=USE_MOBILE_VERSION,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--lang=ru-RU,ru",
            ],
        )
    return persistent_context

async def inspect_ozon_page(url: str) -> Tuple[Optional[bool], str, str, str, Optional[bytes], Optional[str]]:
    global TOTAL_CHECKS_COUNT
    TOTAL_CHECKS_COUNT += 1

    target_url = prepare_url(url)

    async with browser_lock:
        ctx = await get_persistent_context()
        page = await ctx.new_page()
        
        await stealth_async(page)
        
        try:
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            
            await asyncio.sleep(random.uniform(2.0, 3.5))
            await page.mouse.wheel(0, random.randint(300, 600))
            await asyncio.sleep(1.0)

            content = await page.content()

            if re.search(r"проверка безопасности|captcha|access denied|cloudflare", content, re.IGNORECASE):
                screen = await page.screenshot(type="jpeg", quality=75)
                return None, "Неизвестно", "—", "Капча", screen, "Блокировка / Капча Ozon"

            if response and response.status in (403, 429, 500, 502, 503):
                screen = await page.screenshot(type="jpeg", quality=75)
                return None, "Неизвестно", "—", "Сбой HTTP", screen, f"Код ответа {response.status}"

            # Парсинг названия
            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", content, re.IGNORECASE)
            if not title_match:
                title_match = re.search(r"<span[^>]*class=\"[^\"]*title[^\"]*\"[^>]*>([^<]+)</span>", content, re.IGNORECASE)
            item_name = title_match.group(1).strip() if title_match else "Товар Ozon"

            # Парсинг цены
            price_match = re.search(r"([\d\s ]+)\s*₽", content)
            price_text = f"{price_match.group(1).strip()} ₽" if price_match else "Не определена"

            is_out_of_stock = bool(re.search(r"товар закончился|узнать о поступлении|нет в наличии", content, re.IGNORECASE))
            has_buy_button = bool(re.search(r"в корзину|купить в 1 клик|добавить в корзину|>купить<", content, re.IGNORECASE))

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
                        caption = f"⚠️ Сбой проверки:\n📦 {name}\n⚠️ {err}"
                        kb = make_product_keyboard(item["url"], item["id"])
                        if screen:
                            await bot.send_photo(chat_id, BufferedInputFile(screen, filename="err.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
                    continue

                item["last_error"] = None
                kb = make_product_keyboard(item["url"], item["id"])
                
                if is_avail is True and old_status is not True:
                    caption = (
                        f"🔥 <b>ТОВАР В НАЛИЧИИ!</b> 🔥\n\n"
                        f"📦 <b>{name}</b>\n"
                        f"💰 Цена: <b>{price}</b>\n"
                        f"📊 Остаток: {stock}"
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
                            f"📉 <b>СНИЖЕНИЕ ЦЕНЫ!</b>\n\n"
                            f"📦 <b>{name}</b>\n"
                            f"💰 Было: {old_price} ➔ <b>Стало: {price}</b>\n"
                            f"📉 <b>Выгода: {diff} ₽</b>"
                        )
                        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="sale.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)

                await asyncio.sleep(random.uniform(5.0, 10.0))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.error(f"Worker err: {exc}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS + random.uniform(10.0, 30.0))

@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    if chat_id not in user_tracked_items:
        user_tracked_items[chat_id] = []
        save_db()
        
    if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
        monitoring_tasks[chat_id] = asyncio.create_task(monitoring_worker(chat_id))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мой список", callback_data="show_list_btn"), InlineKeyboardButton(text="📊 Статус", callback_data="show_stats_btn")]
    ])

    mode_str = "Мобильный (m.ozon.ru)" if USE_MOBILE_VERSION else "Десктопный (www.ozon.ru)"

    await message.answer(
        f"🔥 <b>OZON TRACKER</b> ✨\n"
        f"👋 Привет, <b>{username}</b>!\n"
        f"🛡 Прокси: <code>{PROXY_IP}</code>\n"
        f"💻 Режим: <b>{mode_str}</b>\n\n"
        f"Отправь ссылку на товар для отслеживания.",
        parse_mode="HTML",
        reply_markup=kb
    )

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    wait_msg = await message.answer("🔄 Запрашиваю сетевой IP...")
    ctx = await get_persistent_context()
    page = await ctx.new_page()
    await stealth_async(page)
    try:
        await page.goto("https://api.ipify.org?format=json", wait_until="commit", timeout=20000)
        await asyncio.sleep(1)
        ip_data = await page.inner_text("body")
        await wait_msg.edit_text(f"🌐 <b>Выходной IP:</b>\n<code>{ip_data}</code>", parse_mode="HTML")
    except Exception as exc:
        await wait_msg.edit_text(f"🔴 <b>Ошибка:</b> {html.escape(str(exc))}", parse_mode="HTML")
    finally:
        await page.close()

@dp.message(Command("list"))
async def cmd_list(message: Message):
    chat_id = message.chat.id
    items = user_tracked_items.get(chat_id, [])
    if not items:
        await message.answer("📦 Ваш список пуст. Отправьте ссылку!")
        return

    text = "📋 <b>Ваши товары:</b>\n\n"
    for idx, item in enumerate(items, 1):
        st = item.get("status")
        icon = "🟢" if st is True else ("🔴" if st is False else "⚪")
        price = item.get("price", "—")
        text += f"{idx}. {icon} <b>{item.get('name', 'Загрузка...')}</b>\n"
        text += f"💰 Цена: {price} | 🔗 <a href='{item['url']}'>Ссылка</a>\n\n"

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
        await message.answer("ℹ️ Этот товар уже есть в базе.")
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

    wait_msg = await message.answer("🎯 Загрузка данных товара...")
    
    is_avail, name, price, stock, screen, err = await inspect_ozon_page(url)
    new_item["name"] = name
    new_item["price"] = price
    new_item["status"] = is_avail
    new_item["stock"] = stock
    save_db()

    status_str = f"⚠️ Ошибка ({err})" if err else ("🟢 В наличии" if is_avail else "🔴 Закончился")
    caption = (
        f"✅ <b>Успешно добавлено</b>\n\n"
        f"📦 <b>{name}</b>\n"
        f"Статус: <b>{status_str}</b>\n"
        f"💰 Цена: <b>{price}</b>\n"
        f"📊 Остаток: <b>{stock}</b>"
    )
    kb = make_product_keyboard(url, item_id)
    try: await wait_msg.delete()
    except: pass

    if screen:
        await bot.send_photo(chat_id, BufferedInputFile(screen, filename="added.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

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

    status_str = f"⚠️ Ошибка ({err})" if err else ("🟢 В наличии" if is_avail else "🔴 Нет")
    caption = (
        f"🔄 <b>Обновлено:</b>\n"
        f"📦 <b>{name}</b>\n"
        f"Статус: {status_str} | 💰 {price} | 📊 {stock}"
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
    mode_str = "Мобильный" if USE_MOBILE_VERSION else "Десктоп"
    text = (
        f"📊 <b>Панель управления:</b>\n\n"
        f"⏱ Аптайм: <code>{uptime_str}</code>\n"
        f"📦 Ваших товаров: <code>{tracked_count}</code>\n"
        f"💻 Режим: <b>{mode_str}</b>\n"
        f"🛡 IP Прокси: <code>{PROXY_IP}</code>"
    )
    await callback.answer()
    await callback.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "clear_all_btn")
async def callback_clear_all(callback: CallbackQuery):
    user_tracked_items[callback.message.chat.id] = []
    save_db()
    await callback.answer("Очищено", show_alert=True)
    await callback.message.edit_text("🗑️ Список пуст.")

@dp.callback_query(F.data == "check_all_btn")
async def callback_check_all(callback: CallbackQuery):
    await callback.answer("Запускаю фоновый обход. Результаты придут в чат.", show_alert=True)

if __name__ == "__main__":
    async def main():
        load_db()
        print("Бот готов к работе!")
        await dp.start_polling(bot, drop_pending_updates=True)
    asyncio.run(main())
