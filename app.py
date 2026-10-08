"""
OZON TRACKER v4.5 (Stealth Edition + Mobile/PC Switch)
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
PAGE_TIMEOUT_MS = 25_000
PARSE_WAIT_SEC = 20
HARD_TIMEOUT_SEC = 80
ALERT_AFTER_FAILS = 6

if not BOT_TOKEN:
    sys.exit("Задайте BOT_TOKEN")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

START_TIME = time.time()
TOTAL_CHECKS = 0
user_tracked_items: Dict[int, List[dict]] = {}
user_settings: Dict[int, dict] = {}  # chat_id -> {"mobile": bool}
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
        "price_value": None, "status": None, "image": None, "checked": None,
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
    """Усовершенствованный пул браузера со сверхмощной маскировкой (Anti-Detect / Stealth v2)"""
    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._fails = 0
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if self._browser and self._browser.is_connected():
            return self._browser
        self._context = None
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
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-position=0,0",
                "--ignore-certificate-errors",
                "--ignore-certificate-errors-spki-list",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-service-autorun",
                "--password-store=basic",
                "--use-mock-keychain",
                "--lang=ru-RU,ru",
            ],
        )
        return self._browser

    async def new_context(self, mobile: bool) -> BrowserContext:
        browser = await self._ensure_browser()
        
        if mobile:
            # Мобильный профиль (маскируемся под iPhone / Android)
            viewport = {"width": 390, "height": 844}
            user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
            is_mobile = True
            has_touch = True
        else:
            # Премиум ПК профиль (Windows 10/11 + Chrome)
            viewport = {"width": 1920, "height": 1080}
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            is_mobile = False
            has_touch = False

        context = await browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            is_mobile=is_mobile,
            has_touch=has_touch,
            permissions=["geolocation"],
        )

        # 100000 раз круче: Глубокая маскировка отпечатков (Anti-Bot evasion scripts)
        await context.add_init_script("""
            () => {
                // Скрываем следы WebDriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                
                // Эмулируем плагины браузера
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
                
                // Подделываем параметры железа
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
                
                // Маскируем WebGL vendor/renderer от палева headless-режима
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter(parameter);
                };
            }
        """)
        return context

    async def new_page(self, mobile: bool = False) -> Page:
        async with self._lock:
            context = await self.new_context(mobile)
            return await context.new_page()

    async def report(self, ok: bool) -> None:
        if ok:
            self._fails = 0
            return
        self._fails += 1
        if self._fails >= 3:
            self._fails = 0
            log.info("Зафиксированы частые сбои, обновляем стратегии прокси/запросов")

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
    available: Optional[bool] = None
    image: Optional[str] = None
    error: str = ""
    screenshot: Optional[bytes] = None


LD_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _find_product(node) -> Optional[dict]:
    if isinstance(node, list):
        for n in node:
            r = _find_product(n)
            if r:
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return node
        if "@graph" in node:
            return _find_product(node["@graph"])
    return None


def parse_page(content: str) -> Optional[CheckResult]:
    name = ""
    price: Optional[float] = None
    image = None
    available: Optional[bool] = None

    for raw in LD_RE.findall(content):
        try:
            product = _find_product(json.loads(raw))
        except Exception:
            continue
        if not product:
            continue
        name = str(product.get("name") or "")
        img = product.get("image")
        image = img[0] if isinstance(img, list) and img else (img if isinstance(img, str) else None)
        offers = product.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = to_num(offers.get("price") or offers.get("lowPrice"))
            av = str(offers.get("availability") or "")
            if av:
                available = "InStock" in av
        break

    if not name:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.S | re.I)
        if m:
            name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    if not name:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', content)
        if m:
            name = html.unescape(m.group(1)).strip()
    if not name:
        return None

    name = re.sub(r"\s*купить в интернет-магазине Ozon.*", "", name, flags=re.I).strip()

    if not image:
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', content)
        image = m.group(1) if m else None

    if price is None:
        m = re.search(r"(\d[\d\u00a0\u2009 ]{0,10}\d)\s*₽", html.unescape(content))
        if m:
            price = to_num(re.sub(r"\D", "", m.group(1)))

    if available is None:
        gone = re.search(r"товар закончился|узнать о поступлении|нет в наличии|распродан|сопоставьте пазл", content, re.I)
        available = not gone

    return CheckResult(ok=True, name=name, price_value=price, available=available, image=image)


async def _grab(page: Page, settle: bool) -> Optional[bytes]:
    try:
        if settle:
            try:
                await page.wait_for_load_state("load", timeout=6000)
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return await page.screenshot(type="jpeg", quality=75, timeout=10_000)
    except Exception:
        return None


async def _fetch(url: str, mobile: bool) -> CheckResult:
    # Корректируем поддомен под выбранный режим
    if mobile and "www.ozon.ru" in url:
        url = url.replace("www.ozon.ru", "m.ozon.ru")
    elif not mobile and "m.ozon.ru" in url:
        url = url.replace("m.ozon.ru", "www.ozon.ru")

    page = await pool.new_page(mobile=mobile)
    goto_error = ""
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            goto_error = str(exc).splitlines()[0][:80]

        # Имитация активности человека (скролл и случайные задержки)
        try:
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception:
            pass

        deadline = time.monotonic() + PARSE_WAIT_SEC
        while time.monotonic() < deadline:
            try:
                content = await page.content()
                
                # Если сработал антибот пазл, дадим паузу
                if "сопоставьте пазл" in content.lower():
                    await asyncio.sleep(3.0)
                    continue

                result = parse_page(content)
                if result:
                    result.screenshot = await _grab(page, settle=True)
                    return result
            except Exception:
                pass
            await asyncio.sleep(1)

        return CheckResult(ok=False, error=goto_error or "Ozon заблокировал или не отдал страницу",
                           screenshot=await _grab(page, settle=False))
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
            res = await asyncio.wait_for(_fetch(url, mobile=mobile), HARD_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            res = CheckResult(ok=False, error="таймаут")
        except Exception as exc:
            res = CheckResult(ok=False, error=str(exc).splitlines()[0][:80])
    await pool.report(res.ok)
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

    target = item.get("target")
    if target and pv is not None:
        if pv <= target and not item.get("target_hit"):
            item["target_hit"] = True
            notes.append(f"🎯 <b>Цена достигла цели!</b> {money(pv)} ≤ {money(target)}")
        elif pv > target:
            item["target_hit"] = False
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

    price = f"💰 <b>{money(pv)}</b>"
    if len(hist) >= 2 and pv is not None and hist[-2] and pv != hist[-2]:
        prev = hist[-2]
        pct = (pv - prev) / prev * 100
        if pv < prev:
            price += f"  <s>{money(prev)}</s>  📉 {pct:+.1f}%"
        else:
            price += f"  📈 {pct:+.1f}%"
    lines.append(price)
    lines.append(f"📦 {status_label(item.get('status'))}  |  Режим: <b>{mode_str}</b>")

    target = item.get("target")
    if target:
        if pv is not None and pv <= target:
            lines.append(f"🎯 Цель {money(target)} — ✅ достигнута")
        elif pv is not None:
            lines.append(f"🎯 Цель {money(target)} · ещё −{money(pv - target)}")
        else:
            lines.append(f"🎯 Цель {money(target)}")

    if len(hist) >= 2:
        lines.append(LINE)
        lines.append(f"📊 мин {money(min(hist))} · макс {money(max(hist))}")
        lines.append(f"<code>{sparkline(hist)}</code>")

    lines.append(LINE)
    lines.append(f"🕒 {item.get('checked') or '—'}")
    return "\n".join(lines)


def card_kb(item: dict) -> InlineKeyboardMarkup:
    iid, t = item["id"], item.get("target")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Обновить", f"check:{iid}"), btn(f"🎯 {money(t)}" if t else "🎯 Цель", f"target:{iid}")],
        [btn("📈 История", f"hist:{iid}"), btn("🔔 Уведомления", f"notif:{iid}")],
        [InlineKeyboardButton(text="🔗 Ozon", url=item["url"]), btn("🗑 Удалить", f"delask:{iid}")],
    ])


def notif_kb(item: dict) -> InlineKeyboardMarkup:
    iid = item["id"]
    on = lambda v: "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"📉 Снижение цены {on(item.get('notify_drop'))}", f"tg:{iid}:drop")],
        [btn(f"📦 Наличие {on(item.get('notify_stock'))}", f"tg:{iid}:stock")],
        [btn("◀️ Назад", f"card:{iid}")],
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
    mode_str = "Мобильный (m.ozon.ru)" if cfg.get("mobile") else "ПК (www.ozon.ru)"
    
    text = f"🛒 <b>OZON TRACKER</b> (Stealth v2)\n{LINE}\nПривет, <b>{name}</b>!\n\n"
    if items:
        in_stock = sum(1 for i in items if i.get("status") is True)
        text += f"📦 Товаров: <b>{len(items)}</b>  ·  🟢 в наличии: <b>{in_stock}</b>\n\n"
    text += f"⚙️ Текущий режим парсинга: <b>{mode_str}</b>\n\nПришлите ссылку на товар Ozon для отслеживания."
    return text


HELP_TEXT = (
    "❓ <b>Помощь и маскировка</b>\n"
    f"{LINE}\n"
    "1️⃣ Пришлите ссылку на товар Ozon.\n"
    "2️⃣ Бот откроет её в защищенном Stealth-режиме, сделает скриншот и покажет цену.\n"
    "3️⃣ Если сайт выдаёт капчу, попробуйте переключить режим на <b>Мобильный</b> кнопкой в меню — мобильная версия защищена мягче."
)


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
        elif item.get("image"):
            sent = await bot.send_photo(chat_id, item["image"], caption=text, reply_markup=kb)
    except TelegramBadRequest:
        sent = None
    if sent is None:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
    item["msg_id"], item["has_photo"] = sent.message_id, bool(sent.photo)


async def send_alert(item: dict, notes: List[str], photo: Optional[bytes]) -> None:
    text = ("🔔 " + "\n".join(notes) + f"\n{LINE}\n<b>{esc(item['name'][:120])}</b>\n"
            f"💰 {money(item.get('price_value'))} · 📦 {status_label(item.get('status'))}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть на Ozon", url=item["url"])],
        [btn("📌 Показать карточку", f"open:{item['id']}")],
    ])
    chat_id = item["chat_id"]
    try:
        if photo:
            await bot.send_photo(chat_id, photo_file(photo), caption=text, reply_markup=kb)
            return
    except TelegramBadRequest:
        pass
    await bot.send_message(chat_id, text, reply_markup=kb)


async def run_check(item: dict, notify: bool = True) -> CheckResult:
    async with lock_of(item):
        chat_id = item["chat_id"]
        old_status, old_price = item.get("status"), item.get("price_value")
        res = await check_product(item["url"], chat_id)
        ok = apply_result(item, res)
        if res.screenshot:
            item["shot"] = res.screenshot

        notes: List[str] = []
        title = ""
        if ok:
            notes = build_notes(item, old_status, old_price)
        else:
            title = f"⚠️ <b>Ошибка обновления:</b> {esc(res.error)}"

        await update_card(item, photo=res.screenshot, title=title)

        if notify and notes:
            await send_alert(item, notes, res.screenshot)
        return res


def render_list(chat_id: int, page: int):
    items = user_tracked_items.get(chat_id, [])
    if not items:
        return ("📦 <b>Список пуст</b>\n\nПришлите ссылку на товар Ozon.", get_main_kb(chat_id))

    pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    rows = []
    for it in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        label = f"{status_icon(it.get('status'))} {it['name'][:26]} · {money(it.get('price_value'))}"
        rows.append([btn(label, f"open:{it['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"list:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("➡️", f"list:{page + 1}"))
        rows.append(nav)
    rows.append([btn("🗑 Очистить всё", "clear_ask"), btn("◀️ В меню", "back_to_main_btn")])
    return (f"📋 <b>Мои товары</b> ({len(items)})", InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_tracked_items.setdefault(message.chat.id, [])
    await message.answer(menu_text(message.chat.id, message.from_user), reply_markup=get_main_kb(message.chat.id))


@dp.callback_query(F.data == "toggle_mode")
async def cb_toggle_mode(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    cfg = get_user_config(chat_id)
    cfg["mobile"] = not cfg.get("mobile", False)
    mode_name = "Мобильный (m.ozon.ru)" if cfg["mobile"] else "ПК (www.ozon.ru)"
    await callback.answer(f"Режим изменен на: {mode_name}", show_alert=True)
    await show_text(callback.message, menu_text(chat_id, callback.from_user), get_main_kb(chat_id))


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "close")
async def cb_close(callback: CallbackQuery):
    await callback.answer()
    await delete_message(callback.message.chat.id, callback.message.message_id)


@dp.callback_query(F.data == "back_to_main_btn")
async def cb_back_to_main(callback: CallbackQuery):
    await callback.answer()
    await show_text(callback.message, menu_text(callback.message.chat.id, callback.from_user), get_main_kb(callback.message.chat.id))


@dp.callback_query(F.data == "help_btn")
async def cb_help(callback: CallbackQuery):
    await callback.answer()
    await show_text(callback.message, HELP_TEXT, MENU_KB)


@dp.callback_query(F.data == "show_stats_btn")
async def cb_stats(callback: CallbackQuery):
    await callback.answer()
    cid = callback.message.chat.id
    uptime = str(datetime.timedelta(seconds=int(time.time() - START_TIME)))
    text = f"📊 <b>Статус</b>\n{LINE}\n⏱ Аптайм: {uptime}\n📦 Товаров: {len(user_tracked_items.get(cid, []))}"
    await show_text(callback.message, text, MENU_KB)


@dp.callback_query(F.data.startswith("list:"))
async def cb_list(callback: CallbackQuery):
    await callback.answer()
    text, kb = render_list(callback.message.chat.id, int(callback.data.split(":")[1]))
    await show_text(callback.message, text, kb)


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
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
    await callback.answer("Обновляю…")
    msg, done = callback.message, 0

    async def one(item: dict):
        nonlocal done
        try:
            await run_check(item, notify=True)
        except Exception:
            pass
        done += 1
        try:
            await show_text(msg, f"⏳ Обновляю: {done}/{len(items)}…")
        except Exception:
            pass

    await asyncio.gather(*(one(i) for i in items))
    await show_text(msg, menu_text(chat_id, callback.from_user), get_main_kb(chat_id))


@dp.callback_query(F.data == "clear_ask")
async def cb_clear_ask(callback: CallbackQuery):
    await callback.answer()
    await show_text(callback.message, "🗑 Удалить все товары?", InlineKeyboardMarkup(
        inline_keyboard=[[btn("✅ Да", "clear_yes"), btn("↩️ Отмена", "list:0")]]))


@dp.callback_query(F.data == "clear_yes")
async def cb_clear_yes(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    for it in user_tracked_items.get(chat_id, []):
        await delete_message(chat_id, it.get("msg_id"))
        item_locks.pop(it["id"], None)
    user_tracked_items[chat_id] = []
    await callback.answer("Очищено")
    text, kb = render_list(chat_id, 0)
    await show_text(callback.message, text, kb)


@dp.message(F.text, lambda m: m.chat.id in pending_target and "ozon.ru" not in m.text and not m.text.startswith("/"))
async def handle_target_input(message: Message):
    chat_id = message.chat.id
    item_id, prompt_id = pending_target.pop(chat_id)
    item = find_item(chat_id, item_id)
    if not item:
        await message.answer("Товар не найден.")
        return
    value = parse_money_input(message.text)
    if value is None:
        pending_target[chat_id] = (item_id, prompt_id)
        await message.answer("Введите цену числом, например <code>1500</code>")
        return
    if value == 0:
        item["target"], item["target_hit"] = None, False
        title = "🎯 Цель снята"
    else:
        item["target"] = value
        pv = item.get("price_value")
        item["target_hit"] = pv is not None and pv <= value
        title = f"🎯 <b>Цель установлена:</b> {money(value)}"
    await delete_message(chat_id, prompt_id)
    await delete_message(chat_id, message.message_id)
    await update_card(item, title=title)


@dp.message(F.text.contains("ozon.ru"))
async def handle_url(message: Message):
    chat_id = message.chat.id
    pending_target.pop(chat_id, None)
    urls: List[str] = []
    for m in URL_RE.finditer(message.text):
        u = normalize_url(m.group(0))
        if u not in urls:
            urls.append(u)
    if not urls:
        await message.answer("Не вижу ссылку на Ozon")
        return

    items = user_tracked_items.setdefault(chat_id, [])
    existing = {it["url"] for it in items}
    fresh = [u for u in urls if u not in existing]
    if not fresh:
        await message.answer("ℹ️ Этот товар уже отслеживается.")
        return
    room = MAX_ITEMS_PER_USER - len(items)
    if room <= 0:
        await message.answer(f"⚠️ Лимит — {MAX_ITEMS_PER_USER} товаров.")
        return

    async def add_one(url: str):
        item = new_item(chat_id, url)
        ph = await message.answer("⏳ <b>Открываю Ozon в Stealth-режиме…</b>")
        item["msg_id"] = ph.message_id
        items.append(item)
        try:
            await run_check(item, notify=False)
        except Exception:
            pass

    await asyncio.gather(*(add_one(u) for u in fresh[:room]))


@dp.callback_query(F.data.startswith("check:"))
async def cb_check(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if lock_of(item).locked():
        await callback.answer("Уже проверяю…")
        return
    await callback.answer("Проверяю…")
    try:
        await update_card(item, title="⏳ <b>Обновляю…</b>")
    except Exception:
        pass
    await run_check(item, notify=True)


@dp.callback_query(F.data.startswith("card:"))
async def cb_card(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=card_kb(item))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("notif:"))
async def cb_notif(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=notif_kb(item))


@dp.callback_query(F.data.startswith("tg:"))
async def cb_toggle(callback: CallbackQuery):
    _, item_id, kind = callback.data.split(":")
    item = find_item(callback.message.chat.id, item_id)
    if not item:
        return
    key = "notify_drop" if kind == "drop" else "notify_stock"
    item[key] = not item[key]
    await callback.answer("Готово")
    try:
        await callback.message.edit_reply_markup(reply_markup=notif_kb(item))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("target:"))
async def cb_target(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        return
    await callback.answer()
    cur = f"\nСейчас: <b>{money(item['price_value'])}</b>" if item.get("price_value") else ""
    prompt = await callback.message.answer(
        f"🎯 <b>Целевая цена</b>{cur}\n\nНапишите цену числом:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Отмена", "tcancel")]]),
    )
    pending_target[callback.message.chat.id] = (item["id"], prompt.message_id)


@dp.callback_query(F.data == "tcancel")
async def cb_tcancel(callback: CallbackQuery):
    pending_target.pop(callback.message.chat.id, None)
    await callback.answer("Отменено")
    await delete_message(callback.message.chat.id, callback.message.message_id)


@dp.callback_query(F.data.startswith("hist:"))
async def cb_history(callback: CallbackQuery):
    item = find_item(callback.message.chat.id, callback.data.split(":")[1])
    if not item:
        return
    await callback.answer()
    hist = item["history"]
    if not hist:
        text = "📈 История пока пуста."
    else:
        vals = [h[1] for h in hist]
        lines = [f"📈 <b>История цены</b>\n<b>{esc(item['name'][:100])}</b>\n{LINE}"]
        if len(vals) >= 2:
            lines.append(f"<code>{sparkline(vals)}</code>")
        lines.append(f"Сейчас: <b>{money(vals[-1])}</b>\nМин: {money(min(vals))} · Макс: {money(max(vals))}\n")
        text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=CLOSE_KB)


@dp.callback_query(F.data.startswith("delask:"))
async def cb_delete_ask(callback: CallbackQuery):
    item_id = callback.data.split(":")[1]
    if not find_item(callback.message.chat.id, item_id):
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Удалить", f"del:{item_id}"), btn("↩️ Отмена", f"card:{item_id}")]]))


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    item_id = callback.data.split(":")[1]
    chat_id = callback.message.chat.id
    user_tracked_items[chat_id] = [it for it in user_tracked_items.get(chat_id, []) if it["id"] != item_id]
    item_locks.pop(item_id, None)
    await callback.answer("Удалено")
    await delete_message(chat_id, callback.message.message_id)


async def monitor_loop() -> None:
    await asyncio.sleep(20)
    while True:
        snapshot = [it for items in list(user_tracked_items.values()) for it in list(items)]
        for item in snapshot:
            await asyncio.sleep(random.uniform(0, 4))
            if item not in user_tracked_items.get(item["chat_id"], []) or lock_of(item).locked():
                continue
            try:
                await run_check(item, notify=True)
            except Exception:
                pass
        await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    monitor = asyncio.create_task(monitor_loop())
    log.info("OZON TRACKER Stealth v2 запущен")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        monitor.cancel()
        await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
