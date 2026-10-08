import os
import sys
import asyncio
import datetime
import logging
import re
import html
import time
import json
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
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
DB_FILE = "database.json"

PROXY_IP = "45.81.78.52"             
PROXY_PORT = "8000"           
PROXY_USER = "8BNMZ4"           
PROXY_PASS = "auBcMS"           
PROXY_URL = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_IP}:{PROXY_PORT}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

START_TIME = time.time()
TOTAL_CHECKS_COUNT = 0
user_tracked_items: Dict[int, List[dict]] = {}
monitoring_tasks: Dict[int, asyncio.Task] = {}

def load_db():
    global user_tracked_items
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                user_tracked_items = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass

def save_db():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(user_tracked_items, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

async def get_ozon_data(url: str) -> Tuple[bool, str, str, str, Optional[bytes]]:
    global TOTAL_CHECKS_COUNT
    TOTAL_CHECKS_COUNT += 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": PROXY_URL},
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            locale="ru-RU"
        )
        page = await context.new_page()
        await stealth_async(page)

        try:
            await page.goto(url, wait_until="commit", timeout=12000)
            await asyncio.sleep(2.0)

            content = await page.content()

            # Название товара
            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", content, re.IGNORECASE)
            if not title_match:
                title_match = re.search(r'<meta property="og:title" content="([^"]+)"', content)
            name = title_match.group(1).strip() if title_match else "Товар Ozon"
            name = re.sub(r"купить в интернет-магазине Ozon.*", "", name, flags=re.IGNORECASE).strip()

            # Цена товара
            price_match = re.search(r"([\d\s ]+)\s*₽", content)
            price = f"{price_match.group(1).strip()} ₽" if price_match else "Не определена"

            # Проверка наличия
            is_out_of_stock = bool(re.search(r"товар закончился|узнать о поступлении|нет в наличии", content, re.IGNORECASE))
            has_buy_button = bool(re.search(r"в корзину|купить в 1 клик|>купить<", content, re.IGNORECASE))
            is_available = has_buy_button and not is_out_of_stock

            photo_bytes = None
            try:
                photo_bytes = await page.screenshot(type="jpeg", quality=80)
            except:
                pass

            await browser.close()
            return is_available, name, price, ("В наличии" if is_available else "Нет в наличии"), photo_bytes

        except Exception as exc:
            await browser.close()
            return False, "Ошибка загрузки", "—", str(exc)[:30], None

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
                is_avail, name, price, stock, _ = await get_ozon_data(item["url"])
                old_status = item.get("status")
                
                item["name"] = name
                item["price"] = price
                item["status"] = is_avail
                save_db()

                # Уведомление, если товар появился в наличии
                if is_avail is True and old_status is not True:
                    caption = f"🔥 <b>ТОВАР СНОВА В НАЛИЧИИ!</b>\n\n📦 <b>{name}</b>\n🟢 Статус: В наличии\n💰 Цена: <b>{price}</b>"
                    await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=make_product_keyboard(item["url"], item["id"]))

                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(600)

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
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="show_list_btn")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="show_stats_btn")]
    ])

    await message.answer(
        f"🔥 <b>OZON TRACKER</b>\n👋 Привет, <b>{username}</b>!\n\nОтправь ссылку на товар Ozon для отслеживания.",
        parse_mode="HTML",
        reply_markup=kb
    )

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    wait_msg = await message.answer("🔄 Проверяю IP прокси...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy={"server": PROXY_URL})
            page = await browser.new_context().new_page()
            await page.goto("https://api.ipify.org?format=json", timeout=8000)
            ip_data = await page.inner_text("body")
            await wait_msg.edit_text(f"🌐 IP прокси: <code>{ip_data}</code>", parse_mode="HTML")
            await browser.close()
    except Exception as exc:
        await wait_msg.edit_text(f"🔴 Ошибка: {html.escape(str(exc)[:40])}", parse_mode="HTML")

@dp.message(Command("list"))
@dp.callback_query(F.data == "show_list_btn")
async def cmd_list(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    chat_id = message.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()

    items = user_tracked_items.get(chat_id, [])
    if not items:
        text = "📦 Ваш список товаров пуст."
        if isinstance(event, CallbackQuery):
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    text = "📋 <b>Отслеживаемые товары:</b>\n\n"
    for idx, item in enumerate(items, 1):
        price = item.get("price", "—")
        st = item.get("status")
        icon = "🟢 В наличии" if st is True else "🔴 Нет в наличии"
        text += f"{idx}. <b>{item.get('name', 'Товар')}</b>\n   Статус: {icon} | Цена: {price}\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Очистить список", callback_data="clear_all_btn")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main_btn")]
    ])
    
    if isinstance(event, CallbackQuery):
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.message(F.text.regexp(r"https?://(?:www\.)?ozon\.ru/\S+"))
async def handle_url(message: Message):
    url = re.search(r"https?://(?:www\.)?ozon\.ru/\S+", message.text).group(0)
    chat_id = message.chat.id

    if chat_id not in user_tracked_items:
        user_tracked_items[chat_id] = []

    if any(item["url"] == url for item in user_tracked_items[chat_id]):
        await message.answer("ℹ️ Этот товар уже есть в списке.")
        return

    item_id = int(time.time() * 1000) % 1000000
    new_item = {
        "id": item_id, "url": url, "name": "Загрузка...",
        "price": "—", "status": True, "stock": "—"
    }
    user_tracked_items[chat_id].append(new_item)
    save_db()

    if chat_id not in monitoring_tasks or monitoring_tasks[chat_id].done():
        monitoring_tasks[chat_id] = asyncio.create_task(monitoring_worker(chat_id))

    wait_msg = await message.answer("⏳ Получаю данные товара...")
    
    is_avail, name, price, stock, photo_bytes = await get_ozon_data(url)
    new_item["name"] = name
    new_item["price"] = price
    new_item["status"] = is_avail
    new_item["stock"] = stock
    save_db()

    status_str = "🟢 В наличии" if is_avail else "🔴 Нет в наличии"
    caption = f"✅ <b>Товар добавлен</b>\n\n📦 <b>{name}</b>\nСтатус: <b>{status_str}</b>\n💰 Цена: <b>{price}</b>"
    kb = make_product_keyboard(url, item_id)
    try: await wait_msg.delete()
    except: pass

    if photo_bytes:
        await message.answer_photo(BufferedInputFile(photo_bytes, filename="ozon.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=kb)

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
    is_avail, name, price, stock, photo_bytes = await get_ozon_data(target["url"])
    target["name"], target["price"], target["status"], target["stock"] = name, price, is_avail, stock
    save_db()

    status_str = "🟢 В наличии" if is_avail else "🔴 Нет в наличии"
    caption = f"🔄 <b>Обновлено:</b>\n\n📦 <b>{name}</b>\nСтатус: <b>{status_str}</b>\n💰 Цена: <b>{price}</b>"
    kb = make_product_keyboard(target["url"], item_id)

    if photo_bytes:
        await callback.message.answer_photo(BufferedInputFile(photo_bytes, filename="ozon.jpg"), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.answer(caption, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("delete_item:"))
async def callback_delete_item(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    user_tracked_items[chat_id] = [it for it in user_tracked_items.get(chat_id, []) if it["id"] != item_id]
    save_db()
    await callback.answer("Удалено", show_alert=True)
    try: await callback.message.delete()
    except: pass

@dp.callback_query(F.data == "show_stats_btn")
async def callback_stats(callback: CallbackQuery):
    uptime_str = str(datetime.timedelta(seconds=int(time.time() - START_TIME)))
    tracked_count = len(user_tracked_items.get(callback.message.chat.id, []))
    text = f"📊 <b>Статус системы:</b>\n\n⏱ Аптайм: {uptime_str}\n📦 Отслеживаемых товаров: {tracked_count}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main_btn")]])
    await callback.answer()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "clear_all_btn")
async def callback_clear_all(callback: CallbackQuery):
    user_tracked_items[callback.message.chat.id] = []
    save_db()
    await callback.answer("Очищено", show_alert=True)
    await callback.message.edit_text("🗑️ Список товаров пуст.")

@dp.callback_query(F.data == "back_to_main_btn")
async def callback_back_to_main(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    username = f"@{callback.from_user.username}" if callback.from_user.username else message.from_user.first_name

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="show_list_btn")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="show_stats_btn")]
    ])

    await callback.message.edit_text(
        f"🔥 <b>OZON TRACKER</b>\n👋 Привет, <b>{username}</b>!\n\nОтправь ссылку на товар Ozon.",
        parse_mode="HTML",
        reply_markup=kb
    )

if __name__ == "__main__":
    async def main():
        load_db()
        print("OZON TRACKER запущен!")
        await dp.start_polling(bot, drop_pending_updates=True)
    asyncio.run(main())
