"""
OZON TRACKER v5 (Anti-Captcha Direct JSON/HTML Parser)
"""
import asyncio
import datetime
import html
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Moscow")
except Exception:
    TZ = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
log = logging.getLogger("ozon")

# ═════════════════════════ Конфиг ═════════════════════════
BOT_TOKEN = "8930660922:AAG-e6Kn4hGA5UWLLmyk9ttLszuxcRvF0sk"
PROXY_HOST = "45.81.78.52"
PROXY_PORT = "8000"
PROXY_USER = "8BNMZ4"
PROXY_PASS = "auBcMS"
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MAX_ITEMS_PER_USER = 30
MAX_PARALLEL_CHECKS = 2
PAGE_SIZE = 6
HISTORY_LIMIT = 200
PAGE_TIMEOUT_MS = 20_000
HARD_TIMEOUT_MS = 30

if not BOT_TOKEN:
    sys.exit("Задайте BOT_TOKEN")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

START_TIME = time.time()
TOTAL_CHECKS = 0
user_tracked_items: Dict[int, List[dict]] = {}
user_settings: Dict[int, dict] = {}
pending_target: Dict[int, Tuple[str, int]] = {}
item_locks: Dict[str, asyncio.Lock] = {}
check_sem = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

URL_RE = re.compile(r"https?://(?:[\w-]+\.)?ozon\.ru/[^\s]+", re.IGNORECASE)
BARS = "▁▂▃▄▅▆▇█"
LINE = "━━━━━━━━━━━━━━"


def esc(s) -> str:
    return html.escape(str(s))


def money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{int(round(v)):,}".replace(",", "\u00a0") + "\u00a0₽"


def to_num(value) -> Optional[float]:
    try:
        return float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_money_input(text: str) -> Optional[float]:
    return to_num(re.sub(r"[^\d.,]", "", text))


def now_str() -> str:
    return datetime.datetime.now(TZ).strftime("%d.%m %H:%M")


def fmt_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def sparkline(values: List[float], width: int = 24) -> str:
    if len(values) < 2:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi == lo:
        return BARS[3] * len(values)
    return "".join(BARS[int((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in values)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def photo_file(data: bytes) -> BufferedInputFile:
    return BufferedInputFile(data, filename="screen.jpg")


def normalize_url(raw: str) -> str:
    url = raw.rstrip(".,;)>»")
    return url.split("?")[0].split("#")[0]


def get_user_config(chat_id: int) -> dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = {"mobile": False}
    return user_settings[chat_id]


CLOSE_KB = InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Закрыть", "close")]])
MENU_KB = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В меню", "back_to_main_btn")]])


def new_item(chat_id: int, url: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:8], "chat_id": chat_id, "url": url, "name": "Товар Ozon",
        "price_value": None, "status": True, "image": None, "checked": None,
        "fails": 0, "fail_notified": False, "target": None, "target_hit": False,
        "notify_drop": True, "notify_stock": True, "history": [],
        "msg_id": None, "has_photo": False, "shot": None,
    }


def find_item(chat_id: int, item_id: str) -> Optional[dict]:
    return next((it for it in user_tracked_items.get(chat_id, []) if it["id"] == item_id), None)


def lock_of(item: dict) -> asyncio.Lock:
    return item_locks.setdefault(item["id"], asyncio.Lock())


async def access_guard(handler, event, data):
    user = data.get("event_from_user")
    if ALLOWED_USERS and (user is None or user.id not in ALLOWED_USERS):
        return None
    return await handler(event, data)


dp.update.outer_middleware(access_guard)


class BrowserPool:
    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if self._browser and self._browser.is_connected():
            return self._browser
        if self._pw is None:
            self._pw = await async_playwright().start()
        proxy = None
        if PROXY_HOST:
            proxy = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
            if PROXY_USER:
                proxy["username"] = PROXY_USER
                proxy["password"] = PROXY_PASS
        self._browser = await self._pw.chromium.launch(
            headless=True, proxy=proxy,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-infobars",
                "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--disable-gpu", "--lang=ru-RU,ru"
            ],
        )
        return self._browser

    async def new_page(self, mobile: bool) -> Page:
        async with self._lock:
            browser = await self._ensure_browser()
            viewport = {"width": 390, "height": 844} if mobile else {"width": 1920, "height": 1080}
            user_agent = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 "
                          "(KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1") if mobile else \
                         ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
            
            context = await browser.new_context(
                viewport=viewport, user_agent=user_agent, locale="ru-RU",
                timezone_id="Europe/Moscow", is_mobile=mobile, has_touch=mobile
            )
            return await context.new_page()

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


pool = BrowserPool()


@dataclass
class CheckResult:
    ok: bool
    name: str = ""
    price_value: Optional[float] = None
    available: bool = True
    image: Optional[str] = None
    error: str = ""
    screenshot: Optional[bytes] = None


def parse_html_content(content: str) -> CheckResult:
    """Умный парсер, извлекающий данные напрямую из текста страницы"""
    name = ""
    price = None
    image = None
    available = True

    # Ищем название в title или og:title
    m_title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', content)
    if m_title:
        name = html.unescape(m_title.group(1))
    else:
        m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.S | re.I)
        if m_h1:
            name = html.unescape(re.sub(r"<[^>]+>", "", m_h1.group(1))).strip()

    name = re.sub(r"\s*купить в интернет-магазине Ozon.*", "", name, flags=re.I).strip()
    if not name:
        name = "Товар Ozon"

    # Ищем картинку
    m_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', content)
    if m_img:
        image = m_img.group(1)

    # Ищем цену (в блоках цен Ozon или через регулярку рубля)
    m_price = re.search(r'"price"\s*:\s*"([0-9\.]+)"', content)
    if m_price:
        price = to_num(m_price.group(1))
    else:
        # Поиск по символу рубля в тексте
        p_match = re.search(r"(\d[\d\u00a0\u2009 ]{0,10}\d)\s*₽", content)
        if p_match:
            price = to_num(re.sub(r"\D", "", p_match.group(1)))

    # Проверка на наличие (если товар кончился или висит заглушка)
    content_lower = content.lower()
    if "товар закончился" in content_lower or "нет в наличии" in content_lower or "узнать о поступлении" in content_lower:
        available = False

    return CheckResult(ok=True, name=name, price_value=price, available=available, image=image)


async def _fetch(url: str, mobile: bool) -> CheckResult:
    if mobile and "www.ozon.ru" in url:
        url = url.replace("www.ozon.ru", "m.ozon.ru")
    elif not mobile and "m.ozon.ru" in url:
        url = url.replace("m.ozon.ru", "www.ozon.ru")

    page = await pool.new_page(mobile=mobile)
    try:
        await page.goto(url, wait_until="commit", timeout=15_000)
        await asyncio.sleep(2.0) # даем странице отдать базовые скрипты
        
        content = await page.content()
        
        # Скриншот для пользователя (даже если там капча, он увидит срез)
        screenshot = None
        try:
            screenshot = await page.screenshot(type="jpeg", quality=75, timeout=5_000)
        except Exception:
            pass

        res = parse_html_content(content)
        res.screenshot = screenshot
        return res

    except Exception as exc:
        return CheckResult(ok=False, error=str(exc).splitlines()[0][:60])
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def check_product(url: str, chat_id: int) -> CheckResult:
    global TOTAL_CHECKS
    cfg = get_user_config(chat_id)
    mobile = cfg.get("mobile", False)

    async with check_sem:
        TOTAL_CHECKS += 1
        try:
            res = await asyncio.wait_for(_fetch(url, mobile=mobile), HARD_TIMEOUT_MS)
        except asyncio.TimeoutError:
            res = CheckResult(ok=False, error="таймаут")
        except Exception as exc:
            res = CheckResult(ok=False, error=str(exc).splitlines()[0][:60])
    return res


def apply_result(item: dict, res: CheckResult) -> bool:
    if not res.ok:
        item["fails"] += 1
        return False
    item.update(name=res.name, status=res.available, image=res.image or item.get("image"),
                checked=now_str(), fails=0, fail_notified=False)
    if res.price_value is not None:
        item["price_value"] = res.price_value
        hist = item["history"]
        if not hist or hist[-1][1] != res.price_value:
            hist.append([int(time.time()), res.price_value])
            del hist[:-HISTORY_LIMIT]
    return True


def build_notes(item: dict, old_status: Optional[bool], old_price: Optional[float]) -> List[str]:
    notes = []
    st = item.get("status")
    if item.get("notify_stock"):
        if old_status is False and st is True:
            notes.append("🟢 <b>Появился в наличии!</b>")
        elif old_status is True and st is False:
            notes.append("🔴 <b>Закончился</b>")

    pv = item.get("price_value")
    if pv is not None and old_price and pv < old_price and item.get("notify_drop"):
        pct = (pv - old_price) / old_price * 100
        notes.append(f"📉 <b>Цена снизилась:</b> <s>{money(old_price)}</s> → <b>{money(pv)}</b> ({pct:+.1f}%)")
    return notes


def status_icon(st: Optional[bool]) -> str:
    return {True: "🟢", False: "🔴"}.get(st, "⚪")


def status_label(st: Optional[bool]) -> str:
    return {True: "В наличии", False: "Нет в наличии"}.get(st, "Неизвестно")


def card_text(item: dict, chat_id: int, title: str = "") -> str:
    pv = item.get("price_value")
    hist = [h[1] for h in item["history"]]
    cfg = get_user_config(chat_id)
    mode_str = "📱 Мобильный" if cfg.get("mobile") else "💻 ПК"

    lines = []
    if title:
        lines += [title, LINE]
    lines.append(f"{status_icon(item.get('status'))} <b>{esc(item['name'][:120])}</b>")
    lines.append("")
    lines.append(f"💰 <b>{money(pv)}</b>  |  📦 {status_label(item.get('status'))}")
    lines.append(f"⚙️ Режим: <b>{mode_str}</b>")

    if len(hist) >= 2:
        lines.append(LINE)
        lines.append(f"📊 мин {money(min(hist))} · макс {money(max(hist))}")
        lines.append(f"<code>{sparkline(hist)}</code>")

    lines.append(LINE)
    lines.append(f"🕒 {item.get('checked') or '—'}")
    return "\n".join(lines)


def card_kb(item: dict) -> InlineKeyboardMarkup:
    iid = item["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Обновить", f"check:{iid}"), InlineKeyboardButton(text="🔗 Ozon", url=item["url"])],
        [btn("📈 История", f"hist:{iid}"), btn("🗑 Удалить", f"delask:{iid}")],
    ])


def get_main_kb(chat_id: int) -> InlineKeyboardMarkup:
    cfg = get_user_config(chat_id)
    mode_btn_text = "📱 Режим: Мобильный" if cfg.get("mobile") else "💻 Режим: ПК"
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📋 Мои товары", "list:0"), btn("🔄 Обновить все", "refresh_all")],
        [btn(mode_btn_text, "toggle_mode")],
        [btn("📊 Статус", "show_stats_btn"), btn("❓ Помощь", "help_btn")],
    ])


def menu_text(chat_id: int, user) -> str:
    items = user_tracked_items.get(chat_id, [])
    name = esc(f"@{user.username}" if user.username else user.first_name or "друг")
    cfg = get_user_config(chat_id)
    mode_str = "Мобильный" if cfg.get("mobile") else "ПК"
    
    text = f"🛒 <b>OZON TRACKER</b>\n{LINE}\nПривет, <b>{name}</b>!\n\n"
    if items:
        text += f"📦 Товаров в отслеживании: <b>{len(items)}</b>\n\n"
    text += f"⚙️ Режим: <b>{mode_str}</b>\n\nПришлите ссылку на товар Ozon."
    return text


async def show_text(msg: Message, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def delete_message(chat_id: int, msg_id: Optional[int]) -> None:
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def update_card(item: dict, photo: Optional[bytes] = None, title: str = "") -> None:
    chat_id, msg_id = item["chat_id"], item.get("msg_id")
    text, kb = card_text(item, chat_id, title), card_kb(item)

    if msg_id:
        try:
            if photo and item["has_photo"]:
                await bot.edit_message_media(
                    chat_id=chat_id, message_id=msg_id,
                    media=InputMediaPhoto(media=photo_file(photo), caption=text), reply_markup=kb)
                return
            if not photo and item["has_photo"]:
                await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text, reply_markup=kb)
                return
            if not photo and not item["has_photo"]:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
        await delete_message(chat_id, msg_id)

    photo = photo or item.get("shot")
    sent = None
    try:
        if photo:
            sent = await bot.send_photo(chat_id, photo_file(photo), caption=text, reply_markup=kb)
    except TelegramBadRequest:
        sent = None
    if sent is None:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
    item["msg_id"], item["has_photo"] = sent.message_id, bool(sent.photo)


async def run_check(item: dict, notify: bool = True) -> CheckResult:
    async with lock_of(item):
        chat_id = item["chat_id"]
        old_status, old_price = item.get("status"), item.get("price_value")
        res = await check_product(item["url"], chat_id)
        apply_result(item, res)
        if res.screenshot:
            item["shot"] = res.screenshot

        await update_card(item, photo=res.screenshot)
        return res


def render_list(chat_id: int, page: int):
    items = user_tracked_items.get(chat_id, [])
    if not items:
        return ("📦 <b>Список пуст</b>", get_main_kb(chat_id))
    rows = [[btn(f"{status_icon(it.get('status'))} {it['name'][:24]} · {money(it.get('price_value'))}", f"open:{it['id']}")] for it in items[:PAGE_SIZE]]
    rows.append([btn("🗑 Очистить", "clear_ask"), btn("◀️ В меню", "back_to_main_btn")])
    return ("📋 <b>Ваши товары:</b>", InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_tracked_items.setdefault(message.chat.id, [])
    await message.answer(menu_text(message.chat.id, message.from_user), reply_markup=get_main_kb(message.chat.id))


@dp.callback_query(F.data == "toggle_mode")
async def cb_toggle_mode(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    cfg = get_user_config(chat_id)
    cfg["mobile"] = not cfg.get("mobile", False)
    mode_name = "Мобильный" if cfg["mobile"] else "ПК"
    await callback.answer(f"Режим: {mode_name}", show_alert=True)
    await show_text(callback.message, menu_text(chat_id, callback.from_user), get_main_kb(chat_id))


@dp.callback_query(F.data == "back_to_main_btn")
async def cb_back_to_main(callback: CallbackQuery):
    await callback.answer()
    await show_text(callback.message, menu_text(callback.message.chat.id, callback.from_user), get_main_kb(callback.message.chat.id))


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        await callback.answer("Не найдено", show_alert=True)
        return
    await callback.answer()
    await delete_message(item["chat_id"], item.get("msg_id"))
    item["msg_id"] = None
    await update_card(item)


@dp.callback_query(F.data == "refresh_all")
async def cb_refresh_all(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    items = list(user_tracked_items.get(chat_id, []))
    if not items:
        await callback.answer("Список пуст", show_alert=True)
        return
    await callback.answer("Обновление...")
    for item in items:
        try:
            await run_check(item, notify=False)
        except Exception:
            pass
    await show_text(callback.message, menu_text(chat_id, callback.from_user), get_main_kb(chat_id))


@dp.message(F.text.contains("ozon.ru"))
async def handle_url(message: Message):
    chat_id = message.chat.id
    urls = [normalize_url(m.group(0)) for m in URL_RE.finditer(message.text)]
    if not urls:
        return

    items = user_tracked_items.setdefault(chat_id, [])
    for u in urls:
        if any(i["url"] == u for i in items):
            continue
        item = new_item(chat_id, u)
        ph = await message.answer("⏳ <b>Получаю данные товара...</b>")
        item["msg_id"] = ph.message_id
        items.append(item)
        try:
            await run_check(item, notify=False)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("check:"))
async def cb_check(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        return
    await callback.answer("Обновляю...")
    await run_check(item, notify=False)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    item_id = callback.data.split(":")[1]
    chat_id = callback.message.chat.id
    user_tracked_items[chat_id] = [it for it in user_tracked_items.get(chat_id, []) if it["id"] != item_id]
    item_locks.pop(item_id, None)
    await callback.answer("Удалено")
    await delete_message(chat_id, callback.message.message_id)


async def monitor_loop() -> None:
    await asyncio.sleep(30)
    while True:
        snapshot = [it for items in list(user_tracked_items.values()) for it in list(items)]
        for item in snapshot:
            await asyncio.sleep(random.uniform(1, 3))
            try:
                await run_check(item, notify=True)
            except Exception:
                pass
        await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    monitor = asyncio.create_task(monitor_loop())
    log.info("OZON TRACKER v5 запущен")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        monitor.cancel()
        await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
