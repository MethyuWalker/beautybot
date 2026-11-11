import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
from web_admin import (
    start_web_admin,
    notify_master_confirmed,
    notify_masters_booking_event,
    format_masters_Line,
)

import sys
import webbrowser

# Если программа запущена как .exe, фиксируем рабочую папку рядом с .exe файлом,
# чтобы база beauty_salon.db и настройки .env сохранялись в той же папке
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))


def setup_first_run():
    """Если файла .env нет — запрашивает данные у клиента в простом диалоге и сохраняет их."""
    if os.path.exists(".env"):
        return

    print("=" * 60)
    print("✨ ПЕРВЫЙ ЗАПУСК БОТА САЛОНА КРАСОТЫ — НАСТРОЙКА ✨")
    print("=" * 60)
    print("Эти данные нужно ввести только один раз. Они сохранятся автоматически.\n")

    token = ""
    while not token or ":" not in token:
        token = input("1. Вставьте ТОКЕН вашего бота (от @BotFather) и нажмите Enter:\n> ").strip()
        if ":" not in token:
            print("⚠️ Ошибка: токен должен содержать двоеточие (например, 123456:ABC-DEF...). Попробуйте снова.\n")

    admin_id = ""
    while not admin_id.isdigit():
        admin_id = input("\n2. Введите ваш цифровой Telegram ID (узнать можно в @userinfobot):\n> ").strip()
        if not admin_id.isdigit():
            print("⚠️ Ошибка: ID должен состоять только из цифр.\n")

    phone = input("\n3. Введите телефон салона (например, +7 (999) 123-45-67):\n> ").strip()
    if not phone:
        phone = "+7 (999) 123-45-67"

    password = input("\n4. Придумайте пароль для входа в Веб-панель CRM (по умолчанию admin123):\n> ").strip()
    if not password:
        password = "admin123"

    with open(".env", "w", encoding="utf-8") as f:
        f.write(f"BOT_TOKEN={token}\n")
        f.write(f"ADMIN_IDS={admin_id}\n")
        f.write(f"SALON_PHONE={phone}\n")
        f.write(f"ADMIN_WEB_PASSWORD={password}\n")
        f.write("ADMIN_WEB_PORT=8080\n")

    print("\n✅ Настройки успешно сохранены в файл .env! Запускаем бота...\n")


setup_first_run()
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SALON_PHONE = os.getenv("SALON_PHONE", "+7 (999) 123-45-67").strip()
INITIAL_ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
_shutdown_notified = False
ADMIN_APPS_PAGE_SIZE = 5


# ==================== FSM СОСТОЯНИЯ ====================
class BookingState(StatesGroup):
    choosing_category = State()
    choosing_service = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    entering_comment = State()
    entering_name = State()
    entering_phone = State()


class ClientEditBookingState(StatesGroup):
    choosing_date = State()
    choosing_time = State()
    entering_comment = State()


class MasterTimeOffState(StatesGroup):
    choosing_type = State()
    entering_start = State()
    entering_end = State()
    entering_reason = State()


class AdminContactClientState(StatesGroup):
    entering_client_query = State()
    entering_first_message = State()


class AdminAdjustBonusState(StatesGroup):
    entering_client_query = State()
    entering_delta = State()


class AdminEditBookingState(StatesGroup):
    choosing_date = State()
    choosing_time = State()
    choosing_service = State()
    choosing_masters = State()
    entering_client_name = State()
    entering_client_phone = State()
    entering_client_username = State()
    entering_comment = State()


# ==================== ФОРМАТИРОВАНИЕ ЗАПИСЕЙ (БЕЗ ТАБЛИЦЫ + ПУСТАЯ СТРОКА + ЧЧ МЕС) ====================
async def check_user_blocked(user_id: int, username: Optional[str] = None) -> Optional[dict]:
    await db.upsert_user(tg_id=user_id, username=username or "")
    return await db.is_user_blacklisted(tg_id=user_id, username=username)


def _status_label(status: str) -> str:
    if status == "paid":
        return "💳 Оплачена"
    if status == "confirmed":
        return "✅ Подтверждена"
    if status == "cancelled":
        return "❌ Отменена"
    return "⏳ Ожидает подтверждения"


def format_single_appointment_block(a: dict) -> str:
    """Формирует текстовый блок одной записи с датой в формате '1 янв, 10:00'."""
    dt_formatted = db.format_ru_datetime(a.get("appointment_datetime", ""))
    st_text = _status_label(a.get("status", "pending"))
    uname_str = f" (@{a['client_username']})" if a.get("client_username") else ""
    duration_part = f", {a['service_duration']} мин" if a.get("service_duration") else ""
    price_part = f" — {a['service_price']} ₽" if a.get("service_price") is not None else ""
    comm = f"\n   💬 <i>{a['comment']}</i>" if a.get("comment") else ""

    return (
        f"📌 <b>Запись #{a['id']}</b> • <b>{dt_formatted}</b> ({st_text})\n"
        f"   💅 {a['service_name']}{price_part}{duration_part}\n"
        f"   👩‍🎨 Мастер: {format_masters_Line(a)}\n"
        f"   👤 {a['client_name']}{uname_str} | 📞 <code>{a['client_phone']}</code>{comm}"
    )


def format_appointments_table(apps: list) -> str:
    """Выводит список записей без таблиц, с пустой строкой между записями и датой ЧЧ МЕС."""
    if not apps:
        return "Записей нет."
    blocks = [format_single_appointment_block(a) for a in apps]
    return "\n\n".join(blocks)


# ==================== КЛАВИАТУРЫ ====================
async def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📅 Записаться на услугу")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="🎁 Мои бонусы")],
        [KeyboardButton(text="👩‍🎨 Наши мастера"), KeyboardButton(text="💬 Связь с администратором")],
    ]
    master = await db.get_master_by_tg_id(user_id)
    if master:
        buttons.append([KeyboardButton(text="👩‍🎨 Кабинет мастера")])

    admin = await db.get_admin_by_tg_id(user_id)
    if admin:
        shift_btn_text = (
            "🔴 Уйти со смены" if admin.get("on_shift") else "🟢 Выйти на смену"
        )
        buttons.append(
            [
                KeyboardButton(text="🛠 Панель администратора"),
                KeyboardButton(text=shift_btn_text),
            ]
        )
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def admin_booking_actions_kb(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить (звонок выполнен)",
                    callback_data=f"adm_confirm_{app_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать запись",
                    callback_data=f"adm_app_view_{app_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить запись",
                    callback_data=f"adm_cancel_{app_id}",
                )
            ],
        ]
    )


def admin_panel_kb(on_shift: bool) -> InlineKeyboardMarkup:
    shift_text = (
        "🔴 Уйти со смены (отключить уведомления)"
        if on_shift
        else "🟢 Выйти на смену (включить уведомления)"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=shift_text,
                    callback_data="adm_toggle_shift",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Все записи и редактирование",
                    callback_data="adm_apps_list_upcoming_0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Сводка записей на завтра",
                    callback_data="adm_tomorrow_table",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✉️ Написать клиенту (начать диалог)",
                    callback_data="adm_contact_client",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎁 Изменить бонусы клиента (+ / -)",
                    callback_data="adm_adjust_bonus",
                )
            ],
        ]
    )


def master_cabinet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Мои подтвержденные записи", callback_data="mst_cab_confirmed")],
            [
                InlineKeyboardButton(text="📆 Расписание на сегодня", callback_data="mst_cab_today"),
                InlineKeyboardButton(text="📅 Заявки на завтра", callback_data="mst_cab_tomorrow"),
            ],
            [InlineKeyboardButton(text="🏖 Мои отгулы и отпуски", callback_data="mst_cab_timeoffs")],
            [InlineKeyboardButton(text="➕ Взять отгул / отпуск", callback_data="mst_cab_add_off")],
        ]
    )


# ==================== СТАРТ И БОНУСЫ ====================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
    )
    bl = await db.is_user_blacklisted(
        tg_id=message.from_user.id, username=message.from_user.username
    )
    if bl:
        reason_line = f"\nПричина: {bl['reason']}" if bl.get("reason") else ""
        await message.answer(
            f"⛔ Ваш аккаунт находится в черном списке салона.{reason_line}"
        )
        return

    bonus_pts = await db.get_client_bonus_balance(
        client_tg_id=message.from_user.id, client_username=message.from_user.username or ""
    )
    await message.answer(
        "✨ <b>Добро пожаловать в салон красоты!</b>\n\n"
        "Здесь вы можете записаться на услугу с точностью до 15 минут и копить бонусные баллы.\n"
        f"🎁 <b>Ваш бонусный баланс:</b> {bonus_pts} баллов (1 балл = 1 ₽).\n"
        f"📞 <b>Телефон салона:</b> {SALON_PHONE}\n"
        "💡 <i>При оплате наличными вам начисляется кэшбэк 5% баллами от стоимости услуги!</i>",
        parse_mode="HTML",
        reply_markup=await main_menu_kb(message.from_user.id),
    )


@router.message(F.text == "🎁 Мои бонусы")
async def show_my_bonuses(message: Message):
    pts = await db.get_client_bonus_balance(
        client_tg_id=message.from_user.id, client_username=message.from_user.username or ""
    )
    await message.answer(
        f"🎁 <b>Ваш бонусный счет: {pts} баллов</b> (1 балл = 1 ₽)\n\n"
        f"• При <b>оплате наличными</b> в салоне вам автоматически начисляется <b>5% баллами</b> от стоимости услуги.\n"
        f"• Списать накопленные баллы можно в любое время как при наличной, так и при безналичной оплате!",
        parse_mode="HTML",
    )


@router.message(F.text == "👩‍🎨 Наши мастера")
async def show_masters(message: Message):
    masters = await db.get_all_masters(only_active=True)
    if not masters:
        await message.answer("Список мастеров пока пуст.")
        return
    lines = ["👩‍🎨 <b>Наши мастера:</b>\n"]
    for m in masters:
        cats = ", ".join(m.get("categories", []))
        lines.append(f"• <b>{m['name']}</b> — {m['specialization']} ({cats})")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ==================== ЗАПИСЬ КЛИЕНТА (КЛИЕНТ ВЫБИРАЕТ ТОЛЬКО 1 МАСТЕРА) ====================
@router.message(F.text == "📅 Записаться на услугу")
async def start_booking(message: Message, state: FSMContext):
    await state.clear()
    bl = await check_user_blocked(message.from_user.id, message.from_user.username)
    if bl:
        reason_line = f"\nПричина: {bl['reason']}" if bl.get("reason") else ""
        await message.answer(
            f"⛔ Вы не можете записаться, так как ваш аккаунт заблокирован.{reason_line}"
        )
        return

    categories = await db.get_categories()
    if not categories:
        await message.answer("Категории услуг пока не добавлены.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}")]
            for cat in categories
        ]
    )
    await state.set_state(BookingState.choosing_category)
    await message.answer("Выберите категорию услуги:", reply_markup=kb)


@router.callback_query(BookingState.choosing_category, F.data.startswith("cat_"))
async def select_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("cat_", 1)[1]
    await state.update_data(category=category)
    services = await db.get_services_by_category(category)
    if not services:
        await callback.answer("В этой категории пока нет услуг.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{s['name']} — {s['price']} ₽ ({s['duration_minutes']} мин)",
                    callback_data=f"srv_{s['id']}",
                )
            ]
            for s in services
        ]
    )
    await state.set_state(BookingState.choosing_service)
    await callback.message.edit_text(
        f"Категория: <b>{category}</b>\nВыберите услугу:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(BookingState.choosing_service, F.data.startswith("srv_"))
async def select_service(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("srv_", 1)[1])
    service = await db.get_service_by_id(service_id)
    await state.update_data(
        service_id=service_id,
        service_name=service["name"],
        service_duration=service["duration_minutes"],
        service_price=service["price"],
    )

    masters = await db.get_masters_for_service(service_id)
    if not masters:
        await callback.message.edit_text("Для этой услуги сейчас нет доступных мастеров.")
        await state.clear()
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"👩‍🎨 {m['name']}", callback_data=f"mst_{m['id']}")]
            for m in masters
        ]
    )
    await state.set_state(BookingState.choosing_master)
    await callback.message.edit_text(
        f"Услуга: <b>{service['name']}</b>\nВыберите мастера:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(BookingState.choosing_master, F.data.startswith("mst_"))
async def select_master(callback: CallbackQuery, state: FSMContext):
    master_id = int(callback.data.split("mst_", 1)[1])
    master = await db.get_master_by_id(master_id)
    await state.update_data(master_id=master_id, master_name=master["name"])

    today = datetime.now().date()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        label = f"{db.format_ru_date(d_str)} ({d.strftime('%a')})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"date_{d_str}")])

    await state.set_state(BookingState.choosing_date)
    await callback.message.edit_text(
        f"Мастер: <b>{master['name']}</b>\nВыберите удобную дату:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(BookingState.choosing_date, F.data.startswith("date_"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("date_", 1)[1]
    data = await state.get_data()
    slots = await db.get_available_slots(
        [data["master_id"]],
        date_str,
        data["service_duration"],
    )

    if not slots:
        await callback.answer(
            "На выбранную дату нет свободных слотов (мастер занят или в отгуле).",
            show_alert=True,
        )
        return

    await state.update_data(date_str=date_str)
    rows, row = [], []
    for s in slots:
        row.append(InlineKeyboardButton(text=s, callback_data=f"time_{s}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await state.set_state(BookingState.choosing_time)
    await callback.message.edit_text(
        f"Дата: <b>{db.format_ru_date(date_str)}</b>\nВыберите время (шаг 15 минут):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(BookingState.choosing_time, F.data.startswith("time_"))
async def select_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("time_", 1)[1]
    await state.update_data(time_str=time_str)
    await state.set_state(BookingState.entering_comment)
    await callback.message.edit_text(
        "💬 Введите комментарий к записи (пожелания, дизайн, снятие старого покрытия) "
        "или отправьте <code>-</code>, чтобы пропустить:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BookingState.entering_comment)
async def enter_comment(message: Message, state: FSMContext):
    raw = message.text.strip()
    comment = "" if raw == "-" else raw
    await state.update_data(comment=comment)
    await state.set_state(BookingState.entering_name)
    await message.answer("Введите ваше имя:")


@router.message(BookingState.entering_name)
async def enter_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await state.set_state(BookingState.entering_phone)
    await message.answer(
        "Отправьте ваш номер телефона кнопкой ниже или введите вручную:",
        reply_markup=kb,
    )


@router.message(BookingState.entering_phone)
async def enter_phone(message: Message, state: FSMContext, bot: Bot):
    bl = await check_user_blocked(message.from_user.id, message.from_user.username)
    if bl:
        await state.clear()
        await message.answer("⛔ Ваш аккаунт находится в черном списке салона.")
        return

    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = "+" + phone
    else:
        phone = message.text.strip()

    data = await state.get_data()
    appointment_dt = f"{data['date_str']} {data['time_str']}"

    if not await db.is_slot_free_for_master(
            [data["master_id"]],
            appointment_dt,
            data["service_duration"],
    ):
        await state.clear()
        await message.answer(
            "⚠️ К сожалению, выбранное время только что заняли. Пожалуйста, выберите другое время.",
            reply_markup=await main_menu_kb(message.from_user.id),
        )
        return

    await db.upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=data["client_name"],
        phone=phone,
    )

    app_id = await db.create_appointment(
        client_tg_id=message.from_user.id,
        client_username=message.from_user.username or "",
        client_name=data["client_name"],
        client_phone=phone,
        master_id=data["master_id"],
        master_ids=[data["master_id"]],
        service_id=data["service_id"],
        appointment_datetime=appointment_dt,
        comment=data.get("comment", ""),
    )
    details = await db.get_appointment_details(app_id)
    comment_line = f"\n💬 <b>Комментарий:</b> {details['comment']}" if details.get("comment") else ""
    dt_ru = db.format_ru_datetime(appointment_dt)

    await state.clear()
    await message.answer(
        f"✅ <b>Вы успешно записаны!</b>\n\n"
        f"📌 <b>Услуга:</b> {details['service_name']} ({details['service_price']} ₽)\n"
        f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(details)}\n"
        f"🕒 <b>Дата и время:</b> {dt_ru}\n"
        f"📞 <b>Телефон:</b> {phone}{comment_line}",
        parse_mode="HTML",
        reply_markup=await main_menu_kb(message.from_user.id),
    )

    uname_str = f" (@{message.from_user.username})" if message.from_user.username else ""
    admin_text = (
        f"🆕 <b>Новая запись #{app_id}</b>\n\n"
        f"👤 <b>Клиент:</b> {details['client_name']}{uname_str}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"💅 <b>Услуга:</b> {details['service_name']} ({details['service_price']} ₽)\n"
        f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(details)}\n"
        f"🕒 <b>Дата и время:</b> {dt_ru}{comment_line}"
    )
    for adm in await db.get_admins(only_on_shift=True):
        try:
            await bot.send_message(
                adm["tg_id"],
                admin_text,
                parse_mode="HTML",
                reply_markup=admin_booking_actions_kb(app_id),
            )
        except Exception:
            pass

    await notify_masters_booking_event(bot, details, "🆕 <b>Новая запись к вам (ожидает подтверждения)</b>")


# ==================== МОИ ЗАПИСИ + РЕДАКТИРОВАНИЕ ЗАПИСИ КЛИЕНТОМ ====================
@router.message(F.text == "📋 Мои записи")
async def my_appointments(message: Message):
    apps = await db.get_client_appointments(message.from_user.id)
    pts = await db.get_client_bonus_balance(
        client_tg_id=message.from_user.id, client_username=message.from_user.username or ""
    )
    if not apps:
        await message.answer(
            f"У вас пока нет активных предстоящих записей.\n🎁 Ваш бонусный баланс: <b>{pts} баллов</b>.",
            parse_mode="HTML",
        )
        return

    for a in apps:
        status_label = _status_label(a["status"])
        comment_line = f"\n💬 <b>Комментарий:</b> {a['comment']}" if a.get("comment") else ""
        dt_ru = db.format_ru_datetime(a["appointment_datetime"])
        text = (
            f"📌 <b>Запись #{a['id']}</b> ({status_label})\n"
            f"💅 <b>Услуга:</b> {a['service_name']} ({a['service_price']} ₽)\n"
            f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(a)}\n"
            f"🕒 <b>Когда:</b> {dt_ru}{comment_line}\n"
            f"🎁 <b>Доступно бонусов для списания:</b> {pts} баллов"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Редактировать время / комментарий",
                        callback_data=f"client_edit_{a['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отменить запись",
                        callback_data=f"client_cancel_{a['id']}",
                    )
                ],
            ]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("client_edit_"))
async def client_edit_booking_start(callback: CallbackQuery, state: FSMContext):
    bl = await check_user_blocked(callback.from_user.id, callback.from_user.username)
    if bl:
        await callback.answer("Ваш аккаунт заблокирован.", show_alert=True)
        return

    app_id = int(callback.data.split("client_edit_", 1)[1])
    details = await db.get_appointment_details(app_id)
    if not details or details["status"] == "cancelled":
        await callback.answer("Запись неактуальна.", show_alert=True)
        return

    await state.update_data(edit_app_id=app_id)
    today = datetime.now().date()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        label = f"{db.format_ru_date(d_str)} ({d.strftime('%a')})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"cedit_date_{d_str}")])

    await state.set_state(ClientEditBookingState.choosing_date)
    await callback.message.answer(
        f"✏️ <b>Редактирование записи #{app_id}</b>\nВыберите новую дату визита:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(ClientEditBookingState.choosing_date, F.data.startswith("cedit_date_"))
async def client_edit_booking_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("cedit_date_", 1)[1]
    data = await state.get_data()
    details = await db.get_appointment_details(data["edit_app_id"])

    slots = await db.get_available_slots(
        details.get("master_ids") or [details["master_id"]],
        date_str,
        details["service_duration"],
        exclude_appointment_id=details["id"],
        )
    if not slots:
        await callback.answer("На этот день нет свободных окон у мастеров.", show_alert=True)
        return

    await state.update_data(new_date=date_str)
    rows, row = [], []
    for s in slots:
        row.append(InlineKeyboardButton(text=s, callback_data=f"cedit_time_{s}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await state.set_state(ClientEditBookingState.choosing_time)
    await callback.message.edit_text(
        f"Выберите новое время на <b>{db.format_ru_date(date_str)}</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(ClientEditBookingState.choosing_time, F.data.startswith("cedit_time_"))
async def client_edit_booking_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("cedit_time_", 1)[1]
    await state.update_data(new_time=time_str)
    await state.set_state(ClientEditBookingState.entering_comment)
    await callback.message.edit_text(
        "💬 Введите новый комментарий к записи (или отправьте <code>-</code>, чтобы оставить прежний):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ClientEditBookingState.entering_comment)
async def client_edit_booking_finish(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    app_id = data["edit_app_id"]
    old = await db.get_appointment_details(app_id)
    new_dt = f"{data['new_date']} {data['new_time']}"
    raw_comm = message.text.strip()
    new_comment = old.get("comment", "") if raw_comm == "-" else raw_comm
    m_ids = old.get("master_ids") or [old["master_id"]]

    if not await db.is_slot_free_for_master(
            m_ids,
            new_dt,
            old["service_duration"],
            exclude_appointment_id=app_id,
    ):
        await state.clear()
        await message.answer("⚠️ Это время уже занято. Попробуйте снова.")
        return

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=old["client_name"],
        client_phone=old["client_phone"],
        master_id=m_ids[0],
        second_master_id=m_ids[1] if len(m_ids) > 1 else None,
        master_ids=m_ids,
        service_id=old["service_id"],
        appointment_datetime=new_dt,
        comment=new_comment,
        client_username=old.get("client_username", ""),
    )
    updated = await db.get_appointment_details(app_id)
    await state.clear()

    comment_line = f"\n💬 <b>Комментарий:</b> {updated['comment']}" if updated.get("comment") else ""
    new_dt_ru = db.format_ru_datetime(updated["appointment_datetime"])
    old_dt_ru = db.format_ru_datetime(old["appointment_datetime"])

    await message.answer(
        f"✅ <b>Запись #{app_id} успешно обновлена!</b>\n\n"
        f"🕒 <b>Новое время:</b> {new_dt_ru}\n"
        f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(updated)}{comment_line}",
        parse_mode="HTML",
    )

    for adm in await db.get_admins(only_on_shift=True):
        try:
            await bot.send_message(
                adm["tg_id"],
                f"✏️ <b>Клиент изменил запись #{app_id} в боте!</b>\n\n"
                f"👤 <b>Клиент:</b> {updated['client_name']} ({updated['client_phone']})\n"
                f"🕒 <b>Было:</b> {old_dt_ru} -> <b>Стало:</b> {new_dt_ru}\n"
                f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(updated)}{comment_line}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await notify_masters_booking_event(bot, updated, "✏️ <b>Клиент перенес/обновил запись</b>")


@router.callback_query(F.data.startswith("client_cancel_"))
async def client_cancel_appointment(callback: CallbackQuery, bot: Bot):
    app_id = int(callback.data.split("client_cancel_", 1)[1])
    details = await db.get_appointment_details(app_id)
    if not details or details["status"] == "cancelled":
        await callback.answer("Запись уже отменена.", show_alert=True)
        return

    await db.update_appointment_status(app_id, "cancelled")
    dt_ru = db.format_ru_datetime(details["appointment_datetime"])
    await callback.message.edit_text(
        f"❌ Ваша запись #{app_id} ({details['service_name']}, {dt_ru}) отменена."
    )

    for adm in await db.get_admins(only_on_shift=True):
        try:
            await bot.send_message(
                adm["tg_id"],
                f"⚠️ <b>Клиент отменил запись #{app_id}</b>\n"
                f"👤 {details['client_name']} ({details['client_phone']})\n"
                f"👩‍🎨 Мастер: {format_masters_Line(details)} | 🕒 {dt_ru}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await notify_masters_booking_event(bot, details, "❌ <b>Внимание: клиент отменил запись к вам!</b>")
    await callback.answer()


@router.callback_query(F.data.startswith("client_confirm_"))
async def client_confirm_appointment(callback: CallbackQuery, bot: Bot):
    app_id = int(callback.data.split("client_confirm_", 1)[1])
    details = await db.get_appointment_details(app_id)
    if not details or details["status"] == "cancelled":
        await callback.answer("Запись неактуальна.", show_alert=True)
        return

    await db.update_appointment_status(app_id, "confirmed")
    dt_ru = db.format_ru_datetime(details["appointment_datetime"])
    await callback.message.edit_text(
        f"✅ Спасибо! Ваша запись #{app_id} на {dt_ru} подтверждена."
    )
    await notify_master_confirmed(bot, details)

    for adm in await db.get_admins(only_on_shift=True):
        try:
            await bot.send_message(
                adm["tg_id"],
                f"✅ <b>Клиент подтвердил запись #{app_id} через бота!</b>\n"
                f"👤 {details['client_name']} ({details['client_phone']})\n"
                f"👩‍🎨 Мастер: {format_masters_Line(details)} | 🕒 {dt_ru}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await callback.answer()


# ==================== РОЛЬ МАСТЕРА В БОТЕ ====================
@router.message(F.text == "👩‍🎨 Кабинет мастера")
async def open_master_cabinet(message: Message):
    master = await db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Роль мастера не привязана к вашему Telegram ID.")
        return

    await message.answer(
        f"👩‍🎨 <b>Кабинет мастера: {master['name']}</b>\n"
        f"Специализация: {master['specialization']}\n\n"
        f"Выберите действие:",
        parse_mode="HTML",
        reply_markup=master_cabinet_kb(),
    )


@router.callback_query(F.data == "mst_cab_confirmed")
async def mst_show_confirmed(callback: CallbackQuery):
    master = await db.get_master_by_tg_id(callback.from_user.id)
    if not master:
        return
    apps = await db.get_master_appointments(master["id"], only_confirmed=True)
    if not apps:
        await callback.message.answer("У вас пока нет подтвержденных предстоящих записей.")
        await callback.answer()
        return

    text = (
            f"✅ <b>Подтвержденные записи ({master['name']}):</b>\n\n"
            + format_appointments_table(apps)
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"mst_cab_today", "mst_cab_tomorrow"}))
async def mst_show_day_schedule(callback: CallbackQuery):
    master = await db.get_master_by_tg_id(callback.from_user.id)
    if not master:
        return
    target = datetime.now().date()
    is_tomorrow = callback.data == "mst_cab_tomorrow"
    if is_tomorrow:
        target += timedelta(days=1)
    d_str = target.strftime("%Y-%m-%d")
    d_ru = db.format_ru_date(d_str)
    label = "на завтра" if is_tomorrow else "на сегодня"

    apps = await db.get_master_appointments(master["id"], date_str=d_str, only_confirmed=False)
    if not apps:
        await callback.message.answer(f"📅 На {d_ru} ({label}) записей к вам нет.")
        await callback.answer()
        return

    text = (
            f"📆 <b>Ваше расписание {label} ({d_ru}) — {master['name']}:</b>\n\n"
            + format_appointments_table(apps)
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "mst_cab_timeoffs")
async def mst_show_timeoffs(callback: CallbackQuery):
    master = await db.get_master_by_tg_id(callback.from_user.id)
    if not master:
        return
    offs = await db.get_master_time_offs(master["id"])
    if not offs:
        await callback.message.answer("У вас нет активных отгулов или отпусков.")
        await callback.answer()
        return

    for o in offs:
        label = "✈️ Отпуск" if o["type"] == "vacation" else "🕒 Отгул"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Удалить", callback_data=f"mst_del_off_{o['id']}")]
            ]
        )
        await callback.message.answer(
            f"{label}: <b>{db.format_ru_datetime(o['start_datetime'])} — {db.format_ru_datetime(o['end_datetime'])}</b>\n"
            f"Причина: {o['reason'] or '—'}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("mst_del_off_"))
async def mst_delete_timeoff(callback: CallbackQuery):
    off_id = int(callback.data.split("mst_del_off_", 1)[1])
    await db.delete_master_time_off(off_id)
    await callback.message.edit_text("✅ Отгул/отпуск удален.")
    await callback.answer()


@router.callback_query(F.data == "mst_cab_add_off")
async def mst_add_timeoff_start(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕒 Отгул (часы или день)", callback_data="offtype_day_off")],
            [InlineKeyboardButton(text="✈️ Отпуск (несколько дней)", callback_data="offtype_vacation")],
        ]
    )
    await state.set_state(MasterTimeOffState.choosing_type)
    await callback.message.answer("Выберите тип отсутствия:", reply_markup=kb)
    await callback.answer()


@router.callback_query(MasterTimeOffState.choosing_type, F.data.startswith("offtype_"))
async def mst_add_timeoff_type(callback: CallbackQuery, state: FSMContext):
    off_type = callback.data.split("offtype_", 1)[1]
    await state.update_data(off_type=off_type)
    await state.set_state(MasterTimeOffState.entering_start)
    await callback.message.answer(
        "Введите дату и время начала в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n"
        "Например: <code>2026-11-10 10:00</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(MasterTimeOffState.entering_start)
async def mst_add_timeoff_start_dt(message: Message, state: FSMContext):
    await state.update_data(start_dt=message.text.strip())
    await state.set_state(MasterTimeOffState.entering_end)
    await message.answer(
        "Введите дату и время окончания в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n"
        "Например: <code>2026-11-10 20:00</code>",
        parse_mode="HTML",
    )


@router.message(MasterTimeOffState.entering_end)
async def mst_add_timeoff_end_dt(message: Message, state: FSMContext):
    await state.update_data(end_dt=message.text.strip())
    await state.set_state(MasterTimeOffState.entering_reason)
    await message.answer("Укажите причину/комментарий (или отправьте <code>-</code>):", parse_mode="HTML")


@router.message(MasterTimeOffState.entering_reason)
async def mst_add_timeoff_finish(message: Message, state: FSMContext):
    master = await db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await state.clear()
        return
    reason = "" if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    await db.add_master_time_off(
        master_id=master["id"],
        off_type=data["off_type"],
        start_datetime=data["start_dt"],
        end_datetime=data["end_dt"],
        reason=reason,
    )
    await state.clear()
    await message.answer(
        f"✅ Запись об отсутствии сохранена!\n"
        f"Период: <b>{db.format_ru_datetime(data['start_dt'])} — {db.format_ru_datetime(data['end_dt'])}</b>",
        parse_mode="HTML",
    )


# ==================== СВЯЗЬ С АДМИНИСТРАТОРОМ (ТИКЕТЫ) ====================
@router.message(F.text == "💬 Связь с администратором")
async def start_support_ticket(message: Message, bot: Bot):
    bl = await check_user_blocked(message.from_user.id, message.from_user.username)
    if bl:
        await message.answer("⛔ Ваш аккаунт заблокирован.")
        return

    existing = await db.get_client_open_ticket(message.from_user.id)
    if existing:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Завершить диалог",
                        callback_data=f"close_ticket_{existing['id']}",
                    )
                ]
            ]
        )
        await message.answer(
            f"У вас уже открыт диалог #{existing['id']} с администратором. "
            f"Просто напишите ваше сообщение в чат!",
            reply_markup=kb,
        )
        return

    username = message.from_user.username or message.from_user.full_name or str(message.from_user.id)
    ticket_id = await db.create_support_ticket(
        client_tg_id=message.from_user.id,
        client_username=username,
        client_name=message.from_user.full_name or username,
    )

    client_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Отменить / Закрыть обращение",
                    callback_data=f"close_ticket_{ticket_id}",
                )
            ]
        ]
    )
    await message.answer(
        "📨 Запрос отправлен администраторам! Ожидайте подключения, либо сразу напишите ваш вопрос ниже:",
        reply_markup=client_kb,
    )

    adm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✋ Взять тикет",
                    callback_data=f"take_ticket_{ticket_id}",
                )
            ]
        ]
    )
    for adm in await db.get_admins(only_on_shift=True):
        try:
            await bot.send_message(
                adm["tg_id"],
                f"🔔 <b>Тикет #{ticket_id}:</b> С вами хочет связаться клиент @{username}.",
                parse_mode="HTML",
                reply_markup=adm_kb,
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("take_ticket_"))
async def admin_take_ticket_cb(callback: CallbackQuery, bot: Bot):
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    ticket_id = int(callback.data.split("take_ticket_", 1)[1])
    ticket = await db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        await callback.answer("Тикет уже закрыт.", show_alert=True)
        return

    admin_name = admin["name"]
    await db.take_support_ticket(ticket_id, callback.from_user.id, admin_name)
    await db.log_admin_action(admin_name, "Взятие тикета в боте", f"Тикет #{ticket_id}")

    close_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть тикет",
                    callback_data=f"close_ticket_{ticket_id}",
                )
            ]
        ]
    )
    await callback.message.edit_text(
        f"✅ Вы взяли тикет #{ticket_id} (клиент @{ticket['client_username']}).\n"
        f"Все сообщения, которые вы отправите сейчас боту, будут пересланы клиенту.",
        reply_markup=close_kb,
    )

    try:
        await bot.send_message(
            ticket["client_tg_id"],
            f"👩‍💼 С вами на связи администратор <b>{admin_name}</b>",
            parse_mode="HTML",
            reply_markup=close_kb,
        )
    except Exception:
        pass
    await callback.answer("Тикет принят!")


@router.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket_cb(callback: CallbackQuery, bot: Bot):
    ticket_id = int(callback.data.split("close_ticket_", 1)[1])
    ticket = await db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        await callback.answer("Диалог уже закрыт.", show_alert=True)
        return

    await db.close_support_ticket(ticket_id)
    closed_by_client = callback.from_user.id == ticket["client_tg_id"]

    await callback.message.edit_text(f"🔒 Тикет #{ticket_id} закрыт.")

    if closed_by_client:
        if ticket.get("admin_tg_id"):
            try:
                await bot.send_message(
                    ticket["admin_tg_id"],
                    f"🔒 Клиент @{ticket['client_username']} завершил диалог по тикету #{ticket_id}.",
                )
            except Exception:
                pass
    else:
        await db.log_admin_action(
            callback.from_user.full_name or "Админ",
            "Закрытие тикета в боте",
            f"Тикет #{ticket_id}",
            )
        try:
            await bot.send_message(
                ticket["client_tg_id"],
                f"🔒 Администратор завершил диалог по обращению #{ticket_id}. Спасибо!",
            )
        except Exception:
            pass
    await callback.answer()


# ==================== СМЕНА АДМИНИСТРАТОРА И АДМИН-ПАНЕЛЬ В БОТЕ ====================
def _admin_panel_text(admin: dict) -> str:
    on_shift = bool(admin.get("on_shift"))
    shift_badge = (
        "🟢 <b>Вы на смене</b> (уведомления о записях и тикетах приходят)"
        if on_shift
        else "🔴 <b>Вы не на смене</b> (уведомления отключены)"
    )
    return (
        f"🛠 <b>Панель управления салоном красоты</b>\n"
        f"Статус: {shift_badge}\n\n"
        f"🌐 <b>Веб-панель CRM:</b> <code>http://127.0.0.1:8080</code>\n"
        f"Выберите нужное действие ниже:"
    )


@router.message(F.text.in_({"🟢 Выйти на смену", "Выйти на смену", "🔴 Уйти со смены", "Уйти со смены"}))
async def handle_shift_reply_button(message: Message):
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    if not admin:
        await message.answer("⛔ У вас нет прав администратора.")
        return

    want_on_shift = "Выйти" in message.text
    await db.set_admin_shift(message.from_user.id, want_on_shift)
    await db.log_admin_action(
        admin["name"],
        "Смена администратора",
        "Вышел на смену" if want_on_shift else "Ушел со смены",
    )
    status_msg = (
        "🟢 <b>Вы вышли на смену!</b> Теперь вам будут приходить все уведомления о новых записях, изменениях и тикетах."
        if want_on_shift
        else "🔴 <b>Вы ушли со смены.</b> Уведомления от бота временно отключены до следующего выхода на смену."
    )
    await message.answer(
        status_msg,
        parse_mode="HTML",
        reply_markup=await main_menu_kb(message.from_user.id),
    )


@router.callback_query(F.data == "adm_toggle_shift")
async def handle_shift_inline_toggle(callback: CallbackQuery):
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        await callback.answer("⛔ У вас нет прав администратора.", show_alert=True)
        return

    new_state = await db.toggle_admin_shift(callback.from_user.id)
    await db.log_admin_action(
        admin["name"],
        "Смена администратора",
        "Вышел на смену" if new_state else "Ушел со смены",
    )
    updated_admin = await db.get_admin_by_tg_id(callback.from_user.id)
    await callback.message.edit_text(
        _admin_panel_text(updated_admin),
        parse_mode="HTML",
        reply_markup=admin_panel_kb(new_state),
    )
    await callback.message.answer(
        "🟢 Вы вышли на смену (уведомления включены)."
        if new_state
        else "🔴 Вы ушли со смены (уведомления отключены).",
        reply_markup=await main_menu_kb(callback.from_user.id),
    )
    await callback.answer()


@router.message(Command("admin"))
@router.message(F.text == "🛠 Панель администратора")
async def open_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    if not admin:
        await message.answer("⛔ У вас нет прав администратора.")
        return

    await message.answer(
        _admin_panel_text(admin),
        parse_mode="HTML",
        reply_markup=admin_panel_kb(bool(admin.get("on_shift"))),
    )


@router.callback_query(F.data == "adm_back_to_panel")
async def adm_back_to_panel_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        return
    await callback.message.edit_text(
        _admin_panel_text(admin),
        parse_mode="HTML",
        reply_markup=admin_panel_kb(bool(admin.get("on_shift"))),
    )
    await callback.answer()


@router.callback_query(F.data == "adm_tomorrow_table")
async def adm_show_tomorrow_table(callback: CallbackQuery):
    if not await db.is_admin(callback.from_user.id):
        return
    tomorrow_str = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_ru = db.format_ru_date(tomorrow_str)
    apps = await db.get_appointments_by_date(tomorrow_str)
    if not apps:
        await callback.message.answer(f"📅 На завтра ({tomorrow_ru}) записей пока нет.")
        await callback.answer()
        return

    text = (
            f"📅 <b>Все записи салона на завтра ({tomorrow_ru}):</b>\n\n"
            + format_appointments_table(apps)
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


# ==================== ПРОСМОТР ВСЕХ ЗАПИСЕЙ И РЕДАКТИРОВАНИЕ ИХ В БОТЕ (АДМИНИСТРАТОР) ====================
def _build_admin_apps_list_kb(
        apps: list, filter_mode: str, page: int
) -> InlineKeyboardMarkup:
    total = len(apps)
    start = page * ADMIN_APPS_PAGE_SIZE
    end = start + ADMIN_APPS_PAGE_SIZE
    page_apps = apps[start:end]

    rows = [
        [
            InlineKeyboardButton(
                text=("• 📅 Актуальные •" if filter_mode == "upcoming" else "📅 Актуальные"),
                callback_data="adm_apps_list_upcoming_0",
            ),
            InlineKeyboardButton(
                text=("• 📆 Сегодня •" if filter_mode == "today" else "📆 Сегодня"),
                callback_data="adm_apps_list_today_0",
            ),
        ],
        [
            InlineKeyboardButton(
                text=("• 📆 Завтра •" if filter_mode == "tomorrow" else "📆 Завтра"),
                callback_data="adm_apps_list_tomorrow_0",
            ),
            InlineKeyboardButton(
                text=("• 🗂 Все записи •" if filter_mode == "all" else "🗂 Все записи"),
                callback_data="adm_apps_list_all_0",
            ),
        ],
    ]

    for a in page_apps:
        dt_ru = db.format_ru_datetime(a["appointment_datetime"])
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ #{a['id']} • {dt_ru} — {a['client_name']}",
                    callback_data=f"adm_app_view_{a['id']}",
                )
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"adm_apps_list_{filter_mode}_{page - 1}",
            )
        )
    if end < total:
        nav_row.append(
            InlineKeyboardButton(
                text="Вперед ➡️",
                callback_data=f"adm_apps_list_{filter_mode}_{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [InlineKeyboardButton(text="🔙 В панель администратора", callback_data="adm_back_to_panel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_admin_apps_list(
        callback: CallbackQuery, filter_mode: str, page: int
):
    apps = await db.get_all_appointments_for_admin(filter_mode=filter_mode, limit=150)
    titles = {
        "upcoming": "📅 Предстоящие записи",
        "today": f"📆 Записи на сегодня ({db.format_ru_date(datetime.now().strftime('%Y-%m-%d'))})",
        "tomorrow": f"📆 Записи на завтра ({db.format_ru_date((datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'))})",
        "all": "🗂 Все записи салона",
    }
    title = titles.get(filter_mode, "📋 Список записей")

    total = len(apps)
    start = page * ADMIN_APPS_PAGE_SIZE
    end = start + ADMIN_APPS_PAGE_SIZE
    page_apps = apps[start:end]

    if not page_apps:
        text = f"<b>{title}</b>\n\nЗаписей в этой категории не найдено."
    else:
        total_pages = max(1, (total + ADMIN_APPS_PAGE_SIZE - 1) // ADMIN_APPS_PAGE_SIZE)
        text = (
                f"<b>{title}</b> (стр. {page + 1} из {total_pages}, всего: {total})\n"
                f"<i>Нажмите на кнопку с номером записи ниже, чтобы отредактировать её:</i>\n\n"
                + format_appointments_table(page_apps)
        )

    kb = _build_admin_apps_list_kb(apps, filter_mode, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm_apps_list_"))
async def adm_apps_list_cb(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    await state.clear()
    tail = callback.data.split("adm_apps_list_", 1)[1]
    filter_mode, page_str = tail.rsplit("_", 1)
    page = int(page_str) if page_str.isdigit() else 0
    await _render_admin_apps_list(callback, filter_mode, page)
    await callback.answer()


def _admin_single_app_edit_kb(app_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🕒 Изменить дату и время",
                callback_data=f"adm_ed_dt_{app_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="💅 Изменить услугу",
                callback_data=f"adm_ed_srv_{app_id}",
            ),
            InlineKeyboardButton(
                text="👩‍🎨 Изменить мастеров",
                callback_data=f"adm_ed_mst_{app_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👤 Изменить данные клиента",
                callback_data=f"adm_ed_cli_{app_id}",
            ),
            InlineKeyboardButton(
                text="💬 Изменить комментарий",
                callback_data=f"adm_ed_comm_{app_id}",
            ),
        ],
    ]
    status_row = []
    if status != "confirmed":
        status_row.append(
            InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"adm_ed_status_{app_id}_confirmed",
            )
        )
    if status != "cancelled":
        status_row.append(
            InlineKeyboardButton(
                text="❌ Отменить запись",
                callback_data=f"adm_ed_status_{app_id}_cancelled",
            )
        )
    if status_row:
        rows.append(status_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Ко всем записям",
                callback_data="adm_apps_list_upcoming_0",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_admin_app_card(target, app_id: int, edit_message: bool = True):
    details = await db.get_appointment_details(app_id)
    if not details:
        if edit_message:
            await target.edit_text("Запись не найдена.")
        else:
            await target.answer("Запись не найдена.")
        return

    text = (
            f"🛠 <b>Управление записью #{app_id}</b>\n\n"
            + format_single_appointment_block(details)
            + "\n\nВыберите параметр для редактирования:"
    )
    kb = _admin_single_app_edit_kb(app_id, details["status"])
    if edit_message:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm_app_view_"))
async def adm_app_view_cb(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    await state.clear()
    app_id = int(callback.data.split("adm_app_view_", 1)[1])
    await _show_admin_app_card(callback.message, app_id, edit_message=True)
    await callback.answer()


# --- 1) Смена статуса из карточки редактирования ---
@router.callback_query(F.data.startswith("adm_ed_status_"))
async def adm_edit_status_cb(callback: CallbackQuery, bot: Bot):
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        return
    rest = callback.data.split("adm_ed_status_", 1)[1]
    app_id_str, new_status = rest.split("_", 1)
    app_id = int(app_id_str)

    await db.update_appointment_status(app_id, new_status)
    details = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        admin["name"],
        f"Изменение статуса записи (Бот) -> {new_status}",
        f"Запись #{app_id}",
    )

    dt_ru = db.format_ru_datetime(details["appointment_datetime"])
    if new_status == "confirmed":
        await notify_master_confirmed(bot, details)
        if details["client_tg_id"] and details["client_tg_id"] > 0:
            try:
                await bot.send_message(
                    details["client_tg_id"],
                    f"✅ Администратор подтвердил вашу запись #{app_id}!\n"
                    f"💅 Услуга: {details['service_name']}\n"
                    f"👩‍🎨 Мастер: {format_masters_Line(details)}\n"
                    f"🕒 Когда: {dt_ru}",
                )
            except Exception:
                pass
    elif new_status == "cancelled":
        await notify_masters_booking_event(bot, details, "❌ <b>Запись к вам отменена администратором</b>")
        if details["client_tg_id"] and details["client_tg_id"] > 0:
            try:
                await bot.send_message(
                    details["client_tg_id"],
                    f"❌ Ваша запись #{app_id} на {dt_ru} была отменена администратором.",
                )
            except Exception:
                pass

    await _show_admin_app_card(callback.message, app_id, edit_message=True)
    await callback.answer("Статус обновлен!")


# --- 2) Изменение даты и времени администратором ---
@router.callback_query(F.data.startswith("adm_ed_dt_"))
async def adm_edit_dt_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.split("adm_ed_dt_", 1)[1])
    await state.update_data(adm_edit_app_id=app_id)
    await state.set_state(AdminEditBookingState.choosing_date)

    today = datetime.now().date()
    rows, row = [], []
    for i in range(14):
        d = today + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        row.append(
            InlineKeyboardButton(
                text=db.format_ru_date(d_str),
                callback_data=f"adm_ed_datepick_{d_str}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад к записи", callback_data=f"adm_app_view_{app_id}")])

    await callback.message.edit_text(
        f"🕒 <b>Изменение даты записи #{app_id}</b>\n"
        f"Выберите дату кнопкой ниже или отправьте дату текстом в формате <code>ГГГГ-ММ-ДД</code>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _adm_show_time_slots_for_date(target_msg: Message, state: FSMContext, date_str: str, edit: bool):
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    details = await db.get_appointment_details(app_id)
    m_ids = details.get("master_ids") or [details["master_id"]]

    slots = await db.get_available_slots(
        m_ids,
        date_str,
        details["service_duration"],
        exclude_appointment_id=app_id,
    )
    await state.update_data(adm_new_date=date_str)
    await state.set_state(AdminEditBookingState.choosing_time)

    rows, row = [], []
    for s in slots:
        row.append(InlineKeyboardButton(text=s, callback_data=f"adm_ed_timepick_{s}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад к записи", callback_data=f"adm_app_view_{app_id}")])

    text = (
        f"Дата: <b>{db.format_ru_date(date_str)}</b>\n"
        f"Выберите свободное время кнопкой или введите время вручную (например, <code>14:30</code>):"
    )
    if edit:
        await target_msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await target_msg.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(AdminEditBookingState.choosing_date, F.data.startswith("adm_ed_datepick_"))
async def adm_edit_date_picked(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("adm_ed_datepick_", 1)[1]
    await _adm_show_time_slots_for_date(callback.message, state, date_str, edit=True)
    await callback.answer()


@router.message(AdminEditBookingState.choosing_date)
async def adm_edit_date_typed(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await message.answer("Введите корректную дату в формате <code>ГГГГ-ММ-ДД</code> (например, 2026-11-15):", parse_mode="HTML")
        return
    await _adm_show_time_slots_for_date(message, state, date_str, edit=False)


async def _adm_apply_new_datetime(
        user_id: int, app_id: int, new_dt: str, bot: Bot
) -> tuple[bool, str]:
    details = await db.get_appointment_details(app_id)
    m_ids = details.get("master_ids") or [details["master_id"]]
    if not await db.is_slot_free_for_master(
            m_ids, new_dt, details["service_duration"], exclude_appointment_id=app_id
    ):
        return False, "⚠️ Выбранное время занято у одного из назначенных мастеров."

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=details["client_name"],
        client_phone=details["client_phone"],
        master_id=m_ids[0],
        second_master_id=m_ids[1] if len(m_ids) > 1 else None,
        master_ids=m_ids,
        service_id=details["service_id"],
        appointment_datetime=new_dt,
        comment=details.get("comment") or "",
        client_username=details.get("client_username") or "",
    )
    admin = await db.get_admin_by_tg_id(user_id)
    updated = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        admin["name"] if admin else "Админ",
        "Изменение даты/времени записи (Бот)",
        f"Запись #{app_id} -> {new_dt}",
    )
    await notify_masters_booking_event(bot, updated, "✏️ <b>Администратор изменил время записи</b>")
    if updated["client_tg_id"] and updated["client_tg_id"] > 0:
        try:
            await bot.send_message(
                updated["client_tg_id"],
                f"✏️ <b>Администратор перенес вашу запись #{app_id}:</b>\n"
                f"🕒 Новое время: <b>{db.format_ru_datetime(new_dt)}</b>\n"
                f"💅 Услуга: {updated['service_name']}\n"
                f"👩‍🎨 Мастер: {format_masters_Line(updated)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True, "✅ Дата и время обновлены!"


@router.callback_query(AdminEditBookingState.choosing_time, F.data.startswith("adm_ed_timepick_"))
async def adm_edit_time_picked(callback: CallbackQuery, state: FSMContext, bot: Bot):
    time_str = callback.data.split("adm_ed_timepick_", 1)[1]
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    new_dt = f"{data['adm_new_date']} {time_str}"

    ok, msg = await _adm_apply_new_datetime(callback.from_user.id, app_id, new_dt, bot)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return
    await state.clear()
    await _show_admin_app_card(callback.message, app_id, edit_message=True)
    await callback.answer(msg)


@router.message(AdminEditBookingState.choosing_time)
async def adm_edit_time_typed(message: Message, state: FSMContext, bot: Bot):
    time_str = message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("Введите время в формате <code>ЧЧ:ММ</code> (например, 14:30):", parse_mode="HTML")
        return
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    new_dt = f"{data['adm_new_date']} {time_str}"
    ok, msg = await _adm_apply_new_datetime(message.from_user.id, app_id, new_dt, bot)
    if not ok:
        await message.answer(msg)
        return
    await state.clear()
    await _show_admin_app_card(message, app_id, edit_message=False)


# --- 3) Изменение услуги администратором ---
@router.callback_query(F.data.startswith("adm_ed_srv_"))
async def adm_edit_service_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.split("adm_ed_srv_", 1)[1])
    services = await db.get_all_services()
    await state.update_data(adm_edit_app_id=app_id)
    await state.set_state(AdminEditBookingState.choosing_service)

    rows = [
        [
            InlineKeyboardButton(
                text=f"{s['category']}: {s['name']} ({s['price']} ₽)",
                callback_data=f"adm_ed_srvpick_{s['id']}",
            )
        ]
        for s in services
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад к записи", callback_data=f"adm_app_view_{app_id}")])
    await callback.message.edit_text(
        f"💅 <b>Выберите новую услугу для записи #{app_id}:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminEditBookingState.choosing_service, F.data.startswith("adm_ed_srvpick_"))
async def adm_edit_service_picked(callback: CallbackQuery, state: FSMContext, bot: Bot):
    srv_id = int(callback.data.split("adm_ed_srvpick_", 1)[1])
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    details = await db.get_appointment_details(app_id)
    m_ids = details.get("master_ids") or [details["master_id"]]

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=details["client_name"],
        client_phone=details["client_phone"],
        master_id=m_ids[0],
        second_master_id=m_ids[1] if len(m_ids) > 1 else None,
        master_ids=m_ids,
        service_id=srv_id,
        appointment_datetime=details["appointment_datetime"],
        comment=details.get("comment") or "",
        client_username=details.get("client_username") or "",
    )
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    updated = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        admin["name"] if admin else "Админ",
        "Изменение услуги в записи (Бот)",
        f"Запись #{app_id} -> {updated['service_name']}",
    )
    await state.clear()
    await notify_masters_booking_event(bot, updated, "✏️ <b>Администратор изменил услугу в записи</b>")
    await _show_admin_app_card(callback.message, app_id, edit_message=True)
    await callback.answer("Услуга обновлена!")


# --- 4) Изменение мастеров (1 или несколько) администратором в боте ---
def _build_masters_multiselect_kb(
        all_masters: list, selected_ids: list, app_id: int
) -> InlineKeyboardMarkup:
    rows = []
    for m in all_masters:
        checked = m["id"] in selected_ids
        mark = "✅" if checked else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} 👩‍🎨 {m['name']} ({m['specialization']})",
                    callback_data=f"adm_ed_msttoggle_{m['id']}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="☑️ Выбрать всех", callback_data="adm_ed_mstall"),
            InlineKeyboardButton(text="💾 Сохранить мастеров", callback_data="adm_ed_mstsave"),
        ]
    )
    rows.append([InlineKeyboardButton(text="🔙 Назад к записи", callback_data=f"adm_app_view_{app_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("adm_ed_mst_"))
async def adm_edit_masters_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.split("adm_ed_mst_", 1)[1])
    details = await db.get_appointment_details(app_id)
    selected_ids = details.get("master_ids") or [details["master_id"]]
    all_masters = await db.get_all_masters(only_active=True)

    await state.update_data(adm_edit_app_id=app_id, adm_selected_masters=selected_ids)
    await state.set_state(AdminEditBookingState.choosing_masters)

    await callback.message.edit_text(
        f"👩‍🎨 <b>Выбор мастеров для записи #{app_id}</b>\n"
        f"Отметьте одного или нескольких мастеров (можно выбрать хоть всех) и нажмите «💾 Сохранить мастеров»:",
        parse_mode="HTML",
        reply_markup=_build_masters_multiselect_kb(all_masters, selected_ids, app_id),
    )
    await callback.answer()


@router.callback_query(AdminEditBookingState.choosing_masters, F.data.startswith("adm_ed_msttoggle_"))
async def adm_edit_masters_toggle(callback: CallbackQuery, state: FSMContext):
    mid = int(callback.data.split("adm_ed_msttoggle_", 1)[1])
    data = await state.get_data()
    selected = list(data.get("adm_selected_masters") or [])
    if mid in selected:
        selected.remove(mid)
    else:
        selected.append(mid)
    await state.update_data(adm_selected_masters=selected)
    all_masters = await db.get_all_masters(only_active=True)
    await callback.message.edit_reply_markup(
        reply_markup=_build_masters_multiselect_kb(all_masters, selected, data["adm_edit_app_id"])
    )
    await callback.answer()


@router.callback_query(AdminEditBookingState.choosing_masters, F.data == "adm_ed_mstall")
async def adm_edit_masters_select_all(callback: CallbackQuery, state: FSMContext):
    all_masters = await db.get_all_masters(only_active=True)
    selected = [m["id"] for m in all_masters]
    data = await state.get_data()
    await state.update_data(adm_selected_masters=selected)
    await callback.message.edit_reply_markup(
        reply_markup=_build_masters_multiselect_kb(all_masters, selected, data["adm_edit_app_id"])
    )
    await callback.answer("Выбраны все мастера")


@router.callback_query(AdminEditBookingState.choosing_masters, F.data == "adm_ed_mstsave")
async def adm_edit_masters_save(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    selected = list(data.get("adm_selected_masters") or [])
    if not selected:
        await callback.answer("Выберите хотя бы одного мастера!", show_alert=True)
        return

    details = await db.get_appointment_details(app_id)
    if not await db.is_slot_free_for_master(
            selected,
            details["appointment_datetime"],
            details["service_duration"],
            exclude_appointment_id=app_id,
    ):
        await callback.answer(
            "⚠️ На время этой записи один из выбранных мастеров уже занят или в отгуле!",
            show_alert=True,
        )
        return

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=details["client_name"],
        client_phone=details["client_phone"],
        master_id=selected[0],
        second_master_id=selected[1] if len(selected) > 1 else None,
        master_ids=selected,
        service_id=details["service_id"],
        appointment_datetime=details["appointment_datetime"],
        comment=details.get("comment") or "",
        client_username=details.get("client_username") or "",
    )
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    updated = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        admin["name"] if admin else "Админ",
        "Изменение мастеров записи (Бот)",
        f"Запись #{app_id} -> {format_masters_Line(updated)}",
    )
    await state.clear()
    await notify_masters_booking_event(bot, updated, "👩‍🎨 <b>Администратор обновил состав мастеров на записи</b>")
    await _show_admin_app_card(callback.message, app_id, edit_message=True)
    await callback.answer("Мастера сохранены!")


# --- 5) Изменение данных клиента (Имя / Телефон / @username) администратором ---
@router.callback_query(F.data.startswith("adm_ed_cli_"))
async def adm_edit_client_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.split("adm_ed_cli_", 1)[1])
    details = await db.get_appointment_details(app_id)
    await state.update_data(adm_edit_app_id=app_id)
    await state.set_state(AdminEditBookingState.entering_client_name)
    await callback.message.edit_text(
        f"👤 <b>Редактирование клиента в записи #{app_id}</b>\n"
        f"Текущее имя: <b>{details['client_name']}</b>\n\n"
        f"Введите новое имя клиента (или отправьте <code>-</code>, чтобы оставить прежнее):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminEditBookingState.entering_client_name)
async def adm_edit_client_name(message: Message, state: FSMContext):
    data = await state.get_data()
    details = await db.get_appointment_details(data["adm_edit_app_id"])
    raw = message.text.strip()
    new_name = details["client_name"] if raw == "-" else raw
    await state.update_data(adm_new_client_name=new_name)
    await state.set_state(AdminEditBookingState.entering_client_phone)
    await message.answer(
        f"Текущий телефон: <code>{details['client_phone']}</code>\n"
        f"Введите новый телефон (или отправьте <code>-</code>, чтобы оставить прежний):",
        parse_mode="HTML",
    )


@router.message(AdminEditBookingState.entering_client_phone)
async def adm_edit_client_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    details = await db.get_appointment_details(data["adm_edit_app_id"])
    raw = message.text.strip()
    new_phone = details["client_phone"] if raw == "-" else raw
    await state.update_data(adm_new_client_phone=new_phone)
    await state.set_state(AdminEditBookingState.entering_client_username)
    cur_u = f"@{details['client_username']}" if details.get("client_username") else "не указан"
    await message.answer(
        f"Текущий @username: <b>{cur_u}</b>\n"
        f"Введите новый <code>@username</code> клиента (или отправьте <code>-</code>, чтобы оставить прежний):",
        parse_mode="HTML",
    )


@router.message(AdminEditBookingState.entering_client_username)
async def adm_edit_client_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    details = await db.get_appointment_details(app_id)
    raw = message.text.strip()
    new_uname = (details.get("client_username") or "") if raw == "-" else raw.lstrip("@")
    m_ids = details.get("master_ids") or [details["master_id"]]

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=data["adm_new_client_name"],
        client_phone=data["adm_new_client_phone"],
        master_id=m_ids[0],
        second_master_id=m_ids[1] if len(m_ids) > 1 else None,
        master_ids=m_ids,
        service_id=details["service_id"],
        appointment_datetime=details["appointment_datetime"],
        comment=details.get("comment") or "",
        client_username=new_uname,
    )
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    await db.log_admin_action(
        admin["name"] if admin else "Админ",
        "Изменение данных клиента в записи (Бот)",
        f"Запись #{app_id}: {data['adm_new_client_name']} (@{new_uname}, {data['adm_new_client_phone']})",
    )
    await state.clear()
    await _show_admin_app_card(message, app_id, edit_message=False)


# --- 6) Изменение комментария администратором ---
@router.callback_query(F.data.startswith("adm_ed_comm_"))
async def adm_edit_comment_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.split("adm_ed_comm_", 1)[1])
    await state.update_data(adm_edit_app_id=app_id)
    await state.set_state(AdminEditBookingState.entering_comment)
    await callback.message.edit_text(
        f"💬 Введите новый комментарий для записи #{app_id} (или отправьте <code>-</code>, чтобы очистить комментарий):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminEditBookingState.entering_comment)
async def adm_edit_comment_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    app_id = data["adm_edit_app_id"]
    details = await db.get_appointment_details(app_id)
    raw = message.text.strip()
    new_comm = "" if raw == "-" else raw
    m_ids = details.get("master_ids") or [details["master_id"]]

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=details["client_name"],
        client_phone=details["client_phone"],
        master_id=m_ids[0],
        second_master_id=m_ids[1] if len(m_ids) > 1 else None,
        master_ids=m_ids,
        service_id=details["service_id"],
        appointment_datetime=details["appointment_datetime"],
        comment=new_comm,
        client_username=details.get("client_username") or "",
    )
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    await db.log_admin_action(
        admin["name"] if admin else "Админ",
        "Изменение комментария записи (Бот)",
        f"Запись #{app_id}: {new_comm}",
    )
    await state.clear()
    await _show_admin_app_card(message, app_id, edit_message=False)


# ==================== СВЯЗЬ С КЛИЕНТОМ В ЛЮБОЕ ВРЕМЯ И БОНУСЫ (АДМИН) ====================
@router.callback_query(F.data == "adm_contact_client")
async def adm_contact_client_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    await state.set_state(AdminContactClientState.entering_client_query)
    await callback.message.answer(
        "✉️ <b>Связь с клиентом</b>\n"
        "Введите <code>@username</code>, номер телефона или Telegram ID клиента:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminContactClientState.entering_client_query)
async def adm_contact_client_resolve(message: Message, state: FSMContext):
    user = await db.find_user_by_identifier(message.text.strip())
    if not user or not user.get("tg_id") or int(user["tg_id"]) <= 0:
        await state.clear()
        await message.answer(
            "⚠️ Клиент с активным Telegram ID не найден (возможно, он еще ни разу не запускал бота)."
        )
        return

    await state.update_data(
        target_tg_id=int(user["tg_id"]),
        target_username=user.get("username") or "",
        target_name=user.get("full_name") or user.get("username") or "Клиент",
    )
    await state.set_state(AdminContactClientState.entering_first_message)
    await message.answer(
        f"✅ Клиент найден: <b>{user.get('full_name') or 'Клиент'}</b> "
        f"(@{user.get('username') or '—'}, {user.get('phone') or '—'}).\n\n"
        f"Напишите первое сообщение для клиента (после этого откроется активный диалог):",
        parse_mode="HTML",
    )


@router.message(AdminContactClientState.entering_first_message)
async def adm_contact_client_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    admin_name = admin["name"] if admin else "Администратор"
    text = message.text.strip()

    ticket = await db.open_or_activate_admin_dialog(
        client_tg_id=data["target_tg_id"],
        client_username=data["target_username"],
        client_name=data["target_name"],
        admin_tg_id=message.from_user.id,
        admin_name=admin_name,
    )
    await db.add_ticket_message(ticket["id"], "admin", admin_name, text)
    await db.log_admin_action(
        admin_name,
        "Инициация диалога с клиентом (Бот)",
        f"Тикет #{ticket['id']} -> {data['target_name']} (@{data['target_username']})",
    )
    await state.clear()

    close_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Завершить диалог",
                    callback_data=f"close_ticket_{ticket['id']}",
                )
            ]
        ]
    )
    try:
        await bot.send_message(
            data["target_tg_id"],
            f"👩‍💼 <b>Администратор {admin_name} связался с вами:</b>\n\n{text}\n\n"
            f"<i>Вы можете ответить прямо в этом чате.</i>",
            parse_mode="HTML",
            reply_markup=close_kb,
        )
        await message.answer(
            f"✅ Сообщение отправлено клиенту <b>{data['target_name']}</b> (Тикет #{ticket['id']}).\n"
            f"Все ваши следующие сообщения в боте будут пересылаться этому клиенту до закрытия диалога.",
            parse_mode="HTML",
            reply_markup=close_kb,
        )
    except Exception as e:
        await message.answer(f"⚠️ Не удалось доставить сообщение клиенту в Telegram: {e}")


@router.callback_query(F.data == "adm_adjust_bonus")
async def adm_adjust_bonus_start(callback: CallbackQuery, state: FSMContext):
    if not await db.is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAdjustBonusState.entering_client_query)
    await callback.message.answer(
        "🎁 <b>Управление бонусами клиента</b>\n"
        "Введите <code>@username</code>, номер телефона или Telegram ID клиента:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminAdjustBonusState.entering_client_query)
async def adm_adjust_bonus_resolve(message: Message, state: FSMContext):
    user = await db.find_user_by_identifier(message.text.strip())
    if not user:
        await state.clear()
        await message.answer("⚠️ Клиент не найден в базе.")
        return

    cur_pts = await db.get_client_bonus_balance(
        client_tg_id=user.get("tg_id") or 0,
        client_phone=user.get("phone") or "",
        client_username=user.get("username") or "",
    )
    await state.update_data(
        b_tg_id=user.get("tg_id") or 0,
        b_phone=user.get("phone") or "",
        b_username=user.get("username") or "",
        b_name=user.get("full_name") or user.get("username") or "Клиент",
    )
    await state.set_state(AdminAdjustBonusState.entering_delta)
    await message.answer(
        f"👤 Клиент: <b>{user.get('full_name') or 'Клиент'}</b> (@{user.get('username') or '—'})\n"
        f"🎁 Текущий баланс: <b>{cur_pts} баллов</b>\n\n"
        f"Введите количество баллов для добавления (например, <code>+250</code> или <code>250</code>) "
        f"или для списания (например, <code>-150</code>):",
        parse_mode="HTML",
    )


@router.message(AdminAdjustBonusState.entering_delta)
async def adm_adjust_bonus_finish(message: Message, state: FSMContext, bot: Bot):
    raw = message.text.strip().replace(" ", "")
    if not raw.lstrip("+-").isdigit():
        await message.answer("Пожалуйста, введите целое число (например, +200 или -100):")
        return

    delta = int(raw)
    data = await state.get_data()
    admin = await db.get_admin_by_tg_id(message.from_user.id)
    admin_name = admin["name"] if admin else "Администратор"

    new_balance = await db.adjust_client_bonus_balance(
        client_tg_id=data["b_tg_id"],
        client_phone=data["b_phone"],
        client_name=data["b_name"],
        delta=delta,
        client_username=data["b_username"],
    )
    await db.log_admin_action(
        admin_name,
        "Ручное изменение бонусов (Бот)",
        f"{data['b_name']} (@{data['b_username']}): {'+' if delta >= 0 else ''}{delta} -> Итого: {new_balance}",
    )
    await state.clear()

    if data["b_tg_id"] and int(data["b_tg_id"]) > 0:
        action_word = f"начислено +{delta}" if delta >= 0 else f"списано {abs(delta)}"
        try:
            await bot.send_message(
                int(data["b_tg_id"]),
                f"🎁 <b>Обновление бонусного счета:</b> вам {action_word} бонусных баллов.\n"
                f"✨ <b>Актуальный баланс:</b> {new_balance} баллов.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await message.answer(
        f"✅ Бонусный баланс клиента <b>{data['b_name']}</b> обновлен!\n"
        f"🎁 <b>Новый баланс:</b> {new_balance} баллов.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_confirm_"))
async def admin_confirm_booking(callback: CallbackQuery, bot: Bot):
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        return

    app_id = int(callback.data.split("adm_confirm_", 1)[1])
    details = await db.get_appointment_details(app_id)
    if not details:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    await db.update_appointment_status(app_id, "confirmed")
    await db.log_admin_action(admin["name"], "Подтверждение записи (Бот)", f"Запись #{app_id}")
    await notify_master_confirmed(bot, details)

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>Статус: ПОДТВЕРЖДЕНО АДМИНИСТРАТОРОМ</b>",
        parse_mode="HTML",
        )
    if details["client_tg_id"] and details["client_tg_id"] > 0:
        try:
            await bot.send_message(
                details["client_tg_id"],
                f"✅ Администратор подтвердил вашу запись #{app_id}!\n"
                f"💅 Услуга: {details['service_name']}\n"
                f"👩‍🎨 Мастер: {format_masters_Line(details)}\n"
                f"🕒 Дата и время: {db.format_ru_datetime(details['appointment_datetime'])}",
            )
        except Exception:
            pass
    await callback.answer("Подтверждено!")


@router.callback_query(F.data.startswith("adm_cancel_"))
async def admin_cancel_booking(callback: CallbackQuery, bot: Bot):
    admin = await db.get_admin_by_tg_id(callback.from_user.id)
    if not admin:
        return

    app_id = int(callback.data.split("adm_cancel_", 1)[1])
    details = await db.get_appointment_details(app_id)
    if not details:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    await db.update_appointment_status(app_id, "cancelled")
    await db.log_admin_action(admin["name"], "Отмена записи (Бот)", f"Запись #{app_id}")
    await notify_masters_booking_event(bot, details, "❌ <b>Запись к вам отменена администратором</b>")

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>Статус: ОТМЕНЕНО АДМИНИСТРАТОРОМ</b>",
        parse_mode="HTML",
        )
    if details["client_tg_id"] and details["client_tg_id"] > 0:
        try:
            await bot.send_message(
                details["client_tg_id"],
                f"❌ Ваша запись #{app_id} на {db.format_ru_datetime(details['appointment_datetime'])} была отменена администратором.",
            )
        except Exception:
            pass
    await callback.answer("Отменено.")


# ==================== ПЕРЕСЫЛКА СООБЩЕНИЙ АКТИВНОГО ТИКЕТА ====================
@router.message(F.text)
async def handle_ticket_chat_messages(message: Message, bot: Bot):
    if await db.is_admin(message.from_user.id):
        adm_ticket = await db.get_admin_active_ticket(message.from_user.id)
        if adm_ticket:
            admin = await db.get_admin_by_tg_id(message.from_user.id)
            admin_name = admin["name"] if admin else "Администратор"
            await db.add_ticket_message(
                adm_ticket["id"], "admin", admin_name, message.text
            )
            close_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔒 Завершить диалог",
                            callback_data=f"close_ticket_{adm_ticket['id']}",
                        )
                    ]
                ]
            )
            try:
                await bot.send_message(
                    adm_ticket["client_tg_id"],
                    f"👩‍💼 <b>{admin_name}:</b>\n{message.text}",
                    parse_mode="HTML",
                    reply_markup=close_kb,
                )
            except Exception:
                pass
            return

    client_ticket = await db.get_client_open_ticket(message.from_user.id)
    if client_ticket:
        sender_name = message.from_user.full_name or message.from_user.username or "Клиент"
        await db.add_ticket_message(
            client_ticket["id"], "client", sender_name, message.text
        )
        close_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Закрыть тикет",
                        callback_data=f"close_ticket_{client_ticket['id']}",
                    )
                ]
            ]
        )
        if client_ticket.get("admin_tg_id"):
            try:
                await bot.send_message(
                    client_ticket["admin_tg_id"],
                    f"💬 <b>Клиент ({sender_name}, тикет #{client_ticket['id']}):</b>\n{message.text}",
                    parse_mode="HTML",
                    reply_markup=close_kb,
                )
            except Exception:
                pass


# ==================== ФОНОВЫЕ ЗАДАЧИ И ОПОВЕЩЕНИЯ ВКЛЮЧЕНИЯ / ОТКЛЮЧЕНИЯ БОТА ====================
async def broadcast_system_status(bot: Bot, text: str):
    if not bot:
        return
    recipients = await db.get_all_notify_tg_ids()
    for tg_id in recipients:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            pass


async def notify_bot_started(bot: Bot):
    await broadcast_system_status(
        bot,
        "🟢 <b>Бот салона красоты запущен и работает в штатном режиме!</b>\n"
        "Вы можете записываться на услуги и управлять своими записями онлайн.",
    )


async def notify_bot_stopped(bot: Bot):
    global _shutdown_notified
    if _shutdown_notified or not bot:
        return
    _shutdown_notified = True
    await broadcast_system_status(
        bot,
        f"⚠️ <b>Внимание! Бот салона красоты временно отключен по техническим причинам.</b>\n\n"
        f"📞 Для записи, переноса визита или по любым вопросам свяжитесь с нами по телефону салона:\n"
        f"<b><code>{SALON_PHONE}</code></b>",
    )


async def check_and_send_24h_reminders(bot: Bot):
    if not bot:
        return
    appointments = await db.get_appointments_for_24h_reminder()
    on_shift_admins = await db.get_admins(only_on_shift=True)

    for a in appointments:
        dt_ru = db.format_ru_datetime(a["appointment_datetime"])
        if a["client_tg_id"] and a["client_tg_id"] > 0:
            client_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Подтверждаю визит",
                            callback_data=f"client_confirm_{a['id']}",
                        ),
                        InlineKeyboardButton(
                            text="❌ Отменить запись",
                            callback_data=f"client_cancel_{a['id']}",
                        ),
                    ]
                ]
            )
            try:
                await bot.send_message(
                    a["client_tg_id"],
                    f"🔔 <b>Напоминание о записи в салон красоты!</b>\n\n"
                    f"Завтра вас ждет мастер: <b>{format_masters_Line(a)}</b>.\n"
                    f"💅 <b>Услуга:</b> {a['service_name']} ({a['service_price']} ₽)\n"
                    f"🕒 <b>Дата и время:</b> {dt_ru}",
                    parse_mode="HTML",
                    reply_markup=client_kb,
                )
            except Exception:
                pass

        comment_line = f"\n💬 <b>Комментарий:</b> {a['comment']}" if a.get("comment") else ""
        admin_text = (
            f"📞 <b>НАПОМИНАНИЕ ЗА СУТКИ — СОВЕРШИТЬ ЗВОНОК КЛИЕНТУ</b>\n\n"
            f"📌 <b>Запись #{a['id']}</b>\n"
            f"👤 <b>Клиент:</b> {a['client_name']}\n"
            f"☎️ <b>Телефон:</b> {a['client_phone']}\n"
            f"💅 <b>Услуга:</b> {a['service_name']}\n"
            f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(a)}\n"
            f"🕒 <b>Время визита:</b> {dt_ru}{comment_line}"
        )
        for adm in on_shift_admins:
            try:
                await bot.send_message(
                    adm["tg_id"],
                    admin_text,
                    parse_mode="HTML",
                    reply_markup=admin_booking_actions_kb(a["id"]),
                )
            except Exception:
                pass

        await db.mark_appointment_reminded(a["id"])


async def send_daily_schedule_to_masters(bot: Bot, is_evening: bool = False):
    if not bot:
        return
    target_date = datetime.now().date()
    if is_evening:
        target_date += timedelta(days=1)
    date_str = target_date.strftime("%Y-%m-%d")
    date_ru = db.format_ru_date(date_str)
    period_title = "на завтра" if is_evening else "на сегодня"

    masters = await db.get_all_masters(only_active=True)
    for m in masters:
        if not m.get("tg_id"):
            continue
        apps = await db.get_master_appointments(m["id"], date_str=date_str, only_confirmed=False)
        if not apps:
            text = (
                f"📆 Добрый {'вечер' if is_evening else 'день'}, <b>{m['name']}</b>!\n"
                f"Ваше расписание {period_title} ({date_ru}): записей пока нет."
            )
        else:
            text = (
                    f"📆 <b>Ваши заявки {period_title} ({date_ru}) — {m['name']}:</b>\n\n"
                    + format_appointments_table(apps)
            )
        try:
            await bot.send_message(m["tg_id"], text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось отправить расписание мастеру {m['name']}: {e}")


# ==================== ЗАПУСК ====================
async def main():
    await db.init_db(initial_admin_ids=INITIAL_ADMIN_IDS)

    web_port = int(os.getenv("ADMIN_WEB_PORT", "8080"))
    bot = None

    if BOT_TOKEN and BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN" and ":" in BOT_TOKEN:
        bot = Bot(token=BOT_TOKEN)
    else:
        logger.warning("⚠️ BOT_TOKEN не задан. Запущена только веб-панель CRM.")

    await start_web_admin(
        bot=bot,
        reminder_func=check_and_send_24h_reminders,
        host="127.0.0.1",
        port=web_port,
    )
    webbrowser.open(f"http://127.0.0.1:{web_port}")

    if bot:
        dp = Dispatcher()
        dp.include_router(router)
        dp.startup.register(notify_bot_started)
        dp.shutdown.register(notify_bot_stopped)

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            check_and_send_24h_reminders,
            trigger="interval",
            minutes=10,
            kwargs={"bot": bot},
        )
        scheduler.add_job(
            send_daily_schedule_to_masters,
            trigger="cron",
            hour=8,
            minute=0,
            kwargs={"bot": bot, "is_evening": False},
        )
        scheduler.add_job(
            send_daily_schedule_to_masters,
            trigger="cron",
            hour=20,
            minute=0,
            kwargs={"bot": bot, "is_evening": True},
        )
        scheduler.start()

        logger.info("🤖 Бот салона красоты и веб-панель CRM успешно запущены!")
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Аварийная остановка бота: {e}")
        finally:
            await notify_bot_stopped(bot)
            await bot.session.close()
    else:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())