"""
Telegram-бот «Гормолзавод Көкшетау · Курт»
Механика: регистрация -> код с упаковки -> баллы -> награды -> напоминания.
"""
import asyncio
import hmac
import logging
import os
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.getenv("BOT_TOKEN", "8879450618:AAFokn_ddU3qjrL-2tNCdPOShEf9YoKqXh8").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "1477906309"))
ADMIN_KEY = os.getenv("ADMIN_KEY", "vqHoPrt_jqCiTnNz2hL_MEoeZvDa9I6X")
RULES_URL = os.getenv("RULES_URL", "https://gormolzavod.kz")  # замените на ссылку с правилами акции
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "kurt.db"))

REMINDER_AFTER_DAYS = 25
MAX_FAILS = 5            # неверных кодов подряд
FAIL_WINDOW_SEC = 600    # пауза 10 минут

# Рабочие промокоды для акций: баллы начисляются случайно от 5 до 30.
BONUS_CODES = [
    "KR-2N7QBY",
    "KR-4K8MTP",
    "KR-7H2RDL",
    "KR-5C9XWV",
    "KR-9S3QMN",
    "KR-8F6PLK",
    "KR-3T7QJV",
    "KR-6D2MRC",
    "KR-4Y9NLS",
    "KR-7V5QXT",
]

# награды: стоимость в баллах -> название
REWARDS = {
    100: "Магнит и стикерпак",
    250: "Фирменная кружка",
    500: "Подарочный набор «Гормолзавод»",
}

CITIES = ["Көкшетау", "Астана", "Алматы", "Басқа · Другой"]
AGES = ["до 18", "18–24", "25–34", "35–44", "45+"]

BTN_CODE = "➕ Код енгізу · Ввести код"
BTN_PROFILE = "👤 Профиль"
BTN_TOP = "🏆 Топ участников"

router = Router()
fails: dict[int, list[float]] = {}  # tg_id -> времена ошибок


# ---------------------------------------------------------------- база данных
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                tg_id INTEGER PRIMARY KEY,
                name TEXT, phone TEXT, city TEXT, age_group TEXT,
                consent_at INTEGER, points INTEGER DEFAULT 0,
                reg_at INTEGER, last_code_at INTEGER, last_reminder_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS codes(
                code TEXT PRIMARY KEY, used_by INTEGER, used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                prize TEXT,
                cost INTEGER,
                claimed_at INTEGER
            );
            """
        )
        if BONUS_CODES:
            c.executemany(
                "INSERT OR IGNORE INTO codes(code) VALUES (?)",
                [(code,) for code in BONUS_CODES],
            )


def get_user(tg_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def code_count(tg_id: int) -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM codes WHERE used_by=?", (tg_id,)).fetchone()[0]


def norm_code(text: str) -> str:
    return "".join(text.upper().split())


def points_for_code() -> int:
    roll = secrets.randbelow(100)
    if roll < 80:
        return 5 + secrets.randbelow(6)
    if roll < 95:
        return 11 + secrets.randbelow(10)
    if roll < 99:
        return 21 + secrets.randbelow(9)
    return 30


# ---------------------------------------------------------------- состояния
class Reg(StatesGroup):
    name = State()
    phone = State()
    city = State()
    age = State()
    consent = State()


class Code(StatesGroup):
    waiting = State()


class AdminCodes(StatesGroup):
    waiting = State()


class AdminAuth(StatesGroup):
    waiting = State()


class AdminContact(StatesGroup):
    waiting = State()


admin_unlocked: set[int] = set()


# ---------------------------------------------------------------- клавиатуры
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CODE), KeyboardButton(text=BTN_PROFILE)], [KeyboardButton(text=BTN_TOP)]],
        resize_keyboard=True,
    )


def welcome_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Бастау · Начать", callback_data="start_reg")
    b.button(text="Акция ережелері · Правила акции", url=RULES_URL)
    b.adjust(1)
    return b.as_markup()


def city_kb():
    b = InlineKeyboardBuilder()
    for c in CITIES:
        b.button(text=c, callback_data=f"city:{c}")
    b.adjust(2)
    return b.as_markup()


def age_kb():
    b = InlineKeyboardBuilder()
    for a in AGES:
        b.button(text=a, callback_data=f"age:{a}")
    b.adjust(3, 2)
    return b.as_markup()


def consent_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Келісемін · Согласен(а)", callback_data="agree")
    b.button(text="📋 Ережелер · Правила", url=RULES_URL)
    b.adjust(1)
    return b.as_markup()


def after_code_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Тағы код · Ввести ещё код", callback_data="more")
    b.button(text="🎁 Менің сыйлықтарым · Мои награды", callback_data="profile")
    b.button(text="👤 Профиль", callback_data="profile")
    b.adjust(1)
    return b.as_markup()


def profile_kb(points: int):
    b = InlineKeyboardBuilder()
    affordable = [cost for cost in REWARDS if points >= cost]
    if affordable:
        b.button(text="🎁 Сыйлық алу · Получить подарок", callback_data="claim_menu")
    b.button(text="🧾 Кодтар тарихы · История кодов", callback_data="history")
    b.button(text="📦 Мои призы · Мои подарки", callback_data="my_claims")
    b.button(text="📋 Ережелер · Правила акции", url=RULES_URL)
    b.button(text="🏆 Топ участников", callback_data="top")
    b.button(text="💬 Қолдау · Поддержка", callback_data="support")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------- утилиты
def progress_bar(points: int) -> str:
    target = next((c for c in sorted(REWARDS) if points < c), None)
    if target is None:
        return "▰▰▰▰▰▰▰▰▰▰ максимум"
    filled = min(10, points * 10 // target)
    return f"{'▰' * filled}{'▱' * (10 - filled)} {points} / {target}"


def profile_text(u) -> str:
    pts = u["points"]
    with db() as c:
        claim_rows = c.execute(
            "SELECT prize, cost, claimed_at FROM claims WHERE tg_id=? ORDER BY claimed_at DESC LIMIT 5",
            (u["tg_id"],),
        ).fetchall()

    lines = [
        f"👤 <b>{u['name']}</b>, {u['city'] or '—'}",
        f"Баланс: <b>{pts} баллов</b> · Кодов: <b>{code_count(u['tg_id'])}</b>",
        "",
        "🎁 <b>Сыйлықтар / Награды</b>",
    ]
    for cost, title in sorted(REWARDS.items()):
        if pts >= cost:
            lines.append(f"✅ {cost} баллов: {title} (можно получить)")
        else:
            lines.append(f"⏳ {cost} баллов: {title} (осталось {cost - pts})")

    lines += ["", progress_bar(pts), ""]

    if claim_rows:
        lines.append("📦 <b>Ваши заказы / Мои заявки</b>")
        for row in claim_rows:
            dt = time.strftime("%d.%m.%Y", time.localtime(row["claimed_at"]))
            lines.append(f"• {dt} — {row['prize']} ({row['cost']} б.)")
    else:
        lines.append("📦 Пока вы не оформляли подарки.")

    lines += ["", "Келесі сатып алуда ескертеміз 💙", "Напомним о следующей покупке, когда подойдёт время."]
    return "\n".join(lines)


def blocked(tg_id: int) -> bool:
    now = time.time()
    fails[tg_id] = [t for t in fails.get(tg_id, []) if now - t < FAIL_WINDOW_SEC]
    return len(fails[tg_id]) >= MAX_FAILS


# ---------------------------------------------------------------- приветствие
WELCOME = (
    "Сәлеметсіз бе! 👋\n"
    "«Гормолзавод Көкшетау» ысталған құртын таңдағаныңызға рахмет.\n"
    "Қаптамадағы кодты тіркеп, ұпай жинаңыз да, сыйлықтар алыңыз.\n\n"
    "Здравствуйте! 👋\n"
    "Спасибо, что выбрали копчёный курт «Гормолзавод Көкшетау».\n"
    "Регистрируйте код с упаковки, копите баллы и получайте подарки."
)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = get_user(m.from_user.id)
    if u and u["consent_at"]:
        await m.answer(f"Қайта қош келдіңіз, {u['name']}! / С возвращением, {u['name']}!", reply_markup=main_menu())
        return
    # сюда можно добавить баннер: await m.answer_photo(FSInputFile("banner.png"), caption=WELCOME, ...)
    await m.answer(WELCOME, reply_markup=welcome_kb())


# ---------------------------------------------------------------- регистрация
@router.callback_query(F.data == "start_reg")
async def start_reg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Reg.name)
    await cb.message.answer("<b>Шаг 1 из 4</b> ●○○○\nАтыңыз кім? / Как вас зовут?")
    await cb.answer()


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = m.text.strip()[:40]
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Нөмірді жіберу · Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer(
        f"<b>Шаг 2 из 4</b> ●●○○\nРахмет, {name}! Телефон нөміріңізді жіберіңіз.\n"
        f"Спасибо, {name}! Поделитесь номером телефона. "
        "Он нужен, чтобы связаться с вами, если вы выиграете приз.",
        reply_markup=kb,
    )


@router.message(Reg.phone, F.contact)
async def reg_phone(m: Message, state: FSMContext):
    await state.update_data(phone=m.contact.phone_number)
    await state.set_state(Reg.city)
    await m.answer("Рахмет! ✅", reply_markup=ReplyKeyboardRemove())
    await m.answer("<b>Шаг 3 из 4</b> ●●●○\nҚай қаладансыз? / Из какого вы города?", reply_markup=city_kb())


@router.message(Reg.phone)
async def reg_phone_wrong(m: Message):
    await m.answer("Төмендегі батырманы басыңыз / Пожалуйста, нажмите кнопку «Поделиться номером».")


@router.callback_query(Reg.city, F.data.startswith("city:"))
async def reg_city(cb: CallbackQuery, state: FSMContext):
    await state.update_data(city=cb.data.split(":", 1)[1])
    await state.set_state(Reg.age)
    await cb.message.answer("<b>Шаг 4 из 4</b> ●●●●\nЖас тобыңыз? / Ваша возрастная группа?", reply_markup=age_kb())
    await cb.answer()


@router.callback_query(Reg.age, F.data.startswith("age:"))
async def reg_age(cb: CallbackQuery, state: FSMContext):
    await state.update_data(age=cb.data.split(":", 1)[1])
    await state.set_state(Reg.consent)
    await cb.message.answer(
        "Дербес деректерді өңдеуге келісесіз бе?\n"
        "Вы согласны на обработку персональных данных в соответствии с Законом РК "
        "«О персональных данных и их защите»? Правила акции доступны по кнопке ниже.",
        reply_markup=consent_kb(),
    )
    await cb.answer()


@router.callback_query(Reg.consent, F.data == "agree")
async def reg_done(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    now = int(time.time())
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO users(tg_id,name,phone,city,age_group,consent_at,points,reg_at) "
            "VALUES(?,?,?,?,?,?,COALESCE((SELECT points FROM users WHERE tg_id=?),0),?)",
            (cb.from_user.id, d["name"], d["phone"], d["city"], d["age"], now, cb.from_user.id, now),
        )
    await state.set_state(Code.waiting)
    await cb.message.answer(
        "Тіркелу аяқталды! 🎉\nЕнді қаптаманың ішіндегі кодты енгізіңіз.\n\n"
        "Регистрация завершена! 🎉\nВведите код с внутренней стороны упаковки "
        "(под отрывной частью).\nПример: <code>KR-7X9QAB</code>",
        reply_markup=main_menu(),
    )
    await cb.answer()


# ---------------------------------------------------------------- ввод кода
async def ask_code(target: Message, state: FSMContext):
    u = get_user(target.chat.id)
    if not u:
        await target.answer("Алдымен тіркеліңіз / Сначала зарегистрируйтесь: /start")
        return
    await state.set_state(Code.waiting)
    await target.answer(
        "Кодты енгізіңіз / Введите код с упаковки.\n"
        "Он находится на внутренней стороне, под отрывной частью."
    )


@router.message(F.text == BTN_CODE)
async def btn_code(m: Message, state: FSMContext):
    await ask_code(m, state)


@router.callback_query(F.data == "more")
async def more(cb: CallbackQuery, state: FSMContext):
    await ask_code(cb.message, state)
    await cb.answer()


@router.message(Code.waiting, F.text)
async def take_code(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = get_user(uid)
    if not u:
        await m.answer("Алдымен тіркеліңіз / Сначала зарегистрируйтесь: /start")
        return
    if blocked(uid):
        await m.answer("Тым көп қате әрекет. 10 минуттан кейін қайталаңыз.\nСлишком много попыток. Попробуйте через 10 минут.")
        return

    code = norm_code(m.text)
    now = int(time.time())
    with db() as c:
        row = c.execute("SELECT used_by FROM codes WHERE code=?", (code,)).fetchone()
        if row is None:
            fails.setdefault(uid, []).append(time.time())
            await m.answer("Код табылмады. Қайта тексеріп көріңіз.\nКод не найден. Проверьте, пожалуйста, и попробуйте ещё раз.")
            return
        earned_points = points_for_code()
        cur = c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL", (uid, now, code))
        if cur.rowcount == 0:
            await m.answer("Бұл код бұрын тіркелген.\nЭтот код уже зарегистрирован.")
            return
        c.execute(
            "UPDATE users SET points=points+?, last_code_at=?, last_reminder_at=NULL WHERE tg_id=?",
            (earned_points, now, uid),
        )
    u = get_user(uid)
    pts = u["points"]
    nxt = next((cost for cost in sorted(REWARDS) if pts < cost), None)
    tail = (
        f"До награды «{REWARDS[nxt]}» осталось {nxt - pts} баллов."
        if nxt
        else "Вы набрали баллы на все награды! 🎁"
    )
    await state.clear()
    await m.answer(
        f"🎉 Код қабылданды! / Код принят!\n\n"
        f"🎲 Сізге түсті / Вам выпало: <b>+{earned_points} ұпай · +{earned_points} баллов</b>\n"
        f"Сіздің балансыңыз / Ваш баланс: <b>{pts} баллов</b>\n\n"
        f"{progress_bar(pts)}\n{tail}",
        reply_markup=after_code_kb(),
    )


# ---------------------------------------------------------------- профиль и награды
async def show_profile(target: Message, tg_id: int):
    u = get_user(tg_id)
    if not u:
        await target.answer("Алдымен тіркеліңіз / Сначала зарегистрируйтесь: /start")
        return
    await target.answer(profile_text(u), reply_markup=profile_kb(u["points"]))


@router.message(F.text == BTN_PROFILE)
@router.message(Command("profile"))
async def profile_msg(m: Message):
    await show_profile(m, m.from_user.id)


@router.callback_query(F.data == "profile")
async def profile_cb(cb: CallbackQuery):
    await show_profile(cb.message, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data == "claim_menu")
async def claim_menu(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    b = InlineKeyboardBuilder()
    for cost, title in sorted(REWARDS.items()):
        if u["points"] >= cost:
            b.button(text=f"{title} · {cost} б.", callback_data=f"claim:{cost}")
    b.adjust(1)
    await cb.message.answer("Қай сыйлықты аласыз? / Какой подарок выберете?", reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("claim:"))
async def claim(cb: CallbackQuery, bot: Bot):
    cost = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    if cost not in REWARDS:
        await cb.answer("Неверный приз", show_alert=True)
        return
    with db() as c:
        cur = c.execute("UPDATE users SET points=points-? WHERE tg_id=? AND points>=?", (cost, uid, cost))
        if cur.rowcount:
            c.execute(
                "INSERT INTO claims(tg_id, prize, cost, claimed_at) VALUES(?,?,?,?)",
                (uid, REWARDS[cost], cost, int(time.time())),
            )
    if cur.rowcount == 0:
        await cb.answer("Баллов недостаточно", show_alert=True)
        return
    u = get_user(uid)
    await cb.message.answer(
        f"🎁 Өтінім қабылданды! / Заявка принята: <b>{REWARDS[cost]}</b>.\n"
        "Менеджер сізбен байланысады / Менеджер свяжется с вами. Рахмет! 💙"
    )
    if ADMIN_ID and uid != ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"🎁 Заявка на подарок\n{REWARDS[cost]} ({cost} б.)\n"
            f"{u['name']}, {u['city']}, {u['phone']}\nTG: {uid}",
        )
    await cb.answer()


@router.callback_query(F.data == "history")
async def history(cb: CallbackQuery):
    with db() as c:
        rows = c.execute(
            "SELECT code, used_at FROM codes WHERE used_by=? ORDER BY used_at DESC LIMIT 10", (cb.from_user.id,)
        ).fetchall()
    if not rows:
        await cb.message.answer("Әлі код жоқ / Пока нет зарегистрированных кодов.")
    else:
        lines = [f"• {time.strftime('%d.%m.%Y', time.localtime(r['used_at']))} — {r['code']}" for r in rows]
        await cb.message.answer("🧾 <b>Кодтар тарихы / История кодов</b>\n" + "\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "my_claims")
async def my_claims(cb: CallbackQuery):
    with db() as c:
        rows = c.execute(
            "SELECT prize, cost, claimed_at FROM claims WHERE tg_id=? ORDER BY claimed_at DESC LIMIT 10",
            (cb.from_user.id,),
        ).fetchall()
    if not rows:
        await cb.message.answer("📦 Сіз әлі сыйлық тапсыруды сұраған жоқсыз / Вы ещё не оформляли призы.")
    else:
        lines = [
            f"• {time.strftime('%d.%m.%Y', time.localtime(r['claimed_at']))} — {r['prize']} ({r['cost']} б.)"
            for r in rows
        ]
        await cb.message.answer("📦 <b>Мои призы / Ваши подарки</b>\n" + "\n".join(lines))
    await cb.answer()


@router.message(F.text == BTN_TOP)
@router.message(Command("top"))
async def top_msg(m: Message):
    await show_top(m)


@router.callback_query(F.data == "top")
async def top_cb(cb: CallbackQuery):
    await show_top(cb.message)
    await cb.answer()


async def show_top(target: Message):
    with db() as c:
        rows = c.execute(
            "SELECT name, points FROM users WHERE consent_at IS NOT NULL ORDER BY points DESC, reg_at ASC LIMIT 10"
        ).fetchall()
    if not rows:
        await target.answer("🏆 Рейтинг пока пуст. Зарегистрируйтесь и вводите коды!")
        return
    lines = [f"{i}. {r['name']} — {r['points']} б." for i, r in enumerate(rows, 1)]
    await target.answer("🏆 <b>Топ участников</b>\n" + "\n".join(lines))


@router.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    await cb.message.answer(
        "💬 Қолдау / Поддержка\n"
        "Тел.: +7 747 377 15 29, +7 7162 77 15 29\n"
        "E-mail: ok@gormolzavod.kz\nInstagram: @gormolzavod"
    )
    await cb.answer()


# ---------------------------------------------------------------- админ

def admin_panel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="🎲 Генерация кодов", callback_data="admin:gen")
    b.button(text="➕ Добавить коды", callback_data="admin:addcodes")
    b.button(text="👥 Участники", callback_data="admin:users")
    b.button(text="🎁 Призы", callback_data="admin:claims")
    b.adjust(2)
    return b.as_markup()


def is_admin_authorized(user_id: int) -> bool:
    return user_id == ADMIN_ID and user_id in admin_unlocked


@router.message(Command("admin"))
@router.message(Command("panel"))
async def admin_panel(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await m.answer("⛔ Нет доступа.")
        return
    if not ADMIN_KEY:
        await m.answer("Админ-ключ не настроен. Задайте переменную окружения ADMIN_KEY и перезапустите бота.")
        return
    admin_unlocked.discard(m.from_user.id)
    await state.set_state(AdminAuth.waiting)
    await m.answer("Введите админ-ключ. Для отмены отправьте /cancel.")


@router.message(AdminAuth.waiting, Command("cancel"))
async def cancel_admin_auth(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Вход в админ-панель отменён.")


@router.message(AdminAuth.waiting, F.text)
async def check_admin_key(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await state.clear()
        await m.answer("⛔ Нет доступа.")
        return
    if not hmac.compare_digest(m.text.strip(), ADMIN_KEY):
        await state.clear()
        await m.answer("Неверный ключ. Чтобы попробовать снова, отправьте /admin.")
        return

    admin_unlocked.add(m.from_user.id)
    await state.clear()
    with db() as c:
        regs = c.execute("SELECT COUNT(*) FROM users WHERE consent_at IS NOT NULL").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
        active = c.execute("SELECT COUNT(DISTINCT used_by) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Регистраций: <b>{regs}</b>\n"
        f"Активных участников: <b>{active}</b>\n"
        f"Использовано кодов: <b>{used}</b>"
    )
    await m.answer(text, reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(cb: CallbackQuery):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Сначала войдите через /admin и введите ключ.", show_alert=True)
        return
    with db() as c:
        regs = c.execute("SELECT COUNT(*) FROM users WHERE consent_at IS NOT NULL").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
        repeat = c.execute(
            "SELECT COUNT(*) FROM (SELECT used_by FROM codes WHERE used_by IS NOT NULL GROUP BY used_by HAVING COUNT(*)>=2)"
        ).fetchone()[0]
        active = c.execute("SELECT COUNT(DISTINCT used_by) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
        claims = c.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    share = f"{repeat / active * 100:.0f}%" if active else "0%"
    await cb.message.answer(
        f"📊 <b>Статистика акции</b>\n"
        f"Регистраций: <b>{regs}</b>\n"
        f"Активных участников: <b>{active}</b>\n"
        f"Кодов использовано: <b>{used}</b>\n"
        f"Повторные покупки: <b>{repeat}</b> ({share})\n"
        f"Оформлено призов: <b>{claims}</b>"
    )
    await cb.answer()


@router.callback_query(F.data == "admin:gen")
async def admin_gen(cb: CallbackQuery):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Сначала войдите через /admin и введите ключ.", show_alert=True)
        return
    await cb.message.answer(
        "🧩 Генерация кодов: отправьте команду /gen 10 или /gen 20\n\n"
        "Пример: <code>/gen 20</code>",
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(F.data == "admin:addcodes")
async def admin_addcodes(cb: CallbackQuery, state: FSMContext):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminCodes.waiting)
    await cb.message.answer(
        "Отправьте коды одним сообщением, по одному в строке.\n"
        "Формат: KR-ABC123. Дубликаты будут пропущены."
    )
    await cb.answer()


@router.message(AdminCodes.waiting, F.text)
async def add_codes_text(m: Message, state: FSMContext):
    if not is_admin_authorized(m.from_user.id):
        await state.clear()
        await m.answer("Доступ к админ-панели закрыт. Отправьте /admin и введите ключ.")
        return
    raw = [norm_code(line) for line in m.text.splitlines()]
    codes = list(dict.fromkeys(code for code in raw if code))
    if not codes or not any(code.startswith("KR-") and len(code) == 9 for code in codes):
        await state.clear()
        await m.answer("Не нашёл кодов формата KR-ABC123. Откройте /admin и нажмите «Добавить коды» ещё раз.")
        return
    added = 0
    with db() as c:
        for code in codes:
            if not code.startswith("KR-") or len(code) != 9:
                continue
            try:
                c.execute("INSERT INTO codes(code) VALUES(?)", (code,))
                added += 1
            except sqlite3.IntegrityError:
                pass
    await state.clear()
    await m.answer(f"Добавлено новых кодов: {added} из {len(codes)}. Повторные коды пропущены.")


@router.callback_query(F.data == "admin:users")
async def admin_users(cb: CallbackQuery):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Сначала войдите через /admin и введите ключ.", show_alert=True)
        return
    with db() as c:
        rows = c.execute(
            "SELECT name, city, points, tg_id FROM users WHERE consent_at IS NOT NULL ORDER BY points DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await cb.message.answer("👥 Участники пока не зарегистрировались.")
    else:
        lines = [f"• {r['name']} — {r['points']} б. ({r['city'] or '—'})" for r in rows]
        await cb.message.answer("👥 <b>Топ участников</b>\n" + "\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "admin:claims")
async def admin_claims(cb: CallbackQuery):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Сначала войдите через /admin и введите ключ.", show_alert=True)
        return
    with db() as c:
        rows = c.execute(
            "SELECT claims.id, claims.tg_id, claims.prize, claims.cost, claims.claimed_at, users.name "
            "FROM claims LEFT JOIN users ON users.tg_id=claims.tg_id "
            "ORDER BY claims.claimed_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await cb.message.answer("🎁 Призы ещё не оформлялись.")
    else:
        b = InlineKeyboardBuilder()
        for row in rows:
            name = row["name"] or f"TG {row['tg_id']}"
            b.button(
                text=f"✉️ {name[:24]} · {row['prize'][:20]}",
                callback_data=f"admin:contact:{row['id']}",
            )
        b.adjust(1)
        lines = []
        for row in rows:
            name = row["name"] or f"TG {row['tg_id']}"
            date = time.strftime("%d.%m.%Y", time.localtime(row["claimed_at"]))
            lines.append(f"• {date} — {name} — {row['prize']} ({row['cost']} б.)")
        await cb.message.answer(
            "🎁 <b>Последние призы</b>\n" + "\n".join(lines) +
            "\n\nНажмите кнопку, чтобы написать получателю через бота:",
            reply_markup=b.as_markup(),
        )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:contact:"))
async def admin_contact_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin_authorized(cb.from_user.id):
        await cb.answer("Сначала войдите через /admin и введите ключ.", show_alert=True)
        return

    claim_id = cb.data.rsplit(":", 1)[1]
    with db() as c:
        row = c.execute(
            "SELECT claims.tg_id, claims.prize, users.name FROM claims "
            "LEFT JOIN users ON users.tg_id=claims.tg_id WHERE claims.id=?",
            (claim_id,),
        ).fetchone()
    if row is None:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return

    await state.update_data(contact_tg_id=row["tg_id"], contact_claim_id=claim_id)
    await state.set_state(AdminContact.waiting)
    recipient_name = row["name"] or f"TG {row['tg_id']}"
    await cb.message.answer(
        f"Напишите сообщение для {recipient_name} "
        f"по заявке «{row['prize']}».\nДля отмены отправьте /cancel."
    )
    await cb.answer()


@router.message(AdminContact.waiting, Command("cancel"))
async def admin_contact_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отправка сообщения отменена.")


@router.message(AdminContact.waiting, F.text)
async def admin_contact_send(m: Message, state: FSMContext, bot: Bot):
    if not is_admin_authorized(m.from_user.id):
        await state.clear()
        await m.answer("Доступ к админ-панели закрыт. Отправьте /admin и введите ключ.")
        return

    text = m.text.strip()
    if not text:
        await m.answer("Сообщение пустое. Введите текст или /cancel.")
        return
    if len(text) > 4000:
        await m.answer("Сообщение слишком длинное. Максимум — 4000 символов.")
        return

    data = await state.get_data()
    recipient_id = data.get("contact_tg_id")
    if recipient_id is None:
        await state.clear()
        await m.answer("Не удалось определить получателя. Откройте раздел «Призы» и выберите заявку ещё раз.")
        return
    if recipient_id == m.from_user.id:
        await state.clear()
        await m.answer("Это ваш аккаунт. Нельзя отправить сообщение самому себе.")
        return

    await bot.send_message(
        recipient_id,
        f"💬 Сообщение от организаторов по вашей заявке на приз:\n\n{text}",
        parse_mode=None,
    )
    await state.clear()
    await m.answer("✅ Сообщение отправлено получателю.")


@router.message(Command("gen"))
async def gen(m: Message):
    if not is_admin_authorized(m.from_user.id):
        await m.answer("Сначала войдите через /admin и введите ключ.")
        return
    parts = m.text.split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    if not 1 <= n <= 1000:
        await m.answer("Укажите количество от 1 до 1000: /gen 100")
        return
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов (0/O, 1/I)
    made = []
    with db() as c:
        while len(made) < n:
            code = "KR-" + "".join(secrets.choice(alphabet) for _ in range(6))
            try:
                c.execute("INSERT INTO codes(code) VALUES(?)", (code,))
                made.append(code)
            except sqlite3.IntegrityError:
                continue
    await m.answer_document(BufferedInputFile("\n".join(made).encode(), filename="codes.txt"), caption=f"Создано кодов: {n}")


@router.message(Command("stats"))
async def stats(m: Message):
    if not is_admin_authorized(m.from_user.id):
        await m.answer("Сначала войдите через /admin и введите ключ.")
        return
    with db() as c:
        regs = c.execute("SELECT COUNT(*) FROM users WHERE consent_at IS NOT NULL").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
        repeat = c.execute(
            "SELECT COUNT(*) FROM (SELECT used_by FROM codes WHERE used_by IS NOT NULL GROUP BY used_by HAVING COUNT(*)>=2)"
        ).fetchone()[0]
        active = c.execute("SELECT COUNT(DISTINCT used_by) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]
        claims = c.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    share = f"{repeat / active * 100:.0f}%" if active else "0%"
    await m.answer(
        f"📊 Статистика акции\nРегистраций: {regs}\nАктивных участников: {active}\n"
        f"Кодов использовано: {used}\nПовторные покупки (2+ кода): {repeat} ({share})\n"
        f"Оформлено призов: {claims}"
    )


# ---------------------------------------------------------------- напоминания
async def reminder_loop(bot: Bot):
    while True:
        try:
            now = int(time.time())
            limit = now - REMINDER_AFTER_DAYS * 86400
            with db() as c:
                rows = c.execute(
                    "SELECT tg_id, name FROM users WHERE last_code_at IS NOT NULL AND last_code_at < ? "
                    "AND (last_reminder_at IS NULL OR last_reminder_at < ?)",
                    (limit, limit),
                ).fetchall()
            for r in rows:
                try:
                    await bot.send_message(
                        r["tg_id"],
                        f"Сәлем, {r['name']}! Құрт таусылып қалған болар 😊 "
                        f"Тағы бір қаптама: 5–30 ұпай, сыйлыққа тағы бір қадам.\n\n"
                        f"Привет, {r['name']}! Наверное, курт уже закончился 😊 "
                        f"Ещё одна упаковка даст от 5 до 30 баллов и приблизит вас к подарку.",
                    )
                except Exception as e:  # пользователь мог заблокировать бота
                    logging.warning("reminder failed for %s: %s", r["tg_id"], e)
                with db() as c:
                    c.execute("UPDATE users SET last_reminder_at=? WHERE tg_id=?", (now, r["tg_id"]))
        except Exception:
            logging.exception("reminder loop error")
        await asyncio.sleep(3600)


# ---------------------------------------------------------------- запуск
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("ОШИБКА: задайте BOT_TOKEN в переменных окружения. ADMIN_ID тоже задайте своим Telegram ID.")
    if not ADMIN_ID:
        raise SystemExit("ОШИБКА: задайте ADMIN_ID в переменных окружения (ваш Telegram ID).")
    init_db()
    print(f">>> БД: {DB_PATH}")
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.get_me()
    print(f"\n>>> БОТ ЗАПУЩЕН: @{me.username}. Откройте его в Telegram и нажмите Start.")
    print(">>> Не закрывайте это окно. Остановить: Ctrl + C\n")
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
