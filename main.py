import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8691184053:AAFkIAbwi25nsZgIkXtS2Sc1hrcaddAU9q8").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "901985552").strip()
DB_PATH = os.getenv("DB_PATH", "barber_bookings.db").strip()
DEFAULT_SERVICE = "Запис у барбершоп"

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN before starting the bot.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("Set ADMIN_ID environment variable with numeric Telegram user id.")
ADMIN_ID = int(ADMIN_ID_RAW)

BARBERSHOP_NAME = "The Noble Cut"
BARBERSHOP_ADDRESS = "м. Київ, вул. Велика Васильківська, 24"
BARBERSHOP_PHONE = "+380 67 555 77 99"
BARBERSHOP_INSTAGRAM = "https://instagram.com/the.noble.cut"
BARBERSHOP_LATITUDE = 50.438868
BARBERSHOP_LONGITUDE = 30.516357

SERVICES = {
    "Чоловіча стрижка": "45 хв • Чиста форма, точна геометрія та бездоганний фініш.",
    "Стрижка + борода": "75 хв • Повний образ: стрижка, контур і догляд за бородою.",
    "Борода": "30 хв • Акуратне моделювання форми та преміальний догляд.",
    "Дитяча стрижка": "40 хв • Стильно, спокійно та комфортно для юного гостя.",
}
PRICES = {
    "Чоловіча стрижка": "700 грн",
    "Стрижка + борода": "1 100 грн",
    "Борода": "450 грн",
    "Дитяча стрижка": "550 грн",
}

TIME_SLOTS = [f"{hour:02d}:00" for hour in range(10, 19)]  # 10:00 ... 18:00
PHONE_RE = re.compile(r"^\+?\d{10,15}$")
router = Router()

MONTHS_UA = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}
WEEKDAYS_UA = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "Пʼятниця",
    5: "Субота",
    6: "Неділя",
}


class BookingForm(StatesGroup):
    choosing_date = State()
    choosing_time = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


class BookingDB:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, time)
            )
            """
        )
        self._conn.commit()

    async def get_user_booking(self, user_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_user_booking_sync, user_id)

    def _get_user_booking_sync(self, user_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT id, user_id, name, phone, service, date, time, created_at
            FROM bookings
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    async def get_booked_slots(self, day_iso: str) -> set[str]:
        return await asyncio.to_thread(self._get_booked_slots_sync, day_iso)

    def _get_booked_slots_sync(self, day_iso: str) -> set[str]:
        rows = self._conn.execute("SELECT time FROM bookings WHERE date = ?", (day_iso,)).fetchall()
        return {row["time"] for row in rows}

    async def add_booking(
        self,
        user_id: int,
        name: str,
        phone: str,
        service: str,
        day_iso: str,
        slot_time: str,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._add_booking_sync, user_id, name, phone, service, day_iso, slot_time
            )

    def _add_booking_sync(
        self, user_id: int, name: str, phone: str, service: str, day_iso: str, slot_time: str
    ) -> bool:
        try:
            self._conn.execute(
                """
                INSERT INTO bookings (user_id, name, phone, service, date, time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    phone,
                    service,
                    day_iso,
                    slot_time,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    async def cancel_booking(self, booking_id: int, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._cancel_booking_sync, booking_id, user_id)

    def _cancel_booking_sync(self, booking_id: int, user_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM bookings WHERE id = ? AND user_id = ?",
            (booking_id, user_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    async def replace_booking(
        self,
        old_booking_id: int,
        user_id: int,
        name: str,
        phone: str,
        service: str,
        day_iso: str,
        slot_time: str,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._replace_booking_sync,
                old_booking_id,
                user_id,
                name,
                phone,
                service,
                day_iso,
                slot_time,
            )

    def _replace_booking_sync(
        self,
        old_booking_id: int,
        user_id: int,
        name: str,
        phone: str,
        service: str,
        day_iso: str,
        slot_time: str,
    ) -> bool:
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "DELETE FROM bookings WHERE id = ? AND user_id = ?",
                (old_booking_id, user_id),
            )
            self._conn.execute(
                """
                INSERT INTO bookings (user_id, name, phone, service, date, time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    phone,
                    service,
                    day_iso,
                    slot_time,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    async def get_upcoming_bookings(self, limit: int = 30) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_upcoming_bookings_sync, limit)

    def _get_upcoming_bookings_sync(self, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, user_id, name, phone, service, date, time, created_at
            FROM bookings
            WHERE date >= ?
            ORDER BY date ASC, time ASC
            LIMIT ?
            """,
            (date.today().isoformat(), limit),
        ).fetchall()
        return [dict(row) for row in rows]


db = BookingDB(DB_PATH)


def format_human_date(day_iso: str) -> str:
    dt = datetime.strptime(day_iso, "%Y-%m-%d").date()
    return f"{dt.day} {MONTHS_UA[dt.month]}"


def normalize_phone(phone: str) -> str:
    value = re.sub(r"[^\d+]", "", phone.strip())
    if value.startswith("00"):
        value = f"+{value[2:]}"
    if value.startswith("0") and len(value) == 10:
        value = f"+38{value}"
    return value


def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.fullmatch(normalize_phone(phone)))


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✂️ Записатися"), KeyboardButton(text="📅 Мій запис")],
            [KeyboardButton(text="💈 Послуги"), KeyboardButton(text="💰 Ціни")],
            [KeyboardButton(text="📍 Адреса"), KeyboardButton(text="📞 Контакти")],
            [KeyboardButton(text="⭐ Відгуки")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Оберіть розділ",
    )


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад в меню")]],
        resize_keyboard=True,
    )


def booking_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Підтвердити", callback_data="booking:confirm"),
                InlineKeyboardButton(text="🔁 Змінити", callback_data="booking:restart"),
            ],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:abort")],
        ]
    )


def my_booking_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Скасувати запис",
                    callback_data=f"my:cancel:{booking_id}",
                ),
                InlineKeyboardButton(
                    text="🔁 Перенести запис",
                    callback_data=f"my:reschedule:{booking_id}",
                ),
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Оновити список", callback_data="admin:refresh")],
        ]
    )


async def build_dates_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []

    for offset in range(14):
        day = date.today() + timedelta(days=offset)
        day_iso = day.isoformat()
        if day.weekday() == 6:
            text = f"🚫 {WEEKDAYS_UA[day.weekday()]}"
            callback = "booking:day_off"
        else:
            booked_slots = await db.get_booked_slots(day_iso)
            free_slots = [slot for slot in TIME_SLOTS if slot not in booked_slots]
            if free_slots:
                text = f"✅ {day.day} {MONTHS_UA[day.month]}"
                callback = f"booking:date:{day_iso}"
            else:
                text = f"❌ {day.day} {MONTHS_UA[day.month]}"
                callback = "booking:full_day"

        pair.append(InlineKeyboardButton(text=text, callback_data=callback))
        if len(pair) == 2:
            rows.append(pair)
            pair = []

    if pair:
        rows.append(pair)

    rows.append([InlineKeyboardButton(text="🏠 У меню", callback_data="booking:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_times_keyboard(day_iso: str) -> InlineKeyboardMarkup:
    booked_slots = await db.get_booked_slots(day_iso)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for slot in TIME_SLOTS:
        if slot in booked_slots:
            text = f"❌ {slot}"
            callback = "booking:busy_time"
        else:
            text = f"✅ {slot}"
            callback = f"booking:time:{slot}"
        row.append(InlineKeyboardButton(text=text, callback_data=callback))
        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ До дат", callback_data="booking:back_dates")])
    rows.append([InlineKeyboardButton(text="🏠 У меню", callback_data="booking:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_preview(data: dict[str, Any]) -> str:
    return (
        "<b>Підтвердження запису</b>\n\n"
        f"📅 <b>Дата:</b> {format_human_date(data['date'])}\n"
        f"🕒 <b>Час:</b> {data['time']}\n"
        f"👤 <b>Ім'я:</b> {data['name']}\n"
        f"📞 <b>Телефон:</b> {data['phone']}\n"
        f"💈 <b>Послуга:</b> {data.get('service', DEFAULT_SERVICE)}\n\n"
        "Перевірте дані і підтвердіть запис."
    )


def admin_booking_message(data: dict[str, Any], tg_user_id: int, username: str | None) -> str:
    uname = f"@{username}" if username else "—"
    return (
        "<b>Нова заявка</b>\n\n"
        f"👤 Ім'я: {data['name']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"💈 Послуга: {data.get('service', DEFAULT_SERVICE)}\n"
        f"📅 Дата: {format_human_date(data['date'])}\n"
        f"🕒 Час: {data['time']}\n"
        f"🆔 User ID: {tg_user_id}\n"
        f"🔗 Username: {uname}"
    )


async def build_admin_dashboard_text(limit: int = 30) -> str:
    bookings = await db.get_upcoming_bookings(limit=limit)
    if not bookings:
        return (
            "<b>Адмін-панель</b>\n\n"
            "Активних майбутніх записів немає."
        )

    lines = [
        "<b>Адмін-панель</b>",
        "",
        f"Активних записів: <b>{len(bookings)}</b>",
        "",
        "<b>Найближчі записи:</b>",
        "",
    ]
    for idx, item in enumerate(bookings, start=1):
        lines.append(
            f"{idx}) 📅 {format_human_date(item['date'])} | 🕒 {item['time']}\n"
            f"👤 {item['name']} | 📞 {item['phone']}\n"
            f"💈 {item['service']} | 🆔 {item['user_id']}"
        )
        lines.append("")
    return "\n".join(lines).strip()


async def show_main_menu(message: Message, text: str | None = None) -> None:
    await message.answer(
        text
        or (
            f"🥂 <b>{BARBERSHOP_NAME}</b>\n\n"
            "Преміальний барбершоп для тих, хто любить стиль, точність і сервіс без компромісів.\n"
            "Оберіть потрібний розділ нижче."
        ),
        reply_markup=main_menu_keyboard(),
    )


async def show_user_booking(message: Message, user_id: int) -> None:
    booking = await db.get_user_booking(user_id)
    if not booking:
        await message.answer(
            "У вас поки немає активного запису.\nНатисніть «✂️ Записатися», щоб обрати дату й час.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.answer(
        (
            "<b>Ваш поточний запис</b>\n\n"
            f"📅 {format_human_date(booking['date'])}\n"
            f"🕒 {booking['time']}\n"
            f"👤 {booking['name']}\n"
            f"📞 {booking['phone']}\n"
            f"💈 {booking['service']}"
        ),
        reply_markup=my_booking_keyboard(int(booking["id"])),
    )


async def start_booking_flow(
    message: Message,
    state: FSMContext,
    reschedule_id: int | None = None,
    prefill_name: str | None = None,
    prefill_phone: str | None = None,
    prefill_service: str | None = None,
) -> None:
    if message.from_user is None:
        return

    existing = await db.get_user_booking(message.from_user.id)
    if existing and reschedule_id is None:
        await message.answer(
            "У вас уже є активний запис. Можете скасувати або перенести його.",
            reply_markup=main_menu_keyboard(),
        )
        await show_user_booking(message, message.from_user.id)
        return

    await state.clear()
    await state.set_state(BookingForm.choosing_date)
    await state.update_data(
        reschedule_id=reschedule_id,
        name=prefill_name,
        phone=prefill_phone,
        service=prefill_service or DEFAULT_SERVICE,
    )
    await message.answer(
        "<b>Оберіть дату візиту</b>\n\nПоказую найближчі 14 днів:",
        reply_markup=await build_dates_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Активного процесу запису немає.", reply_markup=main_menu_keyboard())
        return
    await state.clear()
    await message.answer("Поточний процес скасовано.", reply_markup=main_menu_keyboard())


@router.message(Command("mybooking"))
@router.message(F.text == "📅 Мій запис")
async def cmd_mybooking(message: Message) -> None:
    if message.from_user is None:
        return
    await show_user_booking(message, message.from_user.id)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("У вас немає доступу до адмін-панелі.")
        return
    await message.answer(await build_admin_dashboard_text(), reply_markup=admin_panel_keyboard())


@router.message(F.text == "⬅️ Назад в меню")
async def back_to_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message, "Головне меню відкрите. Оберіть потрібний розділ.")


@router.message(F.text == "✂️ Записатися")
async def start_booking(message: Message, state: FSMContext) -> None:
    await start_booking_flow(message, state)


@router.callback_query(F.data == "booking:menu")
async def callback_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.answer("Повертаю в меню.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:refresh")
async def callback_admin_refresh(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            await build_admin_dashboard_text(),
            reply_markup=admin_panel_keyboard(),
        )
    await callback.answer("Оновлено.")


@router.callback_query(F.data == "booking:day_off")
async def callback_day_off(callback: CallbackQuery) -> None:
    await callback.answer("Неділя — вихідний день. Оберіть іншу дату.", show_alert=True)


@router.callback_query(F.data == "booking:full_day")
async def callback_full_day(callback: CallbackQuery) -> None:
    await callback.answer("На цю дату вільних слотів уже немає.", show_alert=True)


@router.callback_query(F.data == "booking:busy_time")
async def callback_busy_time(callback: CallbackQuery) -> None:
    await callback.answer("Цей час уже зайнятий, оберіть інший.", show_alert=True)


@router.callback_query(F.data == "booking:back_dates")
async def callback_back_dates(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BookingForm.choosing_date)
    if callback.message:
        await callback.message.edit_text(
            "<b>Оберіть дату візиту</b>\n\nПоказую найближчі 14 днів:",
            reply_markup=await build_dates_keyboard(),
        )
    await callback.answer()


@router.callback_query(BookingForm.choosing_date, F.data.startswith("booking:date:"))
async def callback_date_selected(callback: CallbackQuery, state: FSMContext) -> None:
    day_iso = callback.data.split(":")[-1]
    selected_date = datetime.strptime(day_iso, "%Y-%m-%d").date()
    if selected_date.weekday() == 6:
        await callback.answer("Неділя вихідний день.", show_alert=True)
        return

    await state.update_data(date=day_iso)
    await state.set_state(BookingForm.choosing_time)
    if callback.message:
        await callback.message.edit_text(
            f"<b>Оберіть вільний час на {selected_date.day} {MONTHS_UA[selected_date.month]}</b>",
            reply_markup=await build_times_keyboard(day_iso),
        )
    await callback.answer()


@router.callback_query(BookingForm.choosing_time, F.data.startswith("booking:time:"))
async def callback_time_selected(callback: CallbackQuery, state: FSMContext) -> None:
    selected_time = callback.data.split("booking:time:", maxsplit=1)[1]
    data = await state.get_data()
    day_iso = data.get("date")
    if not day_iso:
        await callback.answer("Сесію втрачено. Почніть запис заново.", show_alert=True)
        return

    booked = await db.get_booked_slots(day_iso)
    if selected_time in booked:
        await callback.answer("Цей слот щойно зайняли. Оберіть інший.", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=await build_times_keyboard(day_iso))
        return

    await state.update_data(time=selected_time)
    await state.set_state(BookingForm.entering_name)
    if callback.message:
        await callback.message.answer("👤 Вкажіть ваше ім'я:", reply_markup=back_keyboard())
    await callback.answer()


@router.message(BookingForm.entering_name)
async def process_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Ім'я має містити мінімум 2 символи. Спробуйте ще раз.")
        return

    await state.update_data(name=name)
    await state.set_state(BookingForm.entering_phone)
    await message.answer("📞 Введіть номер телефону у форматі +380XXXXXXXXX або 0XXXXXXXXX.")


@router.message(BookingForm.entering_phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text or "")
    if not is_valid_phone(phone):
        await message.answer("Некоректний номер телефону. Спробуйте ще раз.")
        return

    await state.update_data(phone=phone)
    await state.set_state(BookingForm.confirming)
    data = await state.get_data()
    await message.answer(booking_preview(data), reply_markup=booking_confirm_keyboard())


@router.callback_query(BookingForm.confirming, F.data == "booking:restart")
async def callback_restart(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(BookingForm.choosing_date)
    await state.update_data(
        reschedule_id=data.get("reschedule_id"),
        service=data.get("service", DEFAULT_SERVICE),
    )
    if callback.message:
        await callback.message.answer(
            "Оновлюємо запис. Оберіть нову дату:",
            reply_markup=await build_dates_keyboard(),
        )
    await callback.answer()


@router.callback_query(BookingForm.confirming, F.data == "booking:abort")
async def callback_abort(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.answer("Процес запису скасовано.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(BookingForm.confirming, F.data == "booking:confirm")
async def callback_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    data = await state.get_data()
    day_iso = data.get("date")
    slot_time = data.get("time")
    name = data.get("name")
    phone = data.get("phone")
    service = data.get("service", DEFAULT_SERVICE)
    reschedule_id = data.get("reschedule_id")

    if not all([day_iso, slot_time, name, phone]):
        await state.clear()
        await callback.answer("Дані неповні. Почніть запис заново.", show_alert=True)
        return

    if reschedule_id:
        ok = await db.replace_booking(
            old_booking_id=int(reschedule_id),
            user_id=callback.from_user.id,
            name=name,
            phone=phone,
            service=service,
            day_iso=day_iso,
            slot_time=slot_time,
        )
    else:
        ok = await db.add_booking(
            user_id=callback.from_user.id,
            name=name,
            phone=phone,
            service=service,
            day_iso=day_iso,
            slot_time=slot_time,
        )

    if not ok:
        await state.set_state(BookingForm.choosing_time)
        if callback.message:
            await callback.message.answer(
                "На жаль, цей час вже зайнятий. Оберіть інший слот:",
                reply_markup=await build_times_keyboard(day_iso),
            )
        await callback.answer("Слот уже зайнятий.", show_alert=True)
        return

    await callback.message.answer(
        (
            "✅ <b>Ви успішно записані</b>\n"
            f"📅 {format_human_date(day_iso)}\n"
            f"🕒 {slot_time}"
        ),
        reply_markup=main_menu_keyboard(),
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            admin_booking_message(data, callback.from_user.id, callback.from_user.username),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Відкрити адмінку", callback_data="admin:refresh")]
                ]
            ),
        )
    except Exception as error:
        logging.exception("Cannot send booking to admin: %s", error)

    await state.clear()
    await callback.answer("Запис підтверджено.")


@router.callback_query(F.data.startswith("my:cancel:"))
async def callback_cancel_booking(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    booking_id = int(callback.data.split(":")[-1])
    deleted = await db.cancel_booking(booking_id, callback.from_user.id)
    if not deleted:
        await callback.answer("Запис не знайдено або вже скасовано.", show_alert=True)
        return

    if callback.message:
        await callback.message.answer(
            "✅ Запис скасовано.\nЦей час знову доступний для інших клієнтів.",
            reply_markup=main_menu_keyboard(),
        )
    await callback.answer("Запис скасовано.")


@router.callback_query(F.data.startswith("my:reschedule:"))
async def callback_reschedule_booking(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    booking_id = int(callback.data.split(":")[-1])
    current = await db.get_user_booking(callback.from_user.id)
    if not current or int(current["id"]) != booking_id:
        await callback.answer("Активний запис не знайдено.", show_alert=True)
        return

    if callback.message:
        await start_booking_flow(
            message=callback.message,
            state=state,
            reschedule_id=booking_id,
            prefill_name=current["name"],
            prefill_phone=current["phone"],
            prefill_service=current["service"],
        )
    await callback.answer("Оберіть нову дату для перенесення.")


@router.message(F.text == "💈 Послуги")
async def show_services(message: Message) -> None:
    lines = ["<b>Послуги</b>\n"]
    for service, desc in SERVICES.items():
        lines.append(f"• <b>{service}</b>\n{desc}")
    await message.answer("\n\n".join(lines), reply_markup=back_keyboard())


@router.message(F.text == "💰 Ціни")
async def show_prices(message: Message) -> None:
    lines = ["<b>Прайс</b>\n", "Преміальний результат за чесну вартість."]
    for service, price in PRICES.items():
        lines.append(f"• <b>{service}</b> — {price}")
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(F.text == "📍 Адреса")
async def show_address(message: Message) -> None:
    await message.answer(
        (
            "<b>Адреса</b>\n\n"
            f"📍 {BARBERSHOP_ADDRESS}\n"
            "Зручна локація в центрі міста, комфортна атмосфера та легкий доїзд."
        ),
        reply_markup=back_keyboard(),
    )
    await message.answer_location(
        latitude=BARBERSHOP_LATITUDE,
        longitude=BARBERSHOP_LONGITUDE,
        reply_markup=back_keyboard(),
    )


@router.message(F.text == "📞 Контакти")
async def show_contacts(message: Message) -> None:
    await message.answer(
        (
            "<b>Контакти</b>\n\n"
            f"📞 {BARBERSHOP_PHONE}\n"
            f"📲 Instagram: <a href=\"{BARBERSHOP_INSTAGRAM}\">@the.noble.cut</a>"
        ),
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


@router.message(F.text == "⭐ Відгуки")
async def show_reviews(message: Message) -> None:
    await message.answer(
        (
            "<b>Відгуки гостей</b>\n\n"
            "⭐️⭐️⭐️⭐️⭐️ «Рівень сервісу відчувається з першої хвилини. Дуже сильна стрижка.»\n\n"
            "⭐️⭐️⭐️⭐️⭐️ «Стильно, швидко й без зайвих слів. Саме той барбершоп, який хотілося знайти.»\n\n"
            "⭐️⭐️⭐️⭐️⭐️ «Бороду оформили ідеально. Вигляд дорогий і дуже акуратний.»"
        ),
        reply_markup=back_keyboard(),
    )


@router.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "Я допоможу із записом та підкажу потрібну інформацію. Скористайтеся меню нижче.",
        reply_markup=main_menu_keyboard(),
    )


async def main() -> None:
    await db.init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
