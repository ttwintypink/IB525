import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------------- CONFIG (NO .env) ----------------
UTC = timezone.utc

DB_PATH = "bot.db"

# ВСТАВЬ СВОИ ЗНАЧЕНИЯ САМ (не пересылай токены третьим лицам)
BOT_TOKEN = "8545376566:AAFm2315W462Z-M2pdi9Ys6oS08P-xQQQKU"
BOT_USERNAME = "MemorGarant_robot"  # без "@"
OWNER_ID = 7288805373

SUPPORT_USERNAME = "but_alright"  # техподдержка: @but_alright

# Простая валидация (без упоминания .env)
if not BOT_TOKEN or "PASTE_YOUR" in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in code")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is empty (without @)")
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID is invalid")


# ---------------- FSM ----------------
class CreateDealFlow(StatesGroup):
    waiting_seller_query = State()
    waiting_amount = State()
    waiting_terms = State()


class AdminMgmtFlow(StatesGroup):
    waiting_admin_to_add = State()
    waiting_admin_to_remove = State()


# ---------------- DB ----------------
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            terms TEXT NOT NULL,

            status TEXT NOT NULL,

            invite_token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,

            accepted_at TEXT,
            declined_at TEXT,

            deposit_confirmed_at TEXT,
            delivered_at TEXT,
            received_at TEXT,
            released_at TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            balance REAL NOT NULL,
            PRIMARY KEY (user_id, currency)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            approved_at TEXT
        )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deals_token ON deals(invite_token)")
        await db.commit()


async def upsert_user(telegram_id: int, username: str | None) -> None:
    now = now_iso()
    username_norm = (username or "").lstrip("@").lower().strip() or None
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )).fetchone()

        if row:
            await db.execute(
                "UPDATE users SET username = ?, last_seen_at = ? WHERE telegram_id = ?",
                (username_norm, now, telegram_id),
            )
        else:
            await db.execute(
                "INSERT INTO users (telegram_id, username, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (telegram_id, username_norm, now, now),
            )
        await db.commit()


async def get_user_by_id(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT telegram_id, username FROM users WHERE telegram_id = ?",
            (user_id,),
        )).fetchone()
    if not row:
        return None
    return {"telegram_id": row[0], "username": row[1]}


async def find_user_by_query(q: str) -> dict | None:
    q = (q or "").strip()

    # ID
    if re.fullmatch(r"\d{5,15}", q):
        return await get_user_by_id(int(q))

    # username (@name) or link (t.me/name)
    username = q
    username = username.replace("https://", "").replace("http://", "")
    username = username.replace("t.me/", "").replace("telegram.me/", "")
    if "/" in username:
        username = username.split("/")[-1]
    if "?" in username:
        username = username.split("?")[0]
    username = username.strip().lstrip("@").lower()

    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT telegram_id, username FROM users WHERE username = ?",
            (username,),
        )).fetchone()
    if row:
        return {"telegram_id": row[0], "username": row[1]}
    return None


async def create_deal_invite(buyer_id: int, seller_id: int, amount: float, currency: str, terms: str) -> dict:
    token = secrets.token_urlsafe(16)
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(hours=24)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO deals (
            buyer_id, seller_id, amount, currency, terms,
            status, invite_token, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            buyer_id, seller_id, float(amount), currency.upper(), terms,
            "INVITE_CREATED", token, expires_at.isoformat(), created_at.isoformat()
        ))
        await db.commit()
        deal_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]

    return {"deal_id": deal_id, "token": token, "expires_at": expires_at}


async def get_deal_by_token(token: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("""
        SELECT id, buyer_id, seller_id, amount, currency, terms, status, expires_at
        FROM deals WHERE invite_token = ?
        """, (token,))).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "buyer_id": row[1], "seller_id": row[2],
        "amount": row[3], "currency": row[4], "terms": row[5],
        "status": row[6], "expires_at": row[7]
    }


async def get_deal_by_id(deal_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("""
        SELECT id, buyer_id, seller_id, amount, currency, terms, status
        FROM deals WHERE id = ?
        """, (deal_id,))).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "buyer_id": row[1], "seller_id": row[2],
        "amount": row[3], "currency": row[4], "terms": row[5],
        "status": row[6]
    }


async def set_deal_status(deal_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
        await db.commit()


async def mark_field(deal_id: int, field: str) -> None:
    t = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE deals SET {field} = ? WHERE id = ?", (t, deal_id))
        await db.commit()


async def ensure_balance_row(user_id: int, currency: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT balance FROM balances WHERE user_id = ? AND currency = ?",
            (user_id, currency),
        )).fetchone()
        if not row:
            await db.execute(
                "INSERT INTO balances (user_id, currency, balance) VALUES (?, ?, ?)",
                (user_id, currency, 0.0),
            )
            await db.commit()


async def add_balance(user_id: int, currency: str, amount: float) -> None:
    await ensure_balance_row(user_id, currency)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE balances SET balance = balance + ? WHERE user_id = ? AND currency = ?",
            (float(amount), user_id, currency),
        )
        await db.commit()


async def get_balance(user_id: int, currency: str) -> float:
    await ensure_balance_row(user_id, currency)
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT balance FROM balances WHERE user_id = ? AND currency = ?",
            (user_id, currency),
        )).fetchone()
    return float(row[0]) if row else 0.0


async def list_deals_by_status(status: str, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("""
        SELECT id, buyer_id, seller_id, amount, currency, status
        FROM deals
        WHERE status = ?
        ORDER BY id DESC
        LIMIT ?
        """, (status, limit))).fetchall()
    return [
        {"id": r[0], "buyer_id": r[1], "seller_id": r[2], "amount": r[3], "currency": r[4], "status": r[5]}
        for r in rows
    ]


async def create_withdraw_request(user_id: int, currency: str, amount: float) -> int:
    created_at = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO withdrawals (user_id, currency, amount, status, created_at)
        VALUES (?, ?, ?, 'WITHDRAW_REQUESTED', ?)
        """, (user_id, currency, float(amount), created_at))
        await db.commit()
        wid = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
    return int(wid)


async def list_withdrawals(status: str = "WITHDRAW_REQUESTED", limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("""
        SELECT id, user_id, currency, amount, status, created_at
        FROM withdrawals
        WHERE status = ?
        ORDER BY id DESC
        LIMIT ?
        """, (status, limit))).fetchall()
    return [
        {"id": r[0], "user_id": r[1], "currency": r[2], "amount": r[3], "status": r[4], "created_at": r[5]}
        for r in rows
    ]


async def approve_withdrawal(withdraw_id: int) -> dict | None:
    approved_at = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("""
        SELECT id, user_id, currency, amount, status
        FROM withdrawals WHERE id = ?
        """, (withdraw_id,))).fetchone()
        if not row:
            return None
        if row[4] != "WITHDRAW_REQUESTED":
            return None

        user_id, currency, amount = int(row[1]), str(row[2]), float(row[3])
        bal = await get_balance(user_id, currency)
        if bal < amount:
            return {"error": "INSUFFICIENT_BALANCE", "user_id": user_id, "currency": currency, "amount": amount, "balance": bal}

        await db.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ? AND currency = ?",
            (amount, user_id, currency),
        )
        await db.execute(
            "UPDATE withdrawals SET status = 'WITHDRAW_APPROVED', approved_at = ? WHERE id = ?",
            (approved_at, withdraw_id),
        )
        await db.commit()

    return {"id": withdraw_id, "user_id": user_id, "currency": currency, "amount": amount}


# ---------------- Admins ----------------
async def is_db_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT user_id FROM admins WHERE user_id = ?",
            (user_id,),
        )).fetchone()
    return bool(row)


async def add_admin(user_id: int, added_by: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (user_id, added_by, now_iso()),
        )
        await db.commit()


async def remove_admin(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await db.commit()


async def list_admins(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("""
        SELECT a.user_id, u.username, a.added_by, a.added_at
        FROM admins a
        LEFT JOIN users u ON u.telegram_id = a.user_id
        ORDER BY a.added_at DESC
        LIMIT ?
        """, (limit,))).fetchall()
    return [{"user_id": r[0], "username": r[1], "added_by": r[2], "added_at": r[3]} for r in rows]


# ---------------- UI / Keyboards ----------------
def kb_main(is_admin_flag: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🤝 Создать сделку", callback_data="menu:create_deal")],
        [InlineKeyboardButton(text="💳 Депозит", callback_data="menu:deposit")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
        [InlineKeyboardButton(text="🆘 Техподдержка", callback_data="menu:support")],
    ]
    if is_admin_flag:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")]
    ])


def kb_hide() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Скрыть", callback_data="ui:hide")]
    ])


def kb_invite_actions(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"deal:accept:{deal_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"deal:decline:{deal_id}"),
        ]
    ])


def kb_seller_delivered(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Я передал товар", callback_data=f"deal:delivered:{deal_id}")]
    ])


def kb_buyer_received(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я получил товар", callback_data=f"deal:received:{deal_id}")]
    ])


def kb_admin_menu(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✅ Подтвердить депозит", callback_data="admin:deposits")],
        [InlineKeyboardButton(text="💸 Запросы на вывод", callback_data="admin:withdrawals")],
        [InlineKeyboardButton(text="📄 Сделки (последние)", callback_data="admin:deals_recent")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text="👥 Администраторы", callback_data="admin:admins")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_deposit_pick(deals: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for d in deals:
        rows.append([InlineKeyboardButton(
            text=f"Сделка #{d['id']} — {d['amount']} {d['currency']}",
            callback_data=f"admin:confirm_deposit:{d['id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_withdraw_pick(ws: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for w in ws:
        rows.append([InlineKeyboardButton(
            text=f"Вывод #{w['id']} — {w['amount']} {w['currency']}",
            callback_data=f"admin:approve_withdraw:{w['id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Баланс", callback_data="profile:balance")],
        [InlineKeyboardButton(text="💸 Запросить вывод", callback_data="profile:withdraw")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")]
    ])


def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить администратора", callback_data="admin:add_admin")],
        [InlineKeyboardButton(text="➖ Удалить администратора", callback_data="admin:remove_admin")],
        [InlineKeyboardButton(text="📋 Список администраторов", callback_data="admin:list_admins")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


# ---------------- Text templates (beauty) ----------------
def t_start() -> str:
    return (
        "✨ <b>Auto-Гарант</b>\n"
        "<i>Безопасные сделки. Прозрачные условия. Контроль статусов.</i>\n\n"
        "Выберите действие:"
    )


def t_support() -> str:
    return (
        "🆘 <b>Техподдержка</b>\n\n"
        f"Связаться с оператором: <b>@{SUPPORT_USERNAME}</b>\n\n"
        "› <i>Опишите проблему и прикрепите ID сделки (если есть).</i>"
    )


# ---------------- Bot ----------------
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()


async def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or await is_db_admin(user_id)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.username)
    await state.clear()

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("deal_"):
        token = parts[1].replace("deal_", "", 1).strip()
        await handle_deal_deeplink(message, token)
        return

    await message.answer(t_start(), reply_markup=kb_main(await is_admin(message.from_user.id)))


async def handle_deal_deeplink(message: Message, token: str):
    deal = await get_deal_by_token(token)
    if not deal:
        await message.answer("❌ <b>Приглашение не найдено</b>\n\n› <i>Проверьте ссылку.</i>")
        return

    expires_at = datetime.fromisoformat(deal["expires_at"])
    if datetime.now(UTC) > expires_at:
        await set_deal_status(deal["id"], "EXPIRED")
        await message.answer("⌛ <b>Приглашение истекло</b>\n\n› <i>Попросите создать новое.</i>")
        return

    if message.from_user.id != deal["seller_id"]:
        await message.answer("❌ <b>Это приглашение предназначено другому пользователю</b>")
        return

    text = (
        "🤝 <b>Приглашение в сделку</b>\n\n"
        f"💰 <b>Сумма:</b> <code>{deal['amount']} {deal['currency']}</code>\n\n"
        "🧾 <b>Условия:</b>\n"
        f"<pre>{deal['terms']}</pre>\n"
        "Подтвердите участие:"
    )
    await message.answer(text, reply_markup=kb_invite_actions(deal["id"]))


@dp.callback_query(lambda c: c.data == "menu:back")
async def menu_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(t_start(), reply_markup=kb_main(await is_admin(cb.from_user.id)))
    await cb.answer()


@dp.callback_query(lambda c: c.data == "menu:support")
async def menu_support(cb: CallbackQuery):
    await cb.message.edit_text(t_support(), reply_markup=kb_back())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "menu:create_deal")
async def menu_create_deal(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CreateDealFlow.waiting_seller_query)
    text = (
        "🔍 <b>Поиск пользователя для сделки</b>\n\n"
        "Вы можете найти пользователя по параметрам:\n"
        "├ <b>ID</b>: <code>111112222</code>\n"
        "├ <b>Юзернейм</b>: <code>@username</code>\n"
        "╰ <b>Ссылка</b>: <code>t.me/username</code>\n\n"
        "› <i>Регистр не важен. Нет разницы между UserName и username.</i>\n"
        "› <i>Можно также отправить упоминание через «упомянуть» в Telegram.</i>"
    )
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()


@dp.message(CreateDealFlow.waiting_seller_query)
async def deal_seller_query(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.username)

    # поддержка кликабельного упоминания (text_mention)
    if message.entities:
        for ent in message.entities:
            if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None):
                mentioned_id = ent.user.id
                found = await get_user_by_id(mentioned_id)
                if not found:
                    await message.answer(
                        "❌ <b>Пользователь не найден</b>\n\n› <i>Он должен хотя бы один раз запустить бота: /start</i>",
                        reply_markup=kb_hide()
                    )
                    return
                if found["telegram_id"] == message.from_user.id:
                    await message.answer("⚠️ <b>Нельзя создать сделку с самим собой</b>", reply_markup=kb_hide())
                    return

                await state.update_data(seller_id=int(found["telegram_id"]))
                await state.set_state(CreateDealFlow.waiting_amount)

                label = f"<code>ID {found['telegram_id']}</code>"
                if found["username"]:
                    label = f"<code>@{found['username']}</code>"

                await message.answer(
                    f"✅ <b>Пользователь найден</b>: {label}\n\n"
                    "Введите сумму сделки:\n• <code>1000 USDT</code>\n• <code>5000 RUB</code>"
                )
                return

    q = (message.text or "").strip()
    found = await find_user_by_query(q)
    if not found:
        await message.answer(
            "❌ <b>Пользователь не найден</b>\n\n› <i>Он должен хотя бы один раз запустить бота: /start</i>",
            reply_markup=kb_hide()
        )
        return
    if found["telegram_id"] == message.from_user.id:
        await message.answer("⚠️ <b>Нельзя создать сделку с самим собой</b>", reply_markup=kb_hide())
        return

    await state.update_data(seller_id=int(found["telegram_id"]))
    await state.set_state(CreateDealFlow.waiting_amount)

    label = f"<code>ID {found['telegram_id']}</code>"
    if found["username"]:
        label = f"<code>@{found['username']}</code>"

    await message.answer(
        f"✅ <b>Пользователь найден</b>: {label}\n\n"
        "Введите сумму сделки:\n• <code>1000 USDT</code>\n• <code>5000 RUB</code>"
    )


@dp.message(CreateDealFlow.waiting_amount)
async def deal_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    m = re.fullmatch(r"(\d+(?:[.,]\d{1,8})?)\s*(USDT|RUB)", text, flags=re.IGNORECASE)
    if not m:
        await message.answer("❌ <b>Неверный формат</b>\n\nПример: <code>1000 USDT</code> или <code>5000 RUB</code>")
        return

    amount = float(m.group(1).replace(",", "."))
    if amount <= 0:
        await message.answer("❌ <b>Сумма должна быть больше нуля</b>")
        return

    currency = m.group(2).upper()
    await state.update_data(amount=amount, currency=currency)
    await state.set_state(CreateDealFlow.waiting_terms)

    await message.answer(
        "📌 <b>Условия сделки</b>\n\n"
        "Опишите одним сообщением:\n"
        "• что передаётся\n• сроки\n• что считается подтверждением\n\n"
        "› <i>Чем точнее условия — тем проще решать спорные ситуации.</i>"
    )


@dp.message(CreateDealFlow.waiting_terms)
async def deal_terms(message: Message, state: FSMContext):
    terms = (message.text or "").strip()
    if len(terms) < 10:
        await message.answer("❌ <b>Условия слишком короткие</b>\n\n› <i>Опишите подробнее.</i>")
        return

    data = await state.get_data()
    seller_id = int(data["seller_id"])
    amount = float(data["amount"])
    currency = str(data["currency"])

    invite = await create_deal_invite(
        buyer_id=message.from_user.id,
        seller_id=seller_id,
        amount=amount,
        currency=currency,
        terms=terms,
    )

    link = f"https://t.me/{BOT_USERNAME}?start=deal_{invite['token']}"
    expires_str = invite["expires_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    await state.clear()
    await message.answer(
        "✅ <b>Приглашение создано</b>\n\n"
        f"🔗 <b>Ссылка:</b>\n{link}\n\n"
        f"⏳ <b>Действует до:</b> <code>{expires_str}</code>\n\n"
        "› <i>Отправьте ссылку продавцу.</i>"
    )


@dp.callback_query(lambda c: c.data == "ui:hide")
async def ui_hide(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


# -------- Deal actions (accept/decline) --------
@dp.callback_query(lambda c: c.data and c.data.startswith("deal:accept:"))
async def deal_accept(cb: CallbackQuery):
    deal_id = int(cb.data.split(":")[-1])
    deal = await get_deal_by_id(deal_id)
    if not deal:
        await cb.message.edit_text("❌ <b>Сделка не найдена</b>")
        await cb.answer()
        return
    if cb.from_user.id != deal["seller_id"]:
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    await set_deal_status(deal_id, "AWAITING_DEPOSIT")
    await mark_field(deal_id, "accepted_at")

    try:
        await bot.send_message(
            deal["buyer_id"],
            f"✅ <b>Сделка #{deal_id} принята</b>\n\n"
            "💳 <b>Пора вносить депозит</b>\n\n"
            "› <i>После внесения депозита администратор подтвердит поступление средств.</i>"
        )
    except Exception:
        pass

    await cb.message.edit_text(
        f"✅ <b>Сделка принята</b>\n\n"
        f"🧾 <b>ID:</b> <code>#{deal_id}</code>\n\n"
        "› <i>Покупателю отправлено уведомление о депозите.</i>"
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("deal:decline:"))
async def deal_decline(cb: CallbackQuery):
    deal_id = int(cb.data.split(":")[-1])
    deal = await get_deal_by_id(deal_id)
    if not deal:
        await cb.message.edit_text("❌ <b>Сделка не найдена</b>")
        await cb.answer()
        return
    if cb.from_user.id != deal["seller_id"]:
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    await set_deal_status(deal_id, "DECLINED")
    await mark_field(deal_id, "declined_at")

    try:
        await bot.send_message(deal["buyer_id"], f"❌ <b>Сделка #{deal_id} отклонена</b>\n\n› <i>Продавец отказался.</i>")
    except Exception:
        pass

    await cb.message.edit_text(
        f"❌ <b>Сделка отклонена</b>\n\n"
        f"🧾 <b>ID:</b> <code>#{deal_id}</code>\n\n"
        "› <i>Покупателю отправлено уведомление.</i>"
    )
    await cb.answer()


# -------- Menus: deposit/profile --------
@dp.callback_query(lambda c: c.data == "menu:deposit")
async def menu_deposit(cb: CallbackQuery):
    await cb.message.edit_text(
        "💳 <b>Депозит</b>\n\n"
        "После внесения депозита администратор подтвердит поступление.\n\n"
        "› <i>До подтверждения депозита передавать товар не рекомендуется.</i>",
        reply_markup=kb_back()
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "menu:profile")
async def menu_profile(cb: CallbackQuery):
    await cb.message.edit_text("👤 <b>Профиль</b>\n\nВыберите действие:", reply_markup=kb_profile())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "profile:balance")
async def profile_balance(cb: CallbackQuery):
    usdt = await get_balance(cb.from_user.id, "USDT")
    rub = await get_balance(cb.from_user.id, "RUB")
    await cb.message.edit_text(
        "📊 <b>Баланс</b>\n\n"
        f"• <b>USDT:</b> <code>{usdt}</code>\n"
        f"• <b>RUB:</b> <code>{rub}</code>\n\n"
        "› <i>Вывод подтверждается администратором.</i>",
        reply_markup=kb_profile()
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "profile:withdraw")
async def profile_withdraw(cb: CallbackQuery):
    usdt = await get_balance(cb.from_user.id, "USDT")
    rub = await get_balance(cb.from_user.id, "RUB")

    if usdt <= 0 and rub <= 0:
        await cb.answer("Баланс пуст.", show_alert=True)
        return

    if usdt > 0:
        wid = await create_withdraw_request(cb.from_user.id, "USDT", usdt)
        amount, currency = usdt, "USDT"
    else:
        wid = await create_withdraw_request(cb.from_user.id, "RUB", rub)
        amount, currency = rub, "RUB"

    await cb.message.edit_text(
        "💸 <b>Запрос на вывод создан</b>\n\n"
        f"🧾 <b>ID:</b> <code>#{wid}</code>\n"
        f"💰 <b>Сумма:</b> <code>{amount} {currency}</code>\n\n"
        "› <i>Администратор подтвердит вывод.</i>",
        reply_markup=kb_profile()
    )

    # уведомим владельца
    try:
        await bot.send_message(OWNER_ID, "🔔 <b>Новый запрос на вывод</b>\n\nОткройте: <i>Админ-панель → Запросы на вывод</i>")
    except Exception:
        pass

    await cb.answer()


# ---------------- Admin panel ----------------
@dp.callback_query(lambda c: c.data == "admin:menu")
async def admin_menu(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text(
        "🛠 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=kb_admin_menu(is_owner(cb.from_user.id))
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:deposits")
async def admin_deposits(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    deals = await list_deals_by_status("AWAITING_DEPOSIT", limit=10)
    if not deals:
        await cb.message.edit_text("✅ <b>Нет сделок, ожидающих депозита</b>", reply_markup=kb_admin_menu(is_owner(cb.from_user.id)))
        await cb.answer()
        return

    await cb.message.edit_text(
        "✅ <b>Подтверждение депозита</b>\n\nВыберите сделку:",
        reply_markup=kb_admin_deposit_pick(deals)
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("admin:confirm_deposit:"))
async def admin_confirm_deposit(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    deal_id = int(cb.data.split(":")[-1])
    deal = await get_deal_by_id(deal_id)
    if not deal:
        await cb.answer("Сделка не найдена.", show_alert=True)
        return
    if deal["status"] != "AWAITING_DEPOSIT":
        await cb.answer("Сделка не в статусе ожидания депозита.", show_alert=True)
        return

    await set_deal_status(deal_id, "DEPOSIT_CONFIRMED")
    await mark_field(deal_id, "deposit_confirmed_at")

    # покупателю
    try:
        await bot.send_message(
            deal["buyer_id"],
            f"✅ <b>Депозит подтвержден</b> по сделке <code>#{deal_id}</code>\n\n"
            "Теперь можно выполнять условия сделки.\n\n"
            "› <i>Ожидайте отметку продавца о передаче.</i>"
        )
    except Exception:
        pass

    # продавцу — дать кнопку "Я передал"
    try:
        await bot.send_message(
            deal["seller_id"],
            f"✅ <b>Депозит подтвержден</b> (сделка <code>#{deal_id}</code>)\n\n"
            "Передайте товар/актив строго по условиям:\n"
            f"<pre>{deal['terms']}</pre>\n"
            "После передачи нажмите кнопку ниже:",
            reply_markup=kb_seller_delivered(deal_id)
        )
    except Exception:
        pass

    await cb.message.edit_text(
        f"✅ <b>Депозит подтвержден</b> (сделка <code>#{deal_id}</code>)\n\n"
        "› <i>Уведомления отправлены обеим сторонам.</i>",
        reply_markup=kb_admin_menu(is_owner(cb.from_user.id))
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:withdrawals")
async def admin_withdrawals(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    ws = await list_withdrawals("WITHDRAW_REQUESTED", limit=10)
    if not ws:
        await cb.message.edit_text("💸 <b>Нет запросов на вывод</b>", reply_markup=kb_admin_menu(is_owner(cb.from_user.id)))
        await cb.answer()
        return

    await cb.message.edit_text(
        "💸 <b>Запросы на вывод</b>\n\nВыберите заявку:",
        reply_markup=kb_admin_withdraw_pick(ws)
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("admin:approve_withdraw:"))
async def admin_approve_withdraw(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    wid = int(cb.data.split(":")[-1])
    res = await approve_withdrawal(wid)
    if not res:
        await cb.answer("Заявка не найдена/уже обработана.", show_alert=True)
        return
    if "error" in res and res["error"] == "INSUFFICIENT_BALANCE":
        await cb.message.edit_text(
            "❌ <b>Недостаточно баланса</b>\n\n"
            f"Нужно: <code>{res['amount']} {res['currency']}</code>\n"
            f"Баланс: <code>{res['balance']} {res['currency']}</code>",
            reply_markup=kb_admin_menu(is_owner(cb.from_user.id))
        )
        await cb.answer()
        return

    try:
        await bot.send_message(
            res["user_id"],
            "✅ <b>Вывод подтвержден администратором</b>\n\n"
            f"🧾 <b>ID:</b> <code>#{wid}</code>\n"
            f"💰 <b>Сумма:</b> <code>{res['amount']} {res['currency']}</code>\n\n"
            "› <i>Ожидайте фактического перечисления по вашим реквизитам.</i>"
        )
    except Exception:
        pass

    await cb.message.edit_text(
        f"✅ <b>Вывод подтвержден</b>\n\n🧾 <b>ID:</b> <code>#{wid}</code>",
        reply_markup=kb_admin_menu(is_owner(cb.from_user.id))
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:deals_recent")
async def admin_deals_recent(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("""
        SELECT id, status, amount, currency, buyer_id, seller_id
        FROM deals
        ORDER BY id DESC
        LIMIT 10
        """)).fetchall()

    if not rows:
        await cb.message.edit_text("📄 <b>Сделок пока нет</b>", reply_markup=kb_admin_menu(is_owner(cb.from_user.id)))
        await cb.answer()
        return

    lines = ["📄 <b>Последние сделки</b>\n"]
    for r in rows:
        lines.append(f"• <b>#{r[0]}</b> — <code>{r[2]} {r[3]}</code> — <i>{r[1]}</i>")
    lines.append("\n› <i>Детальная карточка сделки добавим следующей итерацией.</i>")

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_menu(is_owner(cb.from_user.id)))
    await cb.answer()


# ---------------- Admin management (owner only) ----------------
@dp.callback_query(lambda c: c.data == "admin:admins")
async def admin_admins(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer("Только владелец может управлять администраторами.", show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text("👥 <b>Администраторы</b>\n\nВыберите действие:", reply_markup=kb_admins_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:add_admin")
async def admin_add_admin(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(AdminMgmtFlow.waiting_admin_to_add)
    await cb.message.edit_text(
        "➕ <b>Добавить администратора</b>\n\n"
        "Отправьте:\n"
        "• <code>ID</code>\n"
        "• <code>@username</code>\n"
        "• <code>t.me/username</code>\n"
        "• или упоминание (через «упомянуть»)\n\n"
        "› <i>Пользователь должен хотя бы раз запустить бота.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:admins")]])
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:remove_admin")
async def admin_remove_admin(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(AdminMgmtFlow.waiting_admin_to_remove)
    await cb.message.edit_text(
        "➖ <b>Удалить администратора</b>\n\n"
        "Отправьте ID / @username / ссылку / упоминание.\n\n"
        "› <i>Владельца удалить нельзя.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:admins")]])
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:list_admins")
async def admin_list_admins(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer("Только владелец.", show_alert=True)
        return
    admins = await list_admins()
    if not admins:
        await cb.message.edit_text("📋 <b>Администраторы</b>\n\n› <i>Список пуст.</i>", reply_markup=kb_admins_menu())
        await cb.answer()
        return

    lines = ["📋 <b>Администраторы</b>\n"]
    lines.append(f"• <b>Owner</b>: <code>{OWNER_ID}</code> (@{SUPPORT_USERNAME})")
    for a in admins:
        label = f"<code>{a['user_id']}</code>"
        if a["username"]:
            label = f"<code>@{a['username']}</code> (<code>{a['user_id']}</code>)"
        lines.append(f"• {label} — добавлен <code>{a['added_by']}</code>")

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admins_menu())
    await cb.answer()


async def _extract_target_user_id_from_message(message: Message) -> int | None:
    # 1) text_mention entity (кликабельное упоминание)
    if message.entities:
        for ent in message.entities:
            if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None):
                return int(ent.user.id)
    # 2) plain text lookup (ID/@/link)
    q = (message.text or "").strip()
    found = await find_user_by_query(q)
    if found:
        return int(found["telegram_id"])
    return None


@dp.message(AdminMgmtFlow.waiting_admin_to_add)
async def admin_add_admin_msg(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Недостаточно прав.")
        return

    target_id = await _extract_target_user_id_from_message(message)
    if not target_id:
        await message.answer("❌ <b>Пользователь не найден</b>\n\n› <i>Он должен запускать бота (/start).</i>")
        return

    if target_id == OWNER_ID:
        await message.answer("⚠️ <b>Это владелец</b>\n\n› <i>Он уже имеет полный доступ.</i>")
        await state.clear()
        return

    await add_admin(target_id, message.from_user.id)
    await state.clear()

    await message.answer(
        "✅ <b>Администратор добавлен</b>\n\n"
        f"🧾 <b>ID:</b> <code>{target_id}</code>\n\n"
        "› <i>Теперь он может входить в админ-панель и подтверждать депозит/вывод.</i>"
    )


@dp.message(AdminMgmtFlow.waiting_admin_to_remove)
async def admin_remove_admin_msg(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Недостаточно прав.")
        return

    target_id = await _extract_target_user_id_from_message(message)
    if not target_id:
        await message.answer("❌ <b>Пользователь не найден</b>")
        return

    if target_id == OWNER_ID:
        await message.answer("⚠️ <b>Нельзя удалить владельца</b>")
        await state.clear()
        return

    await remove_admin(target_id)
    await state.clear()

    await message.answer(
        "✅ <b>Администратор удалён</b>\n\n"
        f"🧾 <b>ID:</b> <code>{target_id}</code>"
    )


# ---------------- Run ----------------
async def main():
    await db_init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

