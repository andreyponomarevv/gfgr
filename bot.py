import asyncio
import html
import logging
import os
import re
import shutil
import time
from statistics import median

import aiohttp
import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()
load_dotenv("config.env")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
CHANNEL = os.getenv("CHANNEL", "@cehrol")  # обязательная подписка
CHANNEL_URL = f"https://t.me/{CHANNEL.lstrip('@')}"
DEFAULT_MARKUP = 15.0  # % по умолчанию, меняется по категориям в админке
DEFAULT_MIN_FEE = 7000  # минимальная стоимость услуги доставки в рублях

# Премиум-эмодзи для кнопок главного меню (нужен Telegram Premium у аккаунта бота).
# Узнать id: зайти в Админ-панель и прислать боту сообщение с нужным эмодзи.
# Пока пусто — на кнопках используются обычные emoji из текста.
ICON_CATALOG = os.getenv("ICON_CATALOG") or None
ICON_ORDER = os.getenv("ICON_ORDER") or None
ICON_MY = os.getenv("ICON_MY") or None
ICON_SUPPORT = os.getenv("ICON_SUPPORT") or None
DB = "bot.db"

esc = html.escape
STATUS = {"new": "На рассмотрении", "work": "В работе", "no": "Отклонён"}
TABLES = {
    "products": "Товары каталога",
    "categories": "Категории доставки",
    "routes": "Направления",
    "flights": "Рейсы",
}

# <<PRICE-CONFIG>>
def _rx(p):
    return re.compile(p, re.I)


# Где искать цену для направления (страна закупки товара)
SRC = {
    "ae": {"flag": "🇦🇪", "name": "🇦🇪 ОАЭ — Dubai Mall и магазины Дубая", "cur": "AED", "region": "wt-wt",
           "fmt": "us", "fb": 25.0,
           "queries": ["{item} price UAE AED Dubai Mall", "{item} price Dubai noon amazon.ae sharaf dg"],
           "rx": _rx(r"(?:AED|Dhs?\.?|د\.إ)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:AED|Dhs?\b)")},
    "cn": {"flag": "🇨🇳", "name": "🇨🇳 Китай — крупные торговые центры", "cur": "CNY", "region": "wt-wt",
           "fmt": "us", "fb": 12.0,
           "queries": ["{item} price China RMB shopping mall official store", "{item} 价格 官方旗舰店 京东 天猫 ¥"],
           "rx": _rx(r"(?:¥|￥|RMB|CNY)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:元|RMB|CNY)")},
    "tr": {"flag": "🇹🇷", "name": "🇹🇷 Турция — крупные магазины и ТЦ", "cur": "TRY", "region": "tr-tr",
           "fmt": "eu", "fb": 2.5,
           "queries": ["{item} fiyat TL trendyol hepsiburada teknosa mediamarkt", "{item} fiyatı Türkiye TL"],
           "rx": _rx(r"(?:₺|TL|TRY)\s?(\d[\d.]*(?:,\d+)?)|(\d[\d.]*(?:,\d+)?)\s?(?:₺|TL\b|TRY)")},
    "ru": {"flag": "🇷🇺", "name": "🇷🇺 Россия — магазины Москвы", "cur": "RUB", "region": "ru-ru",
           "fmt": "ru", "fb": 1.0,
           "queries": ["{item} цена купить Москва ₽ dns citilink ozon", "{item} цена в рублях"],
           "rx": _rx(r"(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s?(?:₽|руб)")},
}


def _num(s, style):
    if style == "us":
        s = s.replace(",", "")
    elif style == "eu":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = re.sub(r"[\s\u00a0,]", "", s)
    return float(s)


def guess_src(title):
    """Угадываем страну закупки по началу названия направления («Китай → Москва» → Китай)."""
    origin = title.split("→")[0].lower()
    if "москв" in origin or "росси" in origin:
        return "ru"
    if "кита" in origin:
        return "cn"
    if "турц" in origin:
        return "tr"
    return "ae"
# <<END-PRICE-CONFIG>>


# ───────────────────────── UI helpers ─────────────────────────
def B(text, cb=None, style=None, url=None, icon=None):
    """Кнопка. style: primary (синяя) / success (зелёная) / danger (красная) — Bot API 9.4.
    icon: custom_emoji_id премиум-эмодзи перед текстом (нужен Premium у аккаунта бота)."""
    kw = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style
    if icon:
        kw["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def support_url():
    if SUPPORT:
        return f"https://t.me/{SUPPORT}"
    return f"tg://user?id={next(iter(ADMINS))}" if ADMINS else "https://t.me/telegram"


def chat_url(user_id, username):
    return f"https://t.me/{username}" if username else f"tg://user?id={user_id}"


def user_link(u):
    s = f'<a href="tg://user?id={u.id}">{esc(u.full_name)}</a>'
    return s + (f" (@{u.username})" if u.username else "")


def rub(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def main_kb(uid):
    rows = [
        [B("Каталог", "catalog", "primary", icon=ICON_CATALOG), B("Заказать доставку", "order", "primary", icon=ICON_ORDER)],
        [B("Мои заказы", "my", "primary", icon=ICON_MY), B("Поддержка", url=support_url(), style="danger", icon=ICON_SUPPORT)],
    ]
    if uid in ADMINS:
        rows.append([B("Админ-панель", "admin", "danger")])
    return kb(*rows)


WELCOME = (
    "<b>Личная доставка</b>\n"
    "Привозим товары из-за границы и обратно — лично, без посредников.\n\n"
    "<blockquote>Товар оплачивается сразу, услуга доставки — при получении.</blockquote>\n\n"
    "Выберите раздел:"
)


async def show(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=markup)
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=markup)


# ───────────────────────── DB ─────────────────────────
async def q(sql, args=(), one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        if commit:
            await db.commit()
            return cur.lastrowid
        rows = await cur.fetchall()
        return (rows[0] if rows else None) if one else rows


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT);
            CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, photo TEXT, title TEXT, description TEXT, price INTEGER);
            CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, markup REAL DEFAULT 15, min_fee INTEGER DEFAULT 7000);
            CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, src TEXT DEFAULT 'ae');
            CREATE TABLE IF NOT EXISTS flights(id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER, date TEXT);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
                route TEXT, flight TEXT, category TEXT, item TEXT, price TEXT, status TEXT DEFAULT 'new');
            """
        )
        # миграция старой базы (данные не теряются)
        for sql in ("ALTER TABLE products ADD COLUMN price INTEGER",
                    "ALTER TABLE categories ADD COLUMN markup REAL DEFAULT 15",
                    "ALTER TABLE categories ADD COLUMN min_fee INTEGER DEFAULT 7000"):
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass
        try:
            await db.execute("ALTER TABLE routes ADD COLUMN src TEXT DEFAULT 'ae'")
            for rid, title in await (await db.execute("SELECT id, title FROM routes")).fetchall():
                await db.execute("UPDATE routes SET src=? WHERE id=?", (guess_src(title), rid))
        except aiosqlite.OperationalError:
            pass
        if not (await (await db.execute("SELECT 1 FROM categories")).fetchall()):
            await db.executemany("INSERT INTO categories(title) VALUES(?)",
                                 [("💍 Драгоценности",), ("📱 Техника",), ("💊 Лекарства",)])
        if not (await (await db.execute("SELECT 1 FROM routes")).fetchall()):
            await db.executemany("INSERT INTO routes(title, src) VALUES(?,?)",
                                 [("Дубай → Москва", "ae"), ("Москва → Дубай", "ru")])
        await db.commit()


async def upsert_user(u):
    await q(
        "INSERT INTO users(id,username,name) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (u.id, u.username, u.full_name), commit=True,
    )


async def notify_admins(bot, text, markup=None):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception as e:
            log.warning("admin %s notify failed: %s", a, e)


# ───────────────────────── Обязательная подписка ─────────────────────────
_sub_cache = {}
_warned = False


async def is_subscribed(bot, uid):
    global _warned
    if uid in ADMINS or _sub_cache.get(uid, 0) > time.time():
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        ok = m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and getattr(m, "is_member", False))
    except Exception as e:
        log.warning("subscription check failed: %s", e)
        if not _warned:
            _warned = True
            await notify_admins(bot, f"⚠️ Не могу проверить подписку на {esc(CHANNEL)}.\n"
                                     "Добавьте бота администратором в канал — пока проверка отключена.")
        return True
    if ok:
        _sub_cache[uid] = time.time() + 300
    return ok


class SubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if u is None or u.is_bot:
            return await handler(event, data)
        await upsert_user(u)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        if await is_subscribed(data["bot"], u.id):
            return await handler(event, data)
        text = ("🔒 <b>Подпишитесь на наш канал</b>\n\n"
                "Чтобы пользоваться ботом, нужно быть подписанным на канал. "
                "Подпишитесь и нажмите «Я подписался».")
        markup = kb([B("Подписаться", url=CHANNEL_URL, style="primary")],
                    [B("Я подписался", "check_sub", "success")])
        if isinstance(event, CallbackQuery):
            await event.answer()
            await event.message.answer(text, reply_markup=markup)
        else:
            await event.answer(text, reply_markup=markup)


# ───────────────────────── Цена в стране закупки ─────────────────────────
async def get_rate(cur):
    fb = next(v["fb"] for v in SRC.values() if v["cur"] == cur)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://open.er-api.com/v6/latest/{cur}") as r:
                return float((await r.json())["rates"]["RUB"])
    except Exception:
        return fb


def _search_prices(item, cfg):
    found = []
    with DDGS() as d:
        for tpl in cfg["queries"]:
            try:
                for r in d.text(tpl.format(item=item), region=cfg["region"], max_results=10):
                    text = f"{r.get('title', '')} {r.get('body', '')}"
                    for m in cfg["rx"].finditer(text):
                        raw = next((g for g in m.groups() if g), None)
                        try:
                            v = _num(raw, cfg["fmt"])
                        except (ValueError, TypeError):
                            continue
                        if v >= 20:
                            found.append(v)
            except Exception as e:
                log.warning("search failed: %s", e)
    return found


async def estimate_price(item, src="ae", pct=DEFAULT_MARKUP, min_fee=DEFAULT_MIN_FEE):
    """Бесплатный поиск (DuckDuckGo): цены из выдачи по магазинам страны закупки → медиана →
    курс к рублю → стоимость товара. Услуга доставки = максимум из (% от цены товара, минимальная
    сумма услуги). Возвращает (итоговая сумма, цена товара, стоимость услуги) в рублях, или None."""
    cfg = SRC.get(src, SRC["ae"])
    try:
        prices = await asyncio.to_thread(_search_prices, item, cfg)
        if len(prices) < 2:
            return None
        med = median(prices)
        good = [p for p in prices if 0.5 * med <= p <= 2 * med]
        local = median(good or prices)
        rate = 1.0 if cfg["cur"] == "RUB" else await get_rate(cfg["cur"])
        item_rub = local * rate
        fee = max(item_rub * pct / 100, min_fee)
        total = int(round((item_rub + fee) / 100) * 100)
        return total, int(round(item_rub / 100) * 100), int(round(fee / 100) * 100)
    except Exception as e:
        log.warning("price search failed: %s", e)
        return None


# ───────────────────────── FSM ─────────────────────────
class OrderFlow(StatesGroup):
    item = State()
    confirm = State()


class AddProduct(StatesGroup):
    photo = State()
    title = State()
    price = State()
    desc = State()


class AddSimple(StatesGroup):
    title = State()


class EditMarkup(StatesGroup):
    value = State()


class EditMinFee(StatesGroup):
    value = State()


class AddFlight(StatesGroup):
    date = State()


class Broadcast(StatesGroup):
    msg = State()


# ───────────────────────── Клиентская часть ─────────────────────────
user = Router()


@user.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=main_kb(m.from_user.id))


@user.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, WELCOME, main_kb(c.from_user.id))


@user.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery):
    _sub_cache.pop(c.from_user.id, None)
    if await is_subscribed(c.bot, c.from_user.id):
        await show(c, WELCOME, main_kb(c.from_user.id))
    else:
        await c.answer("Вы ещё не подписались на канал", show_alert=True)


@user.callback_query(F.data == "catalog")
async def catalog(c: CallbackQuery):
    items = await q("SELECT * FROM products ORDER BY id DESC")
    if not items:
        return await show(c, "🛍 <b>Каталог</b>\n\nСейчас в наличии ничего нет — загляните позже.",
                          kb([B("Меню", "menu")]))
    await c.answer()
    await c.message.answer("🛍 <b>В наличии на руках</b>")
    for p in items:
        price = f"\n💰 <b>{rub(p['price'])}</b>" if p["price"] else ""
        cap = f"<b>{esc(p['title'])}</b>{price}\n\n{esc((p['description'] or '')[:700])}"
        await c.message.answer_photo(p["photo"], caption=cap,
                                     reply_markup=kb([B("Хочу купить", f"buy:{p['id']}", "success")]))
    await c.message.answer("Это всё, что есть сейчас.", reply_markup=kb([B("Меню", "menu")]))


@user.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    p = await q("SELECT * FROM products WHERE id=?", (int(c.data[4:]),), one=True)
    if not p:
        return await c.answer("Этот товар уже продан", show_alert=True)
    price = f" · {rub(p['price'])}" if p["price"] else ""
    await notify_admins(
        c.bot,
        f"🛒 <b>Заявка на товар из каталога</b>\n\n{esc(p['title'])}{price}\nКлиент: {user_link(c.from_user)}",
        kb([B("Написать клиенту", url=chat_url(c.from_user.id, c.from_user.username), style="primary")]),
    )
    await c.answer("Заявка отправлена — менеджер скоро напишет вам", show_alert=True)


@user.callback_query(F.data == "order")
async def order(c: CallbackQuery, state: FSMContext):
    await state.clear()
    routes = await q("SELECT * FROM routes ORDER BY id")
    if not routes:
        return await show(c, "✈️ Пока нет доступных направлений.", kb([B("Меню", "menu")]))
    rows = [[B(f"📍 {r['title']}", f"route:{r['id']}", "primary")] for r in routes]
    rows.append([B("Меню", "menu")])
    await show(c, "✈️ <b>Заказать доставку</b>\n\nВыберите направление:", kb(*rows))


@user.callback_query(F.data.startswith("route:"))
async def pick_route(c: CallbackQuery, state: FSMContext):
    rid = int(c.data[6:])
    await state.update_data(route_id=rid)
    r = await q("SELECT * FROM routes WHERE id=?", (rid,), one=True)
    flights = await q("SELECT * FROM flights WHERE route_id=? ORDER BY id", (rid,))
    if not r or not flights:
        return await show(c, "Ближайших рейсов по этому направлению пока нет.\nНапишите в поддержку — подскажем.",
                          kb([B("Поддержка", url=support_url())], [B("Назад", "order")]))
    rows = [[B(f"✈️ {f['date']}", f"flt:{f['id']}", "success")] for f in flights]
    rows.append([B("Назад", "order")])
    await show(c, f"📍 <b>{esc(r['title'])}</b>\n\nВыберите ближайший рейс, к которому нужно успеть:", kb(*rows))


@user.callback_query(F.data.startswith("flt:"))
async def pick_flight(c: CallbackQuery, state: FSMContext):
    await state.update_data(flight_id=int(c.data[4:]))
    cats = await q("SELECT * FROM categories ORDER BY id")
    if not cats:
        return await show(c, "Категорий пока нет — напишите в поддержку.", kb([B("Меню", "menu")]))
    rows = [[B(k["title"], f"cat:{k['id']}", "primary")] for k in cats]
    rows.append([B("Назад", "order")])
    await show(c, "🗂 <b>Что нужно привезти?</b>\n\nВыберите категорию:", kb(*rows))


@user.callback_query(F.data.startswith("cat:"))
async def pick_cat(c: CallbackQuery, state: FSMContext):
    await state.update_data(category_id=int(c.data[4:]))
    await state.set_state(OrderFlow.item)
    await show(c, "✍️ Напишите название товара, который нужно заказать.\n\n"
                  "<i>Например: iPhone 17 Pro Max 256GB</i>", kb([B("Отмена", "menu", "danger")]))


@user.message(OrderFlow.item, F.text)
async def got_item(m: Message, state: FSMContext):
    item = m.text.strip()[:200]
    d = await state.get_data()
    route = await q("SELECT src FROM routes WHERE id=?", (d.get("route_id"),), one=True)
    cat = await q("SELECT markup, min_fee FROM categories WHERE id=?", (d.get("category_id"),), one=True)
    src = route["src"] if route and route["src"] in SRC else "ae"
    pct = cat["markup"] if cat and cat["markup"] is not None else DEFAULT_MARKUP
    min_fee = cat["min_fee"] if cat and cat["min_fee"] is not None else DEFAULT_MIN_FEE
    wait = await m.answer(f"🔎 Ищу актуальную цену {SRC[src]['flag']}…")
    res = await estimate_price(item, src, pct, min_fee)
    price = f"≈ {rub(res[0])}" if res else None
    await state.update_data(item=item, price=price)
    await state.set_state(OrderFlow.confirm)
    if res:
        total, item_price, fee = res
        text = (f"<b>{esc(item)}</b>\n\n"
                f"Товар: ≈ {rub(item_price)}\nУслуга доставки: ≈ {rub(fee)}\n"
                f"💰 Итого: <b>{rub(total)}</b>\n"
                "<blockquote>Цена ориентировочная и зависит от курса. Для уточнения точной стоимости "
                "напишите в поддержку.</blockquote>\n\nХотите оформить предзаказ?")
    else:
        text = (f"<b>{esc(item)}</b>\n\nНе удалось определить цену автоматически.\n"
                "Уточните стоимость в поддержке.\n\nХотите оформить предзаказ?")
    await wait.edit_text(text, reply_markup=kb(
        [B("Да, оформить", "pre_yes", "success"), B("Нет", "menu", "danger")],
        [B("Уточнить цену", url=support_url())],
    ))


@user.callback_query(OrderFlow.confirm, F.data == "pre_yes")
async def pre_yes(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    fl = await q("SELECT f.date, r.title FROM flights f JOIN routes r ON r.id=f.route_id WHERE f.id=?",
                 (d.get("flight_id"),), one=True)
    cat = await q("SELECT title FROM categories WHERE id=?", (d.get("category_id"),), one=True)
    route, flight = (fl["title"], fl["date"]) if fl else ("—", "—")
    category = cat["title"] if cat else "—"
    oid = await q(
        "INSERT INTO orders(user_id,username,route,flight,category,item,price) VALUES(?,?,?,?,?,?,?)",
        (c.from_user.id, c.from_user.username, route, flight, category, d["item"], d.get("price") or "—"),
        commit=True,
    )
    await state.clear()
    await notify_admins(
        c.bot,
        f"🆕 <b>Предзаказ №{oid}</b>\n\n👤 {user_link(c.from_user)}\n📍 {esc(route)} · {esc(flight)}\n"
        f"🗂 {esc(category)}\n📦 <b>{esc(d['item'])}</b>\n💰 {d.get('price') or 'цена не определена'}",
        kb(
            [B("Написать клиенту", url=chat_url(c.from_user.id, c.from_user.username), style="primary")],
            [B("В работу", f"ost:{oid}:work", "success"), B("Отклонить", f"ost:{oid}:no", "danger")],
        ),
    )
    await show(c, f"✅ <b>Предзаказ №{oid} оформлен</b>\n\nАдминистратор скоро напишет вам лично.\n"
                  "<blockquote>Товар оплачивается сразу, услуга доставки — при получении.</blockquote>",
               kb([B("Мои заказы", "my", "primary")], [B("Меню", "menu")]))


@user.callback_query(F.data == "my")
async def my_orders(c: CallbackQuery):
    rows = await q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,))
    if not rows:
        return await show(c, "📦 <b>Мои заказы</b>\n\nПока пусто.",
                          kb([B("Заказать доставку", "order", "success")], [B("Меню", "menu")]))
    text = "📦 <b>Мои заказы</b>\n\n" + "\n\n".join(
        f"<b>№{o['id']}</b> · {esc(o['item'])}\n📍 {esc(o['route'])} · {esc(o['flight'])}\n"
        f"{o['price']}\n{STATUS.get(o['status'], o['status'])}" for o in rows)
    await show(c, text, kb([B("Меню", "menu")]))


# ───────────────────────── Админ-панель ─────────────────────────
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMINS))
admin.callback_query.filter(F.from_user.id.in_(ADMINS))


@admin.message(StateFilter(None), F.entities.func(lambda es: es and any(e.type == "custom_emoji" for e in es)))
async def emoji_id_lookup(m: Message):
    ids = [e.custom_emoji_id for e in m.entities if e.type == "custom_emoji"]
    lines = "\n".join(f"<code>{i}</code>" for i in ids)
    await m.answer(f"🆔 <b>ID премиум-эмодзи:</b>\n\n{lines}\n\n"
                   "Скопируйте id и вставьте его в код как icon= у нужной кнопки.")


@admin.callback_query(F.data == "admin")
async def admin_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Админ-панель</b>\n\n"
                  "Пришлите сюда сообщение с премиум-эмодзи (просто отправьте его текстом), "
                  "и я покажу его id для кнопок.", kb(
        [B("Рассылка всем", "bc", "primary")],
        [B("Товары", "adm:products"), B("Категории", "adm:categories")],
        [B("Направления", "adm:routes"), B("Рейсы", "adm:flights")],
        [B("Заказы", "adm:orders")],
        [B("Меню", "menu")],
    ))


async def render_list(c, t):
    if t == "flights":
        items = await q("SELECT f.id, r.title || ' · ' || f.date AS label FROM flights f "
                        "JOIN routes r ON r.id=f.route_id ORDER BY f.id")
        rows = [[B(f"🗑 {i['label'][:40]}", f"del:flights:{i['id']}", "danger")] for i in items]
    elif t == "routes":
        items = await q("SELECT * FROM routes ORDER BY id")
        rows = [[B(f"📍 {i['title'][:30]} · {SRC.get(i['src'], SRC['ae'])['flag']}", f"redit:{i['id']}", "primary")]
                for i in items]
    elif t == "categories":
        items = await q("SELECT * FROM categories ORDER BY id")
        rows = [[B(f"{i['title'][:26]} · {(i['markup'] if i['markup'] is not None else DEFAULT_MARKUP):g}% · "
                   f"от {rub(i['min_fee'] if i['min_fee'] is not None else DEFAULT_MIN_FEE)}",
                   f"cedit:{i['id']}", "primary")] for i in items]
    else:
        items = await q("SELECT id, title, price FROM products ORDER BY id")
        rows = [[B(f"🗑 {i['title'][:30]}" + (f" · {rub(i['price'])}" if i["price"] else ""),
                   f"del:products:{i['id']}", "danger")] for i in items]
    rows.append([B("Добавить", f"add:{t}", "success")])
    rows.append([B("Назад", "admin")])
    if not items:
        hint = "Пока пусто."
    elif t in ("routes", "categories"):
        hint = "Нажмите на элемент, чтобы изменить или удалить."
    else:
        hint = "Нажмите на элемент, чтобы удалить."
    await show(c, f"<b>{TABLES[t]}</b>\n\n{hint}", kb(*rows))


async def render_route(ev, rid):
    r = await q("SELECT * FROM routes WHERE id=?", (rid,), one=True)
    if not r:
        return await show(ev, "Направление не найдено.", kb([B("Назад", "adm:routes")]))
    cur = r["src"] if r["src"] in SRC else "ae"
    rows = [[B(("✅ " if k == cur else "") + v["name"], f"rset:{rid}:{k}", "success" if k == cur else None)]
            for k, v in SRC.items()]
    rows.append([B("Удалить направление", f"del:routes:{rid}", "danger")])
    rows.append([B("Назад", "adm:routes")])
    await show(ev, f"📍 <b>{esc(r['title'])}</b>\n\n🔎 Где искать цены для этого направления "
                   "(страна, откуда везём товар):", kb(*rows))


async def render_cat(ev, cid):
    k = await q("SELECT * FROM categories WHERE id=?", (cid,), one=True)
    if not k:
        return await show(ev, "Категория не найдена.", kb([B("Назад", "adm:categories")]))
    mk = k["markup"] if k["markup"] is not None else DEFAULT_MARKUP
    mf = k["min_fee"] if k["min_fee"] is not None else DEFAULT_MIN_FEE
    await show(ev, f"🗂 <b>{esc(k['title'])}</b>\n\nНаценка: <b>{mk:g}%</b>\nМинимальная стоимость услуги: "
                   f"<b>{rub(mf)}</b>\n<blockquote>Стоимость услуги доставки = наибольшее из (% от цены товара, "
                   "минимальная сумма). Прибавляется к цене товара в стране закупки.</blockquote>",
               kb([B("Изменить наценку", f"cmk:{cid}", "primary"),
                   B("Мин. сумма", f"cmf:{cid}", "primary")],
                  [B("Удалить категорию", f"del:categories:{cid}", "danger")],
                  [B("Назад", "adm:categories")]))


@admin.callback_query(F.data.regexp(r"^adm:(products|categories|routes|flights)$"))
async def adm_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_list(c, c.data.split(":")[1])


@admin.callback_query(F.data.regexp(r"^redit:\d+$"))
async def route_card(c: CallbackQuery):
    await render_route(c, int(c.data[6:]))


@admin.callback_query(F.data.regexp(r"^rset:\d+:(ae|cn|tr|ru)$"))
async def route_set(c: CallbackQuery):
    _, rid, src = c.data.split(":")
    await q("UPDATE routes SET src=? WHERE id=?", (src, int(rid)), commit=True)
    await render_route(c, int(rid))


@admin.callback_query(F.data.regexp(r"^cedit:\d+$"))
async def cat_card(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_cat(c, int(c.data[6:]))


@admin.callback_query(F.data.regexp(r"^cmk:\d+$"))
async def cat_markup(c: CallbackQuery, state: FSMContext):
    cid = int(c.data[4:])
    await state.set_state(EditMarkup.value)
    await state.update_data(cid=cid)
    await show(c, "✍️ Введите наценку в процентах (например: <i>15</i>):",
               kb([B("Отмена", f"cedit:{cid}", "danger")]))


@admin.message(EditMarkup.value, F.text)
async def cat_markup_save(m: Message, state: FSMContext):
    try:
        v = float(m.text.replace(",", ".").replace("%", "").strip())
        if not 0 <= v <= 300:
            raise ValueError
    except ValueError:
        return await m.answer("Введите число от 0 до 300, например 15")
    cid = (await state.get_data())["cid"]
    await q("UPDATE categories SET markup=? WHERE id=?", (v, cid), commit=True)
    await state.clear()
    await render_cat(m, cid)


@admin.callback_query(F.data.regexp(r"^cmf:\d+$"))
async def cat_min_fee(c: CallbackQuery, state: FSMContext):
    cid = int(c.data[4:])
    await state.set_state(EditMinFee.value)
    await state.update_data(cid=cid)
    await show(c, "✍️ Введите минимальную стоимость услуги в рублях (например: <i>7000</i>):",
               kb([B("Отмена", f"cedit:{cid}", "danger")]))


@admin.message(EditMinFee.value, F.text)
async def cat_min_fee_save(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not digits:
        return await m.answer("Введите сумму числом, например: 7000")
    cid = (await state.get_data())["cid"]
    await q("UPDATE categories SET min_fee=? WHERE id=?", (int(digits), cid), commit=True)
    await state.clear()
    await render_cat(m, cid)


@admin.callback_query(F.data.regexp(r"^del:(products|categories|routes|flights):\d+$"))
async def adm_del(c: CallbackQuery):
    _, t, i = c.data.split(":")
    if t == "routes":
        await q("DELETE FROM flights WHERE route_id=?", (int(i),), commit=True)
    await q(f"DELETE FROM {t} WHERE id=?", (int(i),), commit=True)
    await c.answer("Удалено")
    await render_list(c, t)


@admin.callback_query(F.data.regexp(r"^add:(products|categories|routes|flights)$"))
async def adm_add(c: CallbackQuery, state: FSMContext):
    t = c.data.split(":")[1]
    back = kb([B("Отмена", f"adm:{t}", "danger")])
    if t == "products":
        await state.set_state(AddProduct.photo)
        return await show(c, "📸 Шаг 1/4: отправьте <b>фото</b> товара.", back)
    if t == "flights":
        routes = await q("SELECT * FROM routes ORDER BY id")
        if not routes:
            return await c.answer("Сначала добавьте направление", show_alert=True)
        rows = [[B(f"📍 {r['title']}", f"fr:{r['id']}", "primary")] for r in routes]
        rows.append([B("Отмена", "adm:flights", "danger")])
        return await show(c, "Выберите направление для рейса:", kb(*rows))
    await state.set_state(AddSimple.title)
    await state.update_data(table=t)
    hint = "например: <i>Китай → Москва</i>" if t == "routes" else "например: <i>⌚ Часы</i>"
    await show(c, f"✍️ Введите название ({hint}):", back)


@admin.message(AddProduct.photo, F.photo)
async def p_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.photo[-1].file_id)
    await state.set_state(AddProduct.title)
    await m.answer("✍️ Шаг 2/4: введите <b>название</b> товара.")


@admin.message(AddProduct.photo)
async def p_photo_bad(m: Message):
    await m.answer("Нужно отправить именно фото 📸")


@admin.message(AddProduct.title, F.text)
async def p_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AddProduct.price)
    await m.answer("💰 Шаг 3/4: введите <b>цену в рублях</b> (только число, например: <i>45000</i>).")


@admin.message(AddProduct.price, F.text)
async def p_price(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not digits:
        return await m.answer("Введите цену числом, например: 45000")
    await state.update_data(price=int(digits))
    await state.set_state(AddProduct.desc)
    await m.answer("📝 Шаг 4/4: введите <b>описание</b> товара.")


@admin.message(AddProduct.desc, F.text)
async def p_desc(m: Message, state: FSMContext):
    d = await state.get_data()
    await q("INSERT INTO products(photo,title,description,price) VALUES(?,?,?,?)",
            (d["photo"], d["title"], m.text.strip(), d["price"]), commit=True)
    await state.clear()
    await m.answer("✅ Товар добавлен в каталог.",
                   reply_markup=kb([B("Ещё товар", "add:products", "success"), B("Товары", "adm:products")]))


@admin.message(AddSimple.title, F.text)
async def simple_add(m: Message, state: FSMContext):
    t = (await state.get_data())["table"]
    title = m.text.strip()
    if t == "routes":
        rid = await q("INSERT INTO routes(title, src) VALUES(?,?)", (title, guess_src(title)), commit=True)
        await state.clear()
        return await render_route(m, rid)
    cid = await q("INSERT INTO categories(title) VALUES(?)", (title,), commit=True)
    await state.clear()
    await render_cat(m, cid)


@admin.callback_query(F.data.startswith("fr:"))
async def fl_route(c: CallbackQuery, state: FSMContext):
    await state.update_data(route_id=int(c.data[3:]))
    await state.set_state(AddFlight.date)
    await show(c, "📅 Введите дату рейса (например: <i>25 сентября</i>):",
               kb([B("Отмена", "adm:flights", "danger")]))


@admin.message(AddFlight.date, F.text)
async def fl_date(m: Message, state: FSMContext):
    d = await state.get_data()
    await q("INSERT INTO flights(route_id,date) VALUES(?,?)", (d["route_id"], m.text.strip()), commit=True)
    await state.clear()
    await m.answer("✅ Рейс добавлен.",
                   reply_markup=kb([B("Ещё рейс", "add:flights", "success"), B("Рейсы", "adm:flights")]))


@admin.callback_query(F.data == "adm:orders")
async def adm_orders(c: CallbackQuery):
    rows = await q("SELECT * FROM orders ORDER BY id DESC LIMIT 15")
    text = "📦 <b>Последние заказы</b>\n\n" + ("\n\n".join(
        f"<b>№{o['id']}</b> · {esc(o['item'])}\n👤 {'@' + o['username'] if o['username'] else o['user_id']} · "
        f"{esc(o['flight'])}\n{o['price']} · {STATUS.get(o['status'], o['status'])}" for o in rows) or "Пока пусто.")
    await show(c, text, kb([B("Назад", "admin")]))


@admin.callback_query(F.data.regexp(r"^ost:\d+:(work|no)$"))
async def order_status(c: CallbackQuery):
    _, oid, st = c.data.split(":")
    await q("UPDATE orders SET status=? WHERE id=?", (st, int(oid)), commit=True)
    o = await q("SELECT * FROM orders WHERE id=?", (int(oid),), one=True)
    try:
        await c.bot.send_message(o["user_id"], f"Статус вашего предзаказа №{oid}: <b>{STATUS[st]}</b>")
    except Exception:
        pass
    await c.message.edit_text(
        c.message.html_text + f"\n\n<b>{STATUS[st]}</b>",
        reply_markup=kb([B("Написать клиенту", url=chat_url(o["user_id"], o["username"]), style="primary")]),
    )
    await c.answer("Статус обновлён")


# ── Рассылка ──
@admin.callback_query(F.data == "bc")
async def bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await show(c, "📣 Отправьте сообщение для рассылки (текст, фото — как есть).",
               kb([B("Отмена", "admin", "danger")]))


@admin.message(Broadcast.msg)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    n = len(await q("SELECT id FROM users"))
    await m.copy_to(m.chat.id)
    await m.answer(f"Отправить это сообщение <b>{n}</b> пользователям?",
                   reply_markup=kb([B("Отправить", "bc_go", "success"), B("Отмена", "admin", "danger")]))


@admin.callback_query(Broadcast.msg, F.data == "bc_go")
async def bc_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await c.answer("Отправляю…")
    ok = fail = 0
    for u in await q("SELECT id FROM users"):
        try:
            await c.bot.copy_message(u["id"], d["chat_id"], d["message_id"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await c.message.answer(f"✅ Рассылка завершена: доставлено {ok}, не доставлено {fail}.",
                           reply_markup=kb([B("Админ-панель", "admin")]))


# ───────────────────────── Резервные копии базы ─────────────────────────
BACKUP_DIR = "backups"
BACKUP_KEEP = 30  # сколько последних копий хранить
BACKUP_EVERY = 6 * 3600  # период автосохранения, сек


def backup_db(tag="auto"):
    if not os.path.exists(DB):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"bot_{stamp}_{tag}.db")
    try:
        shutil.copy2(DB, dest)
    except Exception as e:
        log.warning("backup failed: %s", e)
        return
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("bot_") and f.endswith(".db")),
        key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)),
    )
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    log.info("db backed up to %s", dest)


async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_EVERY)
        backup_db("periodic")


# ───────────────────────── Запуск ─────────────────────────
async def main():
    await init_db()
    backup_db("startup")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(SubMiddleware())
    dp.callback_query.outer_middleware(SubMiddleware())
    dp.include_routers(user, admin)
    bg = asyncio.create_task(backup_loop())
    try:
        await dp.start_polling(bot)
    finally:
        bg.cancel()
        backup_db("shutdown")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        backup_db("shutdown")
