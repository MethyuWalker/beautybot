import os
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote
import aiosqlite
from typing import Optional, List
from aiohttp import web
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

import database as db

logger = logging.getLogger(__name__)

ADMIN_PASSWORD = os.getenv("ADMIN_WEB_PASSWORD", "admin123")
YANDEX_MAPS_URL = os.getenv("YANDEX_MAPS_URL", "https://yandex.ru/maps")
TWOGIS_URL = os.getenv("TWOGIS_URL", "https://2gis.ru")


def check_auth(request: web.Request) -> bool:
    if not ADMIN_PASSWORD:
        return True
    token = request.headers.get("X-Admin-Password", "")
    return token == ADMIN_PASSWORD


def get_actor_name(request: web.Request) -> str:
    raw = request.headers.get("X-Admin-Name", "Web-Администратор")
    return unquote(raw)


def format_masters_Line(details: dict) -> str:
    names = details.get("masters_names")
    if names and isinstance(names, list):
        if len(names) > 1:
            return " + ".join(names) + f" (совместно: {len(names)} маст.)"
        return names[0] if names else details.get("master_name", "—")
    if details.get("second_master_name"):
        return f"{details['master_name']} + {details['second_master_name']} (совместно)"
    return details.get("master_name", "—")


async def notify_masters_booking_event(bot: Optional[Bot], details: dict, title: str):
    """Отправляет уведомление всем мастерам, назначенным на запись."""
    if not bot:
        return
    master_tg_ids = []
    for tid in details.get("master_tg_ids") or []:
        if tid and tid not in master_tg_ids:
            master_tg_ids.append(tid)
    if details.get("master_tg_id") and details["master_tg_id"] not in master_tg_ids:
        master_tg_ids.append(details["master_tg_id"])
    if details.get("second_master_tg_id") and details["second_master_tg_id"] not in master_tg_ids:
        master_tg_ids.append(details["second_master_tg_id"])

    comment_line = f"\n💬 <b>Комментарий:</b> {details['comment']}" if details.get("comment") else ""
    m_names = details.get("masters_names") or [details.get("master_name", "")]
    joint_line = (
        f"\n🤝 <b>Совместная работа ({len(m_names)} маст.):</b> {' + '.join(m_names)}"
        if len(m_names) > 1
        else ""
    )
    uname_part = f" (@{details['client_username']})" if details.get("client_username") else ""

    for m_tg_id in master_tg_ids:
        try:
            await bot.send_message(
                m_tg_id,
                f"{title}\n\n"
                f"📌 <b>Запись #{details['id']}</b>\n"
                f"🕒 <b>Дата и время:</b> {details['appointment_datetime']}\n"
                f"💅 <b>Услуга:</b> {details['service_name']} ({details['service_duration']} мин)\n"
                f"👤 <b>Клиент:</b> {details['client_name']}{uname_part} ({details['client_phone']})"
                f"{joint_line}{comment_line}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить мастера {m_tg_id}: {e}")


async def notify_master_confirmed(bot: Optional[Bot], details: dict):
    await notify_masters_booking_event(bot, details, "✅ <b>Подтверждена запись к вам!</b>")


def _parse_master_ids_from_payload(data: dict) -> List[int]:
    raw_list = data.get("master_ids")
    result: List[int] = []
    if isinstance(raw_list, list):
        for x in raw_list:
            if str(x).strip().isdigit() and int(x) > 0 and int(x) not in result:
                result.append(int(x))
    elif isinstance(raw_list, str) and raw_list.strip():
        for x in raw_list.split(","):
            if x.strip().isdigit() and int(x.strip()) > 0 and int(x.strip()) not in result:
                result.append(int(x.strip()))

    if not result and str(data.get("master_id", "")).strip().isdigit():
        result.append(int(data["master_id"]))
    if str(data.get("second_master_id", "")).strip().isdigit():
        sm = int(data["second_master_id"])
        if sm > 0 and sm not in result:
            result.append(sm)
    return result


# ==================== ЗАПРОСЫ К БД ====================
async def get_all_appointments_filtered(
        date_str: str = "", master_id: str = "", status: str = ""
):
    query = """
        SELECT DISTINCT a.*,
               m1.name AS master_name,
               m1.tg_id AS master_tg_id,
               s.name AS service_name,
               s.category AS service_category,
               s.price AS service_price,
               s.duration_minutes AS service_duration
        FROM appointments a
        JOIN masters m1 ON a.master_id = m1.id
        JOIN services s ON a.service_id = s.id
        LEFT JOIN appointment_masters am ON a.id = am.appointment_id
        WHERE 1=1
    """
    params = []
    if date_str:
        query += " AND a.appointment_datetime LIKE ?"
        params.append(f"{date_str}%")
    if master_id and str(master_id).isdigit():
        mid = int(master_id)
        query += " AND (am.master_id = ? OR a.master_id = ? OR a.second_master_id = ?)"
        params.extend([mid, mid, mid])
    if status:
        query += " AND a.status = ?"
        params.append(status)

    query += " ORDER BY a.appointment_datetime ASC"

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        await db._enrich_appointments_with_masters(conn, rows)

    for r in rows:
        r["client_bonus_balance"] = await db.get_client_bonus_balance(
            r.get("client_tg_id") or 0,
            r.get("client_phone") or "",
            r.get("client_username") or "",
            )
    return rows


# ==================== REST API ЗАПИСЕЙ И КАССЫ ====================
async def api_get_appointments(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    date_str = request.query.get("date", "")
    master_id = request.query.get("master_id", "")
    status = request.query.get("status", "")
    apps = await get_all_appointments_filtered(date_str, master_id, status)
    return web.json_response(apps)


async def api_create_appointment(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    master_ids = _parse_master_ids_from_payload(data)
    if not master_ids:
        return web.json_response({"error": "Выберите хотя бы одного мастера!"}, status=400)

    service_id = int(data["service_id"])
    client_name = data["client_name"].strip()
    client_phone = data["client_phone"].strip()
    client_username = (data.get("client_username") or "").strip().lstrip("@")
    comment = data.get("comment", "").strip()
    appointment_dt = f"{data['date']} {data['time']}"

    service = await db.get_service_by_id(service_id)
    duration = service["duration_minutes"] if service else 60

    free = await db.is_slot_free_for_master(master_ids, appointment_dt, duration)
    if not free:
        return web.json_response(
            {"error": "Выбранное время пересекается с другой записью или отгулом одного из выбранных мастеров!"},
            status=400,
        )

    app_id = await db.create_appointment(
        client_tg_id=int(data.get("client_tg_id") or 0),
        client_username=client_username,
        client_name=client_name,
        client_phone=client_phone,
        master_id=master_ids[0],
        second_master_id=master_ids[1] if len(master_ids) > 1 else None,
        master_ids=master_ids,
        service_id=service_id,
        appointment_datetime=appointment_dt,
        comment=comment,
    )
    details = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        get_actor_name(request),
        "Создание записи",
        f"Запись #{app_id}: {client_name} (@{client_username or '—'}, {client_phone}) | "
        f"Мастера: {format_masters_Line(details)} | {appointment_dt}",
    )
    bot: Optional[Bot] = request.app.get("bot")
    await notify_masters_booking_event(bot, details, "🆕 <b>Администратор назначил вас на запись</b>")
    return web.json_response({"ok": True, "id": app_id, "appointment": details})


async def api_update_appointment(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    app_id = int(request.match_info["id"])
    body = await request.json()
    old_details = await db.get_appointment_details(app_id)
    if not old_details:
        return web.json_response({"error": "Запись не найдена"}, status=404)

    actor = get_actor_name(request)
    bot: Optional[Bot] = request.app.get("bot")

    new_client_name = body.get("client_name", old_details["client_name"]).strip()
    new_client_phone = body.get("client_phone", old_details["client_phone"]).strip()
    new_client_username = (
        body.get("client_username", old_details.get("client_username") or "").strip().lstrip("@")
    )

    parsed_masters = _parse_master_ids_from_payload(body)
    new_master_ids = parsed_masters if parsed_masters else (old_details.get("master_ids") or [old_details["master_id"]])

    new_service_id = int(body.get("service_id", old_details["service_id"]))
    new_dt = body.get("appointment_datetime", old_details["appointment_datetime"]).strip()
    new_comment = body.get("comment", old_details.get("comment") or "").strip()
    new_status = body.get("status", old_details["status"])

    service = await db.get_service_by_id(new_service_id)
    duration = service["duration_minutes"] if service else 60

    if new_status != "cancelled":
        free = await db.is_slot_free_for_master(
            new_master_ids,
            new_dt,
            duration,
            exclude_appointment_id=app_id,
        )
        if not free:
            return web.json_response(
                {"error": "Новое время занято хотя бы у одного из выбранных мастеров!"},
                status=400,
            )

    await db.update_appointment_full(
        appointment_id=app_id,
        client_name=new_client_name,
        client_phone=new_client_phone,
        client_username=new_client_username,
        master_id=new_master_ids[0],
        second_master_id=new_master_ids[1] if len(new_master_ids) > 1 else None,
        master_ids=new_master_ids,
        service_id=new_service_id,
        appointment_datetime=new_dt,
        comment=new_comment,
        status=new_status,
    )

    updated = await db.get_appointment_details(app_id)
    await db.log_admin_action(
        actor,
        "Редактирование записи",
        f"Запись #{app_id}: {updated['client_name']} (@{updated.get('client_username') or '—'}) | "
        f"{updated['appointment_datetime']} | Мастера: {format_masters_Line(updated)} | Статус: {updated['status']}",
    )

    comment_text = f"\n💬 <b>Комментарий:</b> {updated['comment']}" if updated.get("comment") else ""
    if old_details["status"] != new_status and new_status == "confirmed":
        await notify_master_confirmed(bot, updated)
        if bot and updated["client_tg_id"] and updated["client_tg_id"] > 0:
            try:
                await bot.send_message(
                    updated["client_tg_id"],
                    f"✅ <b>Администратор подтвердил вашу запись #{app_id}!</b>\n"
                    f"💅 <b>Услуга:</b> {updated['service_name']} ({updated['service_price']} ₽)\n"
                    f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(updated)}\n"
                    f"🕒 <b>Дата и время:</b> {updated['appointment_datetime']}{comment_text}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    elif old_details["status"] != new_status and new_status == "cancelled":
        await notify_masters_booking_event(bot, updated, "❌ <b>Запись к вам отменена администратором</b>")
        if bot and updated["client_tg_id"] and updated["client_tg_id"] > 0:
            try:
                await bot.send_message(
                    updated["client_tg_id"],
                    f"❌ Ваша запись #{app_id} на {updated['appointment_datetime']} "
                    f"({updated['service_name']}) была отменена администратором.",
                )
            except Exception:
                pass
    else:
        await notify_masters_booking_event(bot, updated, "✏️ <b>Администратор обновил параметры записи</b>")
        if bot and updated["client_tg_id"] and updated["client_tg_id"] > 0:
            try:
                await bot.send_message(
                    updated["client_tg_id"],
                    f"✏️ <b>Администратор изменил детали вашей записи #{app_id}:</b>\n\n"
                    f"👤 <b>Имя:</b> {updated['client_name']}\n"
                    f"💅 <b>Услуга:</b> {updated['service_name']} ({updated['service_price']} ₽)\n"
                    f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(updated)}\n"
                    f"🕒 <b>Дата и время:</b> {updated['appointment_datetime']}"
                    f"{comment_text}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    return web.json_response({"ok": True, "appointment": updated})


async def api_pay_appointment(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    app_id = int(request.match_info["id"])
    body = await request.json()
    payment_method = body.get("payment_method", "cash")
    points_to_spend = int(body.get("points_to_spend") or 0)
    discount_amount = int(body.get("discount_amount") or 0)

    updated = await db.process_appointment_payment(
        appointment_id=app_id,
        payment_method=payment_method,
        points_to_spend=points_to_spend,
        discount_amount=discount_amount,
    )
    if not updated:
        return web.json_response({"error": "Запись не найдена"}, status=404)

    pay_label = "Наличный (+5% бонусов)" if payment_method == "cash" else "Безналичный"
    await db.log_admin_action(
        get_actor_name(request),
        "Расчёт клиента (Касса)",
        f"Запись #{app_id} ({updated['client_name']}): {pay_label} | "
        f"Прайс: {updated['full_price']} ₽ | Списано баллов: {updated['points_spent']} | "
        f"Оплачено: {updated['final_paid']} ₽ | Мастерам (40%): {updated['master_share']} ₽ | "
        f"Салону: {updated['salon_share']} ₽",
    )

    bot: Optional[Bot] = request.app.get("bot")
    if bot and updated.get("client_tg_id") and updated["client_tg_id"] > 0:
        discount_parts = []
        if updated.get("points_spent"):
            discount_parts.append(f"списано {updated['points_spent']} баллов")
        if updated.get("discount_amount"):
            discount_parts.append(f"скидка {updated['discount_amount']} ₽")
        discount_info = f" (с учетом: {', '.join(discount_parts)})" if discount_parts else ""
        earned_line = (
            f"\n✨ <b>Начислено за визит (5%):</b> +{updated['points_earned']} баллов"
            if updated.get("points_earned")
            else ""
        )

        review_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⭐️ Оставить отзыв в 2ГИС", url=TWOGIS_URL),
                    InlineKeyboardButton(text="📍 Отзыв на Яндекс Картах", url=YANDEX_MAPS_URL),
                ]
            ]
        )
        thank_you_msg = (
            f"💖 <b>Спасибо, что выбрали наш салон красоты!</b>\n\n"
            f"👩‍🎨 <b>Мастер:</b> {format_masters_Line(updated)}\n"
            f"💅 <b>Услуга:</b> {updated['service_name']}\n"
            f"💳 <b>Итоговая стоимость:</b> {updated['final_paid']} ₽{discount_info}"
            f"{earned_line}\n"
            f"🎁 <b>Актуальный баланс бонусов:</b> {updated['client_bonus_balance']} баллов\n\n"
            f"Мы будем очень признательны, если вы поделитесь впечатлениями и оставите отзыв "
            f"об услуге на <b>2ГИС</b> или <b>Яндекс Картах</b> по кнопкам ниже! 🙏✨"
        )
        try:
            await bot.send_message(
                updated["client_tg_id"],
                thank_you_msg,
                parse_mode="HTML",
                reply_markup=review_kb,
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить чек клиенту {updated['client_tg_id']}: {e}")

    return web.json_response({"ok": True, "appointment": updated})


async def api_get_slots(request: web.Request):
    master_ids_raw = request.query.get("master_ids", "")
    master_ids: List[int] = []
    if master_ids_raw:
        for x in master_ids_raw.split(","):
            if x.strip().isdigit() and int(x.strip()) > 0:
                master_ids.append(int(x.strip()))
    if not master_ids and request.query.get("master_id", "").isdigit():
        master_ids.append(int(request.query["master_id"]))

    exclude_raw = request.query.get("exclude_id", "")
    exclude_id = int(exclude_raw) if exclude_raw.isdigit() else None

    date_str = request.query["date"]
    service_id = int(request.query["service_id"])
    service = await db.get_service_by_id(service_id)
    slots = await db.get_available_slots(
        master_ids,
        date_str,
        service["duration_minutes"] if service else 60,
        exclude_appointment_id=exclude_id,
    )
    return web.json_response(slots)


# ==================== REST API: КЛИЕНТЫ, БОНУСЫ ВРУЧНУЮ, СВЯЗЬ В ЛЮБОЕ ВРЕМЯ, ЧС, ОТЧЕТ ====================
async def api_get_clients(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    sort_by = request.query.get("sort_by", "last_visit")
    clients = await db.get_clients_summary(sort_by=sort_by)
    return web.json_response(clients)


async def api_adjust_client_bonus(request: web.Request):
    """Ручное начисление или списание бонусных баллов администратором."""
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    delta = int(data.get("delta") or 0)
    if delta == 0:
        return web.json_response({"error": "Укажите ненулевое количество баллов (+ или -)"}, status=400)

    tg_id = int(data.get("tg_id") or 0)
    phone = (data.get("client_phone") or "").strip()
    name = (data.get("client_name") or "Клиент").strip()
    username = (data.get("username") or "").strip().lstrip("@")
    reason = (data.get("reason") or "").strip()

    new_balance = await db.adjust_client_bonus_balance(
        client_tg_id=tg_id,
        client_phone=phone,
        client_name=name,
        delta=delta,
        client_username=username,
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Ручная корректировка бонусов",
        f"{name} (@{username or '—'}, {phone or '—'}): {'+' if delta > 0 else ''}{delta} балл. "
        f"-> Новый баланс: {new_balance} ({reason or 'без комментария'})",
    )

    bot: Optional[Bot] = request.app.get("bot")
    if bot and tg_id > 0:
        action_str = f"начислено <b>+{delta}</b>" if delta > 0 else f"списано <b>{abs(delta)}</b>"
        reason_line = f"\n💬 Комментарий: {reason}" if reason else ""
        try:
            await bot.send_message(
                tg_id,
                f"🎁 <b>Обновление бонусного счета:</b> администратором {action_str} бонусных баллов.{reason_line}\n"
                f"✨ <b>Ваш актуальный баланс:</b> {new_balance} баллов.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return web.json_response({"ok": True, "new_balance": new_balance})


async def api_contact_client_anytime(request: web.Request):
    """Позволяет администратору в любое время написать клиенту через бота и открыть активный диалог."""
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    identifier = (data.get("identifier") or "").strip()
    text = (data.get("text") or "").strip()
    admin_name = get_actor_name(request)

    if not identifier or not text:
        return web.json_response({"error": "Укажите клиента и текст сообщения"}, status=400)

    user = await db.find_user_by_identifier(identifier)
    if not user or not user.get("tg_id") or int(user["tg_id"]) <= 0:
        return web.json_response(
            {"error": "У этого клиента нет активного Telegram ID (он еще не запускал бота)."},
            status=400,
        )

    client_tg_id = int(user["tg_id"])
    client_username = user.get("username") or ""
    client_name = user.get("full_name") or client_username or "Клиент"

    ticket = await db.open_or_activate_admin_dialog(
        client_tg_id=client_tg_id,
        client_username=client_username,
        client_name=client_name,
        admin_tg_id=None,
        admin_name=admin_name,
    )
    await db.add_ticket_message(ticket["id"], "admin", admin_name, text)
    await db.log_admin_action(
        admin_name,
        "Связь с клиентом (CRM)",
        f"Тикет #{ticket['id']} -> {client_name} (@{client_username or '—'})",
    )

    bot: Optional[Bot] = request.app.get("bot")
    if bot:
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
                client_tg_id,
                f"👩‍💼 <b>Администратор {admin_name} связался с вами:</b>\n\n{text}\n\n"
                f"<i>Вы можете ответить прямо в этом чате.</i>",
                parse_mode="HTML",
                reply_markup=close_kb,
            )
        except Exception as e:
            return web.json_response(
                {"error": f"Не удалось доставить сообщение в Telegram: {e}"}, status=400
            )

    return web.json_response({"ok": True, "ticket": ticket})


async def api_get_blacklist(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(await db.get_blacklist())


async def api_add_blacklist(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    username = (data.get("username") or "").strip().lstrip("@")
    reason = (data.get("reason") or "").strip()
    tg_id = int(data["tg_id"]) if str(data.get("tg_id", "")).strip().lstrip("-").isdigit() else None
    if not username:
        return web.json_response({"error": "Укажите @username пользователя в Telegram"}, status=400)

    await db.add_to_blacklist(username=username, tg_id=tg_id, reason=reason)
    await db.log_admin_action(
        get_actor_name(request),
        "Блокировка клиента (ЧС)",
        f"@{username} (Причина: {reason or 'не указана'})",
    )
    return web.json_response({"ok": True})


async def api_remove_blacklist(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    target = request.match_info["target"]
    await db.remove_from_blacklist(target)
    await db.log_admin_action(
        get_actor_name(request), "Разблокировка из ЧС", f"Пользователь: {target}"
    )
    return web.json_response({"ok": True})


async def api_get_weekly_report(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    start_date = request.query.get("start_date", "")
    end_date = request.query.get("end_date", "")
    if not start_date or not end_date:
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        start_date = monday.strftime("%Y-%m-%d")
        end_date = sunday.strftime("%Y-%m-%d")
    report = await db.get_weekly_financial_report(start_date, end_date)
    return web.json_response(report)


# ==================== REST API: КАТЕГОРИИ, УСЛУГИ, МАСТЕРА, АДМИНЫ, ТИКЕТЫ ====================
async def api_get_categories(request: web.Request):
    return web.json_response(await db.get_categories_full())


async def api_add_category(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    name = data["name"].strip()
    cat_id = await db.add_category(name)
    await db.log_admin_action(get_actor_name(request), "Добавление категории", name)
    return web.json_response({"ok": True, "id": cat_id})


async def api_update_category(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    cat_id = int(request.match_info["id"])
    data = await request.json()
    new_name = data["name"].strip()
    await db.update_category(cat_id, new_name)
    await db.log_admin_action(
        get_actor_name(request), "Редактирование категории", f"ID #{cat_id} -> {new_name}"
    )
    return web.json_response({"ok": True})


async def api_delete_category(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    cat_id = int(request.match_info["id"])
    await db.delete_category(cat_id)
    await db.log_admin_action(
        get_actor_name(request), "Удаление категории", f"Категория ID #{cat_id}"
    )
    return web.json_response({"ok": True})


async def api_get_services(request: web.Request):
    return web.json_response(await db.get_all_services())


async def api_add_service(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    srv_id = await db.add_service(
        category=data["category"],
        name=data["name"],
        price=int(data["price"]),
        duration_minutes=int(data["duration_minutes"]),
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Добавление услуги",
        f"{data['category']}: {data['name']} ({data['price']} ₽, {data['duration_minutes']} мин)",
    )
    return web.json_response({"ok": True, "id": srv_id})


async def api_update_service(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    srv_id = int(request.match_info["id"])
    data = await request.json()
    await db.update_service(
        service_id=srv_id,
        category=data["category"],
        name=data["name"],
        price=int(data["price"]),
        duration_minutes=int(data["duration_minutes"]),
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Редактирование услуги",
        f"ID #{srv_id}: {data['name']} ({data['price']} ₽, {data['duration_minutes']} мин)",
    )
    return web.json_response({"ok": True})


async def api_delete_service(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    srv_id = int(request.match_info["id"])
    await db.delete_service(srv_id)
    await db.log_admin_action(
        get_actor_name(request), "Удаление услуги", f"Услуга ID #{srv_id}"
    )
    return web.json_response({"ok": True})


async def api_get_masters(request: web.Request):
    return web.json_response(await db.get_all_masters(only_active=False))


async def api_add_master(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    tg_id = int(data["tg_id"]) if str(data.get("tg_id", "")).strip().isdigit() else None
    master_id = await db.add_master(
        name=data["name"],
        specialization=data["specialization"],
        categories=data.get("categories", []),
        tg_id=tg_id,
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Добавление мастера",
        f"{data['name']} (TG ID: {tg_id or 'нет'})",
    )
    return web.json_response({"ok": True, "id": master_id})


async def api_update_master(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    master_id = int(request.match_info["id"])
    data = await request.json()
    tg_id = int(data["tg_id"]) if str(data.get("tg_id", "")).strip().isdigit() else None
    await db.update_master(
        master_id=master_id,
        name=data["name"],
        specialization=data["specialization"],
        categories=data.get("categories", []),
        tg_id=tg_id,
        is_active=int(data.get("is_active", 1)),
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Редактирование мастера",
        f"Мастер #{master_id}: {data['name']} (TG ID: {tg_id or 'нет'})",
    )
    return web.json_response({"ok": True})


async def api_delete_master(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    master_id = int(request.match_info["id"])
    await db.delete_master(master_id)
    await db.log_admin_action(
        get_actor_name(request), "Удаление мастера", f"Мастер ID #{master_id}"
    )
    return web.json_response({"ok": True})


async def api_get_time_offs(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    master_id = request.query.get("master_id")
    offs = await db.get_master_time_offs(int(master_id) if master_id else None)
    return web.json_response(offs)


async def api_add_time_off(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    off_id = await db.add_master_time_off(
        master_id=int(data["master_id"]),
        off_type=data.get("type", "day_off"),
        start_datetime=data["start_datetime"],
        end_datetime=data["end_datetime"],
        reason=data.get("reason", ""),
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Добавление отгула/отпуска",
        f"Мастер #{data['master_id']}: {data['start_datetime']} — {data['end_datetime']}",
    )
    return web.json_response({"ok": True, "id": off_id})


async def api_update_time_off(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    off_id = int(request.match_info["id"])
    data = await request.json()
    await db.update_master_time_off(
        off_id=off_id,
        master_id=int(data["master_id"]),
        off_type=data.get("type", "day_off"),
        start_datetime=data["start_datetime"],
        end_datetime=data["end_datetime"],
        reason=data.get("reason", ""),
    )
    await db.log_admin_action(
        get_actor_name(request),
        "Редактирование отгула/отпуска",
        f"ID #{off_id}: {data['start_datetime']} — {data['end_datetime']}",
    )
    return web.json_response({"ok": True})


async def api_delete_time_off(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    off_id = int(request.match_info["id"])
    await db.delete_master_time_off(off_id)
    await db.log_admin_action(
        get_actor_name(request), "Удаление отгула/отпуска", f"ID #{off_id}"
    )
    return web.json_response({"ok": True})


async def api_get_admins(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(await db.get_admins())


async def api_add_admin(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    tg_id = int(data["tg_id"])
    name = data["name"].strip()
    await db.add_admin(tg_id, name)
    await db.log_admin_action(
        get_actor_name(request), "Добавление администратора", f"{name} (TG ID: {tg_id})"
    )
    return web.json_response({"ok": True})


async def api_delete_admin(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    tg_id = int(request.match_info["tg_id"])
    await db.remove_admin(tg_id)
    await db.log_admin_action(
        get_actor_name(request), "Удаление администратора", f"TG ID: {tg_id}"
    )
    return web.json_response({"ok": True})


async def api_get_logs(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(await db.get_admin_logs(250))


async def api_get_tickets(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(await db.get_all_tickets())


async def api_get_ticket_messages(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    ticket_id = int(request.match_info["id"])
    return web.json_response(await db.get_ticket_messages(ticket_id))


async def api_take_ticket(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    ticket_id = int(request.match_info["id"])
    data = await request.json()
    admin_name = data.get("admin_name") or get_actor_name(request)

    ticket = await db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        return web.json_response({"error": "Тикет уже закрыт"}, status=400)

    await db.take_support_ticket(ticket_id, None, admin_name)
    await db.log_admin_action(
        admin_name, "Взятие тикета поддержки", f"Тикет #{ticket_id} ({ticket['client_name']})"
    )

    bot: Optional[Bot] = request.app.get("bot")
    if bot and ticket["client_tg_id"]:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Завершить диалог с администратором",
                        callback_data=f"close_ticket_{ticket_id}",
                    )
                ]
            ]
        )
        try:
            await bot.send_message(
                ticket["client_tg_id"],
                f"👩‍💼 С вами на связи администратор <b>{admin_name}</b>.\n"
                f"Напишите ваш вопрос прямо в этот чат:",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass

    return web.json_response({"ok": True})


async def api_send_ticket_message(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    ticket_id = int(request.match_info["id"])
    data = await request.json()
    text = data["text"].strip()
    admin_name = data.get("admin_name") or get_actor_name(request)

    ticket = await db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        return web.json_response({"error": "Тикет закрыт"}, status=400)

    if ticket["status"] == "open":
        await db.take_support_ticket(ticket_id, None, admin_name)

    await db.add_ticket_message(ticket_id, "admin", admin_name, text)

    bot: Optional[Bot] = request.app.get("bot")
    if bot and ticket["client_tg_id"]:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Завершить диалог",
                        callback_data=f"close_ticket_{ticket_id}",
                    )
                ]
            ]
        )
        try:
            await bot.send_message(
                ticket["client_tg_id"],
                f"👩‍💼 <b>{admin_name}:</b>\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass

    return web.json_response({"ok": True})


async def api_close_ticket(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    ticket_id = int(request.match_info["id"])
    ticket = await db.get_ticket_by_id(ticket_id)
    if not ticket:
        return web.json_response({"error": "Не найден"}, status=404)

    await db.close_support_ticket(ticket_id)
    actor = get_actor_name(request)
    await db.log_admin_action(actor, "Закрытие тикета", f"Тикет #{ticket_id}")

    bot: Optional[Bot] = request.app.get("bot")
    if bot and ticket["client_tg_id"]:
        try:
            await bot.send_message(
                ticket["client_tg_id"],
                f"🔒 Диалог по обращению #{ticket_id} завершен администратором. Спасибо за обращение!",
            )
        except Exception:
            pass
    return web.json_response({"ok": True})


async def api_broadcast(request: web.Request):
    if not check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    bot: Optional[Bot] = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Telegram-бот не подключен"}, status=400)

    reader = await request.multipart()
    text = ""
    media_type = "photo"
    file_bytes = None
    filename = "media"

    async for field in reader:
        if field.name == "text":
            text = (await field.read(decode=True)).decode("utf-8", errors="ignore").strip()
        elif field.name == "media_type":
            media_type = (await field.read(decode=True)).decode("utf-8", errors="ignore").strip()
        elif field.name == "file":
            if field.filename:
                filename = field.filename
                file_bytes = await field.read()

    users = await db.get_all_users()
    sent_count = 0

    for u in users:
        tg_id = u["tg_id"]
        if not tg_id or tg_id < 0:
            continue
        try:
            if file_bytes and len(file_bytes) > 0:
                input_file = BufferedInputFile(file_bytes, filename=filename)
                if media_type == "video":
                    await bot.send_video(tg_id, video=input_file, caption=text or None, parse_mode="HTML")
                elif media_type == "document":
                    await bot.send_document(tg_id, document=input_file, caption=text or None, parse_mode="HTML")
                else:
                    await bot.send_photo(tg_id, photo=input_file, caption=text or None, parse_mode="HTML")
            else:
                await bot.send_message(tg_id, text, parse_mode="HTML")
            sent_count += 1
        except Exception:
            pass

    await db.log_admin_action(
        get_actor_name(request),
        "Массовая рассылка",
        f"Доставлено: {sent_count} из {len(users)} пользователей",
    )
    return web.json_response({"ok": True, "sent": sent_count, "total": len(users)})


# ==================== АДМИН-CRM С ВЕРТИКАЛЬНОЙ ДИАГРАММОЙ ГАНТА, КАССОЙ, КЛИЕНТАМИ И ОТЧЕТАМИ ====================
HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Beauty Salon — CRM, Касса и Диаграмма Ганта</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gantt-striped {
            background: repeating-linear-gradient(
                45deg,
                rgba(244, 63, 94, 0.14),
                rgba(244, 63, 94, 0.14) 8px,
                rgba(244, 63, 94, 0.28) 8px,
                rgba(244, 63, 94, 0.28) 16px
            );
        }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen">

<!-- Модальное окно авторизации -->
<div id="loginModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-sm space-y-3">
        <h2 class="text-xl font-bold text-slate-900">Вход в админ-панель</h2>
        <input id="adminNameInput" type="text" placeholder="Ваше имя (для логов и тикетов)" value="Администратор"
               class="w-full border border-slate-300 rounded-xl px-4 py-2 text-sm">
        <input id="passwordInput" type="password" placeholder="Пароль (admin123)"
               class="w-full border border-slate-300 rounded-xl px-4 py-2 text-sm">
        <button onclick="savePassword()"
                class="w-full bg-pink-600 hover:bg-pink-700 text-white font-medium py-2.5 rounded-xl transition">
            Войти
        </button>
    </div>
</div>

<!-- Шапка -->
<header class="bg-white border-b border-slate-200 sticky top-0 z-30">
    <div class="max-w-[1480px] mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-3">
            <span class="text-2xl">💅</span>
            <div>
                <h1 class="text-base font-bold text-slate-900">Beauty Salon CRM</h1>
                <p class="text-xs text-slate-500">Любое кол-во мастеров на услугу • Касса и Бонусы (+/-) • История клиентов • Отчётность 40%/60%</p>
            </div>
        </div>
        <div class="flex items-center gap-2">
            <span id="currentAdminBadge" class="text-xs bg-slate-100 px-3 py-1.5 rounded-lg text-slate-700 font-medium"></span>
            <button onclick="openContactClientModal()"
                    class="text-xs bg-indigo-600 hover:bg-indigo-700 text-white font-medium px-3 py-2 rounded-lg transition">
                ✉️ Написать клиенту
            </button>
            <button onclick="openBookingModal()"
                    class="text-xs bg-pink-600 hover:bg-pink-700 text-white font-medium px-3.5 py-2 rounded-lg transition">
                ➕ Записать клиента
            </button>
            <button onclick="logout()"
                    class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-3 py-2 rounded-lg">
                Аккаунт
            </button>
        </div>
    </div>
</header>

<main class="max-w-[1480px] mx-auto px-4 py-5">
    <!-- Вкладки -->
    <div class="flex flex-wrap gap-1.5 mb-5 border-b border-slate-200 pb-3">
        <button onclick="switchTab('gantt')" id="tab-gantt" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-pink-600 text-white">📊 Диаграмма Ганта</button>
        <button onclick="switchTab('appointments')" id="tab-appointments" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">📋 Записи и Касса</button>
        <button onclick="switchTab('clients')" id="tab-clients" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">👥 Клиенты и Бонусы</button>
        <button onclick="switchTab('reports')" id="tab-reports" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">📈 Недельный отчёт (40/60)</button>
        <button onclick="switchTab('blacklist')" id="tab-blacklist" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">🚫 Чёрный список (@username)</button>
        <button onclick="switchTab('masters')" id="tab-masters" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">👩‍🎨 Мастера и Отпуски</button>
        <button onclick="switchTab('services')" id="tab-services" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">✨ Категории и Услуги</button>
        <button onclick="switchTab('tickets')" id="tab-tickets" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">💬 Диалоги с клиентами</button>
        <button onclick="switchTab('broadcast')" id="tab-broadcast" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">📢 Рассылка</button>
        <button onclick="switchTab('admins')" id="tab-admins" class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100">🛡 Админы и Логи</button>
    </div>

    <!-- 1. ВЕРТИКАЛЬНАЯ ДИАГРАММА ГАНТА С КРАСНОЙ ЛИНИЕЙ ТЕКУЩЕГО ВРЕМЕНИ -->
    <section id="view-gantt" class="space-y-4">
        <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-4">
            <div class="flex items-center gap-2">
                <button onclick="shiftGanttDate(-1)" class="px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-xs font-medium">← Пред. день</button>
                <input type="date" id="ganttDate" onchange="onGanttDateChange()" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm font-bold">
                <button onclick="shiftGanttDate(1)" class="px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-xs font-medium">След. день →</button>
                <button onclick="setGanttToday()" class="px-3 py-1.5 rounded-xl bg-pink-50 text-pink-700 hover:bg-pink-100 text-xs font-medium">Сегодня</button>
            </div>
            <div class="flex flex-wrap items-center gap-4 text-xs">
                <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-amber-400 inline-block"></span> Ожидает звонка</span>
                <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-emerald-500 inline-block"></span> Подтверждена</span>
                <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-purple-600 inline-block"></span> 💳 Рассчитан (Оплачено)</span>
                <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-indigo-600 inline-block"></span> 🤝 Несколько мастеров</span>
                <span class="flex items-center gap-1.5"><span class="w-4 h-0.5 bg-red-600 inline-block"></span> 🔴 Текущее время</span>
            </div>
        </div>

        <div id="weekStrip" class="grid grid-cols-7 gap-2"></div>

        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <div id="verticalGanttWrapper" class="min-w-[820px]">
                <div id="verticalGanttHeader" class="flex border-b border-slate-200 bg-slate-50 sticky top-0 z-20"></div>
                <div id="verticalGanttBody" class="flex relative"></div>
            </div>
        </div>
    </section>

    <!-- 2. СПИСОК ЗАПИСЕЙ И КАССА -->
    <section id="view-appointments" class="space-y-4 hidden">
        <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-end justify-between gap-3">
            <div class="flex flex-wrap items-end gap-3">
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Дата</label>
                    <input type="date" id="filterDate" onchange="loadAppointments()" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Мастер</label>
                    <select id="filterMaster" onchange="loadAppointments()" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm bg-white"></select>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Статус</label>
                    <select id="filterStatus" onchange="loadAppointments()" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm bg-white">
                        <option value="">Все</option>
                        <option value="pending">⏳ Ожидает звонка</option>
                        <option value="confirmed">✅ Подтверждена</option>
                        <option value="paid">💳 Оплачена (Рассчитан)</option>
                        <option value="cancelled">❌ Отменена</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Поиск (имя, @username, телефон)</label>
                    <input type="text" id="searchClient" oninput="renderAppointments()" placeholder="Поиск..." class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm">
                </div>
            </div>
        </div>

        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <table class="w-full text-left border-collapse text-sm">
                <thead>
                    <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                        <th class="py-3 px-4">ID</th>
                        <th class="py-3 px-4">Дата и время</th>
                        <th class="py-3 px-4">Клиент / @username / Бонусы</th>
                        <th class="py-3 px-4">Услуга / Чеки</th>
                        <th class="py-3 px-4">Мастера</th>
                        <th class="py-3 px-4">Комментарий</th>
                        <th class="py-3 px-4">Статус</th>
                        <th class="py-3 px-4 text-right">Действия и Расчёт</th>
                    </tr>
                </thead>
                <tbody id="appointmentsTableBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>
    </section>

    <!-- 3. КЛИЕНТЫ, ИСТОРИЯ И РУЧНОЕ УПРАВЛЕНИЕ БОНУСАМИ -->
    <section id="view-clients" class="space-y-4 hidden">
        <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-4">
            <div class="flex flex-wrap items-center gap-2">
                <span class="text-xs font-bold text-slate-500 mr-1">Сортировать по:</span>
                <button onclick="changeClientsSort('last_visit')" id="csort-last_visit"
                        class="px-3 py-1.5 rounded-xl text-xs font-semibold bg-pink-600 text-white">
                    🕒 Дате последней записи
                </button>
                <button onclick="changeClientsSort('visits')" id="csort-visits"
                        class="px-3 py-1.5 rounded-xl text-xs font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
                    👣 Посещаемости
                </button>
                <button onclick="changeClientsSort('total_spent')" id="csort-total_spent"
                        class="px-3 py-1.5 rounded-xl text-xs font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200">
                    💰 Общей сумме
                </button>
            </div>
            <div>
                <input type="text" id="clientSearchInput" oninput="renderClientsTable()"
                       placeholder="Поиск по имени, телефону или @username..."
                       class="border border-slate-300 rounded-xl px-3.5 py-1.5 text-sm w-72">
            </div>
        </div>

        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <table class="w-full text-left border-collapse text-sm">
                <thead>
                    <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                        <th class="py-3 px-4">Клиент</th>
                        <th class="py-3 px-4">Telegram (@username)</th>
                        <th class="py-3 px-4">Посещаемость</th>
                        <th class="py-3 px-4">Вся сумма</th>
                        <th class="py-3 px-4">Средний чек</th>
                        <th class="py-3 px-4">Мастера клиента</th>
                        <th class="py-3 px-4">Последняя запись</th>
                        <th class="py-3 px-4">Бонусы</th>
                        <th class="py-3 px-4 text-right">Действия (История / Бонусы / Связь / ЧС)</th>
                    </tr>
                </thead>
                <tbody id="clientsTableBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>
    </section>

    <!-- 4. ОТЧЕТНОСТЬ ПО НЕДЕЛЯМ (40% МАСТЕР / 60% САЛОН) -->
    <section id="view-reports" class="space-y-5 hidden">
        <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-4">
            <div class="flex flex-wrap items-center gap-2">
                <button onclick="shiftReportWeek(-7)" class="px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-xs font-medium">← Пред. неделя</button>
                <input type="date" id="repStartDate" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm font-semibold">
                <span class="text-slate-400">—</span>
                <input type="date" id="repEndDate" class="border border-slate-300 rounded-xl px-3 py-1.5 text-sm font-semibold">
                <button onclick="loadWeeklyReport()" class="px-3.5 py-1.5 rounded-xl bg-pink-600 hover:bg-pink-700 text-white text-xs font-semibold">Показать отчёт</button>
                <button onclick="shiftReportWeek(7)" class="px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-xs font-medium">След. неделя →</button>
                <button onclick="setCurrentWeekReport()" class="px-3 py-1.5 rounded-xl bg-pink-50 text-pink-700 text-xs font-semibold">Текущая неделя</button>
            </div>
            <div class="text-xs text-slate-500">
                💡 При оплате баллами мастера получают <b>40% от полной стоимости услуги</b>, а салон — оставшуюся сумму.
            </div>
        </div>

        <div id="reportSummaryCards" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3"></div>

        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <div class="p-4 border-b border-slate-200 font-bold text-sm">👩‍🎨 Расчёт выплат по мастерам за период (40% от полной стоимости услуг)</div>
            <table class="w-full text-left border-collapse text-sm">
                <thead>
                    <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                        <th class="py-3 px-4">Мастер</th>
                        <th class="py-3 px-4">Участий в услугах</th>
                        <th class="py-3 px-4">Сумма услуг по прайсу (доля)</th>
                        <th class="py-3 px-4">Зарплата мастера (40%)</th>
                    </tr>
                </thead>
                <tbody id="reportMastersBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>

        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <div class="p-4 border-b border-slate-200 font-bold text-sm">🧾 Детализация оплаченных записей за неделю</div>
            <table class="w-full text-left border-collapse text-xs">
                <thead>
                    <tr class="border-b border-slate-200 text-slate-500 bg-slate-50">
                        <th class="py-2.5 px-4">Дата и время</th>
                        <th class="py-2.5 px-4">Клиент</th>
                        <th class="py-2.5 px-4">Услуга</th>
                        <th class="py-2.5 px-4">Мастера</th>
                        <th class="py-2.5 px-4">Способ оплаты</th>
                        <th class="py-2.5 px-4">Прайс (100%)</th>
                        <th class="py-2.5 px-4">Баллы / Скидка</th>
                        <th class="py-2.5 px-4">Оплачено</th>
                        <th class="py-2.5 px-4">Мастерам (40%)</th>
                        <th class="py-2.5 px-4">Салону (остаток)</th>
                    </tr>
                </thead>
                <tbody id="reportAppointmentsBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>
    </section>

    <!-- 5. ЧЕРНЫЙ СПИСОК ПО @USERNAME -->
    <section id="view-blacklist" class="grid grid-cols-1 lg:grid-cols-3 gap-6 hidden">
        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-3 h-fit">
            <h3 class="font-bold text-base text-rose-700">🚫 Заблокировать по @username</h3>
            <p class="text-xs text-slate-500">Заблокированный пользователь не сможет записываться через Telegram-бота.</p>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Telegram @username</label>
                <input id="blUsername" type="text" placeholder="@ivanova_beauty" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Причина блокировки</label>
                <input id="blReason" type="text" placeholder="Неявка без предупреждения" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
            <button onclick="addBlacklistEntry()" class="w-full bg-rose-600 hover:bg-rose-700 text-white font-semibold py-2.5 rounded-xl text-sm">
                🚫 Добавить в чёрный список
            </button>
        </div>

        <div class="lg:col-span-2 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
            <table class="w-full text-left border-collapse text-sm">
                <thead>
                    <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                        <th class="py-3 px-4">@username</th>
                        <th class="py-3 px-4">Telegram ID</th>
                        <th class="py-3 px-4">Причина</th>
                        <th class="py-3 px-4">Дата добавления</th>
                        <th class="py-3 px-4 text-right">Действие</th>
                    </tr>
                </thead>
                <tbody id="blacklistTableBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>
    </section>

    <!-- 6. МАСТЕРА И ОТГУЛЫ -->
    <section id="view-masters" class="space-y-6 hidden">
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-3">
                <h3 id="masterFormTitle" class="font-bold text-base">➕ Добавить / Изменить мастера</h3>
                <input type="hidden" id="editMasterId" value="">
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Имя и фамилия</label>
                    <input id="masterName" type="text" placeholder="Анна Смирнова" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Специализация</label>
                    <input id="masterSpec" type="text" placeholder="Топ-мастер маникюра" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Telegram ID мастера</label>
                    <input id="masterTgId" type="number" placeholder="123456789" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Категории мастера</label>
                    <div id="masterCategoriesCheckboxes" class="grid grid-cols-2 gap-2 text-sm mt-1"></div>
                </div>
                <div class="flex items-center gap-2 pt-1">
                    <input type="checkbox" id="masterActive" checked>
                    <label for="masterActive" class="text-xs text-slate-700">Активен</label>
                </div>
                <div class="flex gap-2">
                    <button onclick="saveMasterForm()" class="flex-1 bg-pink-600 hover:bg-pink-700 text-white font-medium py-2 rounded-xl text-sm">Сохранить</button>
                    <button onclick="resetMasterForm()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 px-3 py-2 rounded-xl text-sm">Сброс</button>
                </div>
            </div>

            <div class="lg:col-span-2 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                            <th class="py-3 px-4">Мастер</th>
                            <th class="py-3 px-4">Telegram ID</th>
                            <th class="py-3 px-4">Категории</th>
                            <th class="py-3 px-4">Статус</th>
                            <th class="py-3 px-4 text-right">Действия</th>
                        </tr>
                    </thead>
                    <tbody id="mastersTableBody" class="divide-y divide-slate-100"></tbody>
                </table>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-3">
                <h3 id="timeOffFormTitle" class="font-bold text-base">🏖 Отгулы и отпуски мастеров</h3>
                <input type="hidden" id="editTimeOffId" value="">
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Мастер</label>
                    <select id="timeOffMaster" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white"></select>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Тип</label>
                    <select id="timeOffType" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white">
                        <option value="day_off">🕒 Отгул</option>
                        <option value="vacation">✈️ Отпуск</option>
                    </select>
                </div>
                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">Начало (ГГГГ-ММ-ДД ЧЧ:ММ)</label>
                        <input id="timeOffStart" type="text" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">Конец (ГГГГ-ММ-ДД ЧЧ:ММ)</label>
                        <input id="timeOffEnd" type="text" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs">
                    </div>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Причина</label>
                    <input id="timeOffReason" type="text" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                </div>
                <div class="flex gap-2">
                    <button onclick="saveTimeOffForm()" class="flex-1 bg-pink-600 hover:bg-pink-700 text-white font-medium py-2 rounded-xl text-sm">Сохранить</button>
                    <button onclick="resetTimeOffForm()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 px-3 py-2 rounded-xl text-sm">Сброс</button>
                </div>
            </div>

            <div class="lg:col-span-2 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                            <th class="py-3 px-4">Мастер</th>
                            <th class="py-3 px-4">Тип</th>
                            <th class="py-3 px-4">Период</th>
                            <th class="py-3 px-4">Причина</th>
                            <th class="py-3 px-4 text-right">Действия</th>
                        </tr>
                    </thead>
                    <tbody id="timeOffsTableBody" class="divide-y divide-slate-100"></tbody>
                </table>
            </div>
        </div>
    </section>

    <!-- 7. КАТЕГОРИИ И УСЛУГИ -->
    <section id="view-services" class="space-y-6 hidden">
        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-4">
            <h3 class="font-bold text-base">📂 Категории услуг</h3>
            <div class="flex gap-2 max-w-md">
                <input id="newCatName" type="text" placeholder="Новая категория" class="flex-1 border border-slate-300 rounded-xl px-3 py-2 text-sm">
                <button onclick="addCategory()" class="bg-pink-600 hover:bg-pink-700 text-white px-4 py-2 rounded-xl text-sm font-medium">➕ Добавить</button>
            </div>
            <div id="categoriesList" class="flex flex-wrap gap-2"></div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-3">
                <h3 id="srvFormTitle" class="font-bold text-base">➕ Добавить / Изменить услугу</h3>
                <input type="hidden" id="editSrvId" value="">
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Категория</label>
                    <select id="srvCategory" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white"></select>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">Название услуги</label>
                    <input id="srvName" type="text" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                </div>
                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">Цена (₽)</label>
                        <input id="srvPrice" type="number" value="2000" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">Длительность (мин, кратно 15)</label>
                        <input id="srvDuration" type="number" value="60" step="15" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="saveServiceForm()" class="flex-1 bg-pink-600 hover:bg-pink-700 text-white font-medium py-2 rounded-xl text-sm">Сохранить</button>
                    <button onclick="resetServiceForm()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 px-3 py-2 rounded-xl text-sm">Сброс</button>
                </div>
            </div>

            <div class="lg:col-span-2 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-x-auto">
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b border-slate-200 text-xs text-slate-500 bg-slate-50">
                            <th class="py-3 px-4">Категория</th>
                            <th class="py-3 px-4">Услуга</th>
                            <th class="py-3 px-4">Цена</th>
                            <th class="py-3 px-4">Длительность</th>
                            <th class="py-3 px-4 text-right">Действия</th>
                        </tr>
                    </thead>
                    <tbody id="servicesTableBody" class="divide-y divide-slate-100"></tbody>
                </table>
            </div>
        </div>
    </section>

    <!-- 8. ТИКЕТЫ И СВЯЗЬ С КЛИЕНТОМ В ЛЮБОЕ ВРЕМЯ -->
    <section id="view-tickets" class="grid grid-cols-1 lg:grid-cols-3 gap-6 hidden">
        <div class="bg-white rounded-2xl border border-slate-200 shadow-sm p-4 space-y-3">
            <div class="flex justify-between items-center">
                <h3 class="font-bold text-sm">Диалоги с клиентами</h3>
                <div class="flex gap-2">
                    <button onclick="openContactClientModal()" class="text-xs bg-indigo-600 text-white px-2.5 py-1 rounded-lg">✉️ Написать первым</button>
                    <button onclick="loadTickets()" class="text-xs text-pink-600 hover:underline">Обновить</button>
                </div>
            </div>
            <div id="ticketsList" class="space-y-2 max-h-[540px] overflow-y-auto"></div>
        </div>

        <div class="lg:col-span-2 bg-white rounded-2xl border border-slate-200 shadow-sm p-5 flex flex-col h-[600px]">
            <div id="ticketChatHeader" class="border-b border-slate-200 pb-3 mb-3 flex justify-between items-center">
                <span class="text-sm text-slate-500">Выберите диалог слева или нажмите «Написать первым»</span>
            </div>
            <div id="ticketMessagesBox" class="flex-1 overflow-y-auto space-y-2 pr-2"></div>
            <div id="ticketInputArea" class="pt-3 border-t border-slate-200 flex gap-2 hidden">
                <input id="ticketReplyInput" type="text" placeholder="Введите ответ клиенту..."
                       onkeydown="if(event.key==='Enter') sendTicketReply()"
                       class="flex-1 border border-slate-300 rounded-xl px-3.5 py-2 text-sm">
                <button onclick="sendTicketReply()" class="bg-pink-600 hover:bg-pink-700 text-white px-4 py-2 rounded-xl text-sm font-medium">Отправить</button>
            </div>
        </div>
    </section>

    <!-- 9. РАССЫЛКА -->
    <section id="view-broadcast" class="max-w-2xl bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4 hidden">
        <h3 class="font-bold text-base">📢 Массовая рассылка с медиафайлами</h3>
        <div>
            <label class="block text-xs text-slate-500 mb-1">Тип медиа</label>
            <select id="bcMediaType" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white">
                <option value="photo">🖼 Фото</option>
                <option value="video">🎬 Видео</option>
                <option value="document">📎 Документ</option>
            </select>
        </div>
        <div>
            <input id="bcFile" type="file" class="w-full text-sm">
        </div>
        <div>
            <textarea id="bcText" rows="5" placeholder="Текст рассылки..." class="w-full border border-slate-300 rounded-xl p-3 text-sm"></textarea>
        </div>
        <button onclick="sendBroadcast()" id="bcSubmitBtn" class="bg-pink-600 hover:bg-pink-700 text-white font-medium px-6 py-2.5 rounded-xl text-sm">
            🚀 Запустить рассылку
        </button>
    </section>

    <!-- 10. АДМИНЫ И ЛОГИ -->
    <section id="view-admins" class="grid grid-cols-1 lg:grid-cols-3 gap-6 hidden">
        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-4">
            <h3 class="font-bold text-base">🛡 Администраторы</h3>
            <div class="space-y-2">
                <input id="newAdminTgId" type="number" placeholder="Telegram ID" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                <input id="newAdminName" type="text" placeholder="Имя администратора" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
                <button onclick="addAdmin()" class="w-full bg-pink-600 hover:bg-pink-700 text-white font-medium py-2 rounded-xl text-sm">➕ Добавить</button>
            </div>
            <div id="adminsList" class="divide-y divide-slate-100 text-sm"></div>
        </div>

        <div class="lg:col-span-2 bg-white p-5 rounded-2xl border border-slate-200 shadow-sm space-y-3">
            <div class="flex justify-between items-center">
                <h3 class="font-bold text-base">📜 Логи действий администраторов</h3>
                <button onclick="loadLogs()" class="text-xs text-pink-600 hover:underline">Обновить</button>
            </div>
            <div class="overflow-x-auto max-h-[520px] overflow-y-auto">
                <table class="w-full text-left border-collapse text-xs">
                    <thead>
                        <tr class="border-b border-slate-200 text-slate-500 bg-slate-50">
                            <th class="py-2.5 px-3">Время</th>
                            <th class="py-2.5 px-3">Администратор</th>
                            <th class="py-2.5 px-3">Действие</th>
                            <th class="py-2.5 px-3">Подробности</th>
                        </tr>
                    </thead>
                    <tbody id="logsTableBody" class="divide-y divide-slate-100"></tbody>
                </table>
            </div>
        </div>
    </section>
</main>

<!-- МОДАЛЬНОЕ ОКНО СОЗДАНИЯ И РЕДАКТИРОВАНИЯ ЗАПИСИ (ЛЮБОЕ КОЛ-ВО МАСТЕРОВ + @USERNAME) -->
<div id="bookingModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-6 w-full max-w-lg space-y-3 max-h-[92vh] overflow-y-auto">
        <div class="flex justify-between items-center">
            <h3 id="bookingModalTitle" class="font-bold text-base">Новая запись</h3>
            <button onclick="closeBookingModal()" class="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <input type="hidden" id="modalBookingId" value="">

        <div>
            <div class="flex justify-between items-center mb-1">
                <label class="text-xs font-semibold text-slate-600">👩‍🎨 Мастера (можно выбрать любое количество, хоть всех):</label>
                <div class="space-x-2">
                    <button type="button" onclick="toggleAllBookingMasters(true)" class="text-[11px] text-pink-600 font-semibold hover:underline">Выбрать всех</button>
                    <button type="button" onclick="toggleAllBookingMasters(false)" class="text-[11px] text-slate-500 hover:underline">Сбросить</button>
                </div>
            </div>
            <div id="modalMastersCheckboxes" class="grid grid-cols-1 sm:grid-cols-2 gap-1.5 border border-slate-200 rounded-xl p-2.5 bg-slate-50 max-h-36 overflow-y-auto"></div>
        </div>

        <div>
            <label class="block text-xs text-slate-500 mb-1">Услуга</label>
            <select id="modalService" onchange="loadModalSlots()" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white"></select>
        </div>

        <div class="grid grid-cols-2 gap-3">
            <div>
                <label class="block text-xs text-slate-500 mb-1">Дата</label>
                <input type="date" id="modalDate" onchange="loadModalSlots()" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Время (шаг 15 мин)</label>
                <select id="modalTime" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white"></select>
            </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
            <div>
                <label class="block text-xs text-slate-500 mb-1">Имя клиента</label>
                <input type="text" id="modalClientName" placeholder="Екатерина" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Телефон</label>
                <input type="text" id="modalClientPhone" placeholder="+79991234567" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Telegram @username</label>
                <input type="text" id="modalClientUsername" placeholder="@katya_beauty" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
            </div>
        </div>

        <div>
            <label class="block text-xs text-slate-500 mb-1">💬 Комментарий к записи</label>
            <textarea id="modalComment" rows="2" placeholder="Пожелания клиента, дизайн, заметки..."
                      class="w-full border border-slate-300 rounded-xl p-2.5 text-sm"></textarea>
        </div>

        <div id="modalStatusBlock" class="hidden">
            <label class="block text-xs text-slate-500 mb-1">Статус записи</label>
            <select id="modalStatus" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm bg-white">
                <option value="pending">⏳ Ожидает звонка</option>
                <option value="confirmed">✅ Подтверждена (звонок выполнен)</option>
                <option value="paid">💳 Оплачена (Рассчитан)</option>
                <option value="cancelled">❌ Отменена</option>
            </select>
        </div>

        <div class="flex flex-wrap gap-2 pt-2">
            <button onclick="submitBookingModal()" class="flex-1 bg-pink-600 hover:bg-pink-700 text-white font-semibold py-2.5 rounded-xl text-sm">
                💾 Сохранить
            </button>
            <button id="openPayFromEditBtn" onclick="openPaymentFromBookingModal()" class="hidden bg-purple-600 hover:bg-purple-700 text-white font-semibold px-4 py-2.5 rounded-xl text-sm">
                💳 Рассчитать клиента
            </button>
        </div>
    </div>
</div>

<!-- МОДАЛЬНОЕ ОКНО КАССЫ: РАСЧЕТ КЛИЕНТА -->
<div id="paymentModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-6 w-full max-w-md space-y-4">
        <div class="flex justify-between items-center border-b border-slate-200 pb-3">
            <h3 class="font-bold text-base text-purple-900">💳 Расчёт клиента (Касса)</h3>
            <button onclick="closePaymentModal()" class="text-slate-400 hover:text-slate-600">✕</button>
        </div>

        <div id="payInfoSummary" class="bg-slate-50 rounded-xl p-3 text-xs space-y-1"></div>

        <div>
            <label class="block text-xs font-semibold text-slate-600 mb-1.5">Способ оплаты</label>
            <div class="grid grid-cols-2 gap-2">
                <button type="button" onclick="selectPayMethod('cash')" id="payBtn-cash"
                        class="py-2.5 px-3 rounded-xl border text-xs font-bold bg-purple-600 text-white border-purple-600">
                    💵 Наличный расчёт (+5% баллов)
                </button>
                <button type="button" onclick="selectPayMethod('cashless')" id="payBtn-cashless"
                        class="py-2.5 px-3 rounded-xl border text-xs font-bold bg-white text-slate-700 border-slate-300">
                    💳 Безналичный 
                </button>
            </div>
        </div>

        <div class="grid grid-cols-2 gap-3">
            <div>
                <div class="flex justify-between items-center mb-1">
                    <label class="text-xs text-slate-500">Списать баллы</label>
                    <button type="button" onclick="useMaxPoints()" class="text-[11px] text-purple-600 font-semibold hover:underline">Макс.</button>
                </div>
                <input type="number" id="payPointsInput" min="0" value="0" oninput="recalcPaymentPreview()"
                       class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm font-semibold">
            </div>
            <div>
                <label class="block text-xs text-slate-500 mb-1">Доп. скидка (₽)</label>
                <input type="number" id="payDiscountInput" min="0" value="0" oninput="recalcPaymentPreview()"
                       class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm font-semibold">
            </div>
        </div>

        <div id="payCalcPreview" class="bg-purple-50 border border-purple-200 rounded-xl p-3.5 text-xs space-y-1.5"></div>

        <button onclick="submitPaymentModal()" class="w-full bg-purple-600 hover:bg-purple-700 text-white font-bold py-3 rounded-xl text-sm shadow">
            ✅ Провести оплату и отправить чек в Telegram
        </button>
    </div>
</div>

<!-- МОДАЛЬНОЕ ОКНО РУЧНОГО НАЧИСЛЕНИЯ / СПИСАНИЯ БОНУСОВ -->
<div id="bonusAdjustModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm space-y-3">
        <div class="flex justify-between items-center border-b border-slate-200 pb-2">
            <h3 class="font-bold text-base text-purple-900">🎁 Изменить бонусы вручную</h3>
            <button onclick="document.getElementById('bonusAdjustModal').classList.add('hidden')" class="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div id="bonusModalClientInfo" class="text-xs text-slate-600 bg-slate-50 p-3 rounded-xl"></div>
        <div>
            <label class="block text-xs text-slate-500 mb-1">Количество баллов (например: +200 или -150)</label>
            <input type="number" id="bonusDeltaInput" placeholder="200 или -100" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm font-bold">
        </div>
        <div>
            <label class="block text-xs text-slate-500 mb-1">Комментарий (необязательно)</label>
            <input type="text" id="bonusReasonInput" placeholder="Подарок ко дню рождения / корректировка" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
        </div>
        <button onclick="submitBonusAdjustModal()" class="w-full bg-purple-600 hover:bg-purple-700 text-white font-bold py-2.5 rounded-xl text-sm">
            💾 Применить изменение
        </button>
    </div>
</div>

<!-- МОДАЛЬНОЕ ОКНО СВЯЗИ С КЛИЕНТОМ В ЛЮБОЕ ВРЕМЯ -->
<div id="contactClientModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-6 w-full max-w-md space-y-3">
        <div class="flex justify-between items-center border-b border-slate-200 pb-2">
            <h3 class="font-bold text-base text-indigo-900">✉️ Связаться с клиентом через бота</h3>
            <button onclick="document.getElementById('contactClientModal').classList.add('hidden')" class="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <p class="text-xs text-slate-500">Клиент получит сообщение в Telegram и сможет сразу ответить вам в режиме диалога.</p>
        <div>
            <label class="block text-xs text-slate-500 mb-1">@username, телефон или Telegram ID клиента</label>
            <input type="text" id="contactClientIdentifier" placeholder="@username или +7999..." class="w-full border border-slate-300 rounded-xl px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-xs text-slate-500 mb-1">Текст сообщения</label>
            <textarea id="contactClientText" rows="3" placeholder="Здравствуйте! Пишем вам из салона красоты..." class="w-full border border-slate-300 rounded-xl p-3 text-sm"></textarea>
        </div>
        <button onclick="submitContactClientModal()" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2.5 rounded-xl text-sm">
            📨 Отправить и открыть диалог
        </button>
    </div>
</div>

<!-- МОДАЛЬНОЕ ОКНО ИСТОРИИ КЛИЕНТА -->
<div id="clientHistoryModal" class="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50 hidden">
    <div class="bg-white rounded-2xl shadow-xl p-6 w-full max-w-3xl space-y-4 max-h-[90vh] overflow-y-auto">
        <div class="flex justify-between items-center border-b border-slate-200 pb-3">
            <h3 id="clientHistoryTitle" class="font-bold text-base">История клиента</h3>
            <button onclick="document.getElementById('clientHistoryModal').classList.add('hidden')" class="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div id="clientHistoryStats" class="grid grid-cols-2 sm:grid-cols-4 gap-3"></div>
        <div class="overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
                <thead>
                    <tr class="border-b border-slate-200 text-slate-500 bg-slate-50">
                        <th class="py-2.5 px-3">ID</th>
                        <th class="py-2.5 px-3">Дата и время</th>
                        <th class="py-2.5 px-3">Услуга</th>
                        <th class="py-2.5 px-3">Мастера</th>
                        <th class="py-2.5 px-3">Статус</th>
                        <th class="py-2.5 px-3">Оплата / Баллы</th>
                        <th class="py-2.5 px-3">Комментарий</th>
                    </tr>
                </thead>
                <tbody id="clientHistoryTableBody" class="divide-y divide-slate-100"></tbody>
            </table>
        </div>
    </div>
</div>

<script>
let allCategories = [];
let allServices = [];
let allMasters = [];
let allTimeOffs = [];
let allAppointments = [];
let ganttAppointments = [];
let allClients = [];
let currentClientsSort = 'last_visit';
let activeTicketId = null;

let currentPayingApp = null;
let selectedPayMethod = 'cash';
let currentBonusClient = null;

const ROW_HEIGHT_PX = 36;

function getTodayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function getPass() { return localStorage.getItem('salon_admin_pass') || 'admin123'; }
function getAdminName() { return localStorage.getItem('salon_admin_name') || 'Администратор'; }

function savePassword() {
    localStorage.setItem('salon_admin_pass', document.getElementById('passwordInput').value.trim());
    localStorage.setItem('salon_admin_name', document.getElementById('adminNameInput').value.trim() || 'Администратор');
    document.getElementById('loginModal').classList.add('hidden');
    initApp();
}

function logout() { document.getElementById('loginModal').classList.remove('hidden'); }

async function apiFetch(url, options = {}) {
    options.headers = options.headers || {};
    options.headers['X-Admin-Password'] = getPass();
    options.headers['X-Admin-Name'] = encodeURIComponent(getAdminName());
    if (options.body && !(options.body instanceof FormData)) {
        options.headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, options);
    if (res.status === 401) {
        document.getElementById('loginModal').classList.remove('hidden');
        throw new Error('Unauthorized');
    }
    const data = await res.json();
    if (!res.ok) {
        alert(data.error || 'Ошибка запроса');
        throw new Error(data.error || 'Error');
    }
    return data;
}

function switchTab(tab) {
    ['gantt', 'appointments', 'clients', 'reports', 'blacklist', 'masters', 'services', 'tickets', 'broadcast', 'admins'].forEach(t => {
        document.getElementById('view-' + t).classList.toggle('hidden', t !== tab);
        const btn = document.getElementById('tab-' + t);
        btn.className = t === tab
            ? 'px-3.5 py-2 rounded-xl text-xs font-semibold bg-pink-600 text-white'
            : 'px-3.5 py-2 rounded-xl text-xs font-semibold bg-white text-slate-700 hover:bg-slate-100';
    });
    if (tab === 'clients') loadClients();
    if (tab === 'reports') loadWeeklyReport();
    if (tab === 'blacklist') loadBlacklist();
    if (tab === 'tickets') loadTickets();
    if (tab === 'admins') { loadAdmins(); loadLogs(); }
}

// ==================== ВЕРТИКАЛЬНАЯ ДИАГРАММА ГАНТА + КРАСНАЯ ЛИНИЯ ТЕКУЩЕГО ВРЕМЕНИ ====================
function setGanttToday() {
    document.getElementById('ganttDate').value = getTodayStr();
    onGanttDateChange();
}

function shiftGanttDate(days) {
    const inp = document.getElementById('ganttDate');
    const d = inp.value ? new Date(inp.value + 'T00:00:00') : new Date();
    d.setDate(d.getDate() + days);
    inp.value = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    onGanttDateChange();
}

async function onGanttDateChange() {
    const dateStr = document.getElementById('ganttDate').value;
    ganttAppointments = await apiFetch('/api/appointments?date=' + dateStr);
    renderWeekStrip(dateStr);
    renderVerticalGanttChart(dateStr);
}

function renderWeekStrip(selectedDateStr) {
    const strip = document.getElementById('weekStrip');
    const base = new Date(selectedDateStr + 'T00:00:00');
    const dayNames = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
    let html = '';
    for (let i = -3; i <= 3; i++) {
        const d = new Date(base);
        d.setDate(d.getDate() + i);
        const dStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        const active = dStr === selectedDateStr;
        html += `
        <button onclick="document.getElementById('ganttDate').value='${dStr}'; onGanttDateChange();"
                class="p-2 rounded-xl border text-center transition ${active ? 'bg-pink-600 text-white border-pink-600' : 'bg-white text-slate-700 border-slate-200 hover:bg-slate-50'}">
            <div class="text-[11px] opacity-75">${dayNames[d.getDay()]}</div>
            <div class="text-xs font-bold">${String(d.getDate()).padStart(2,'0')}.${String(d.getMonth()+1).padStart(2,'0')}</div>
        </button>`;
    }
    strip.innerHTML = html;
}

function timeToMinutes(tStr) {
    const [h, m] = tStr.split(':').map(Number);
    return h * 60 + m;
}

function getRedTimeLineHtml() {
    const now = new Date();
    const curMin = now.getHours() * 60 + now.getMinutes();
    const startMin = 10 * 60;
    const endMin = 20 * 60;
    if (curMin < startMin || curMin > endMin) return '';
    const topPx = ((curMin - startMin) / 15) * ROW_HEIGHT_PX;
    const timeLabel = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`;
    return `
        <div id="ganttCurrentTimeLine" style="top:${topPx}px;"
             class="absolute left-0 right-0 z-30 pointer-events-none flex items-center">
            <span class="bg-red-600 text-white text-[10px] font-bold px-2 py-0.5 rounded-r-full shadow">
                🔴 ${timeLabel}
            </span>
            <div class="flex-1 border-t-2 border-red-600 shadow-sm"></div>
        </div>
    `;
}

function renderVerticalGanttChart(dateStr) {
    const activeMasters = allMasters.filter(m => m.is_active);
    const headerEl = document.getElementById('verticalGanttHeader');
    const bodyEl = document.getElementById('verticalGanttBody');
    const startMin = 10 * 60;
    const totalSteps = 40;
    const totalHeightPx = totalSteps * ROW_HEIGHT_PX;

    headerEl.innerHTML = `
        <div class="w-24 shrink-0 p-3 border-r border-slate-200 text-xs font-bold text-slate-600 text-center">
            Время (15м)
        </div>
        ${activeMasters.map(m => `
            <div class="flex-1 min-w-[210px] p-3 border-r border-slate-200 text-center">
                <div class="font-bold text-xs text-slate-900">👩‍🎨 ${m.name}</div>
                <div class="text-[11px] text-slate-400 truncate">${(m.categories || []).join(', ')}</div>
            </div>
        `).join('')}
    `;

    let timeRowsHtml = '';
    for (let step = 0; step < totalSteps; step++) {
        const mins = startMin + step * 15;
        const hh = String(Math.floor(mins / 60)).padStart(2, '0');
        const mm = String(mins % 60).padStart(2, '0');
        const isHourStart = mm === '00';
        timeRowsHtml += `
            <div style="height:${ROW_HEIGHT_PX}px;"
                 class="border-b ${isHourStart ? 'border-slate-300 bg-slate-100/70 font-bold text-slate-800' : 'border-slate-100 text-slate-400'} px-2 flex items-center justify-center font-mono text-xs">
                ${hh}:${mm}
            </div>
        `;
    }

    const dayStart = new Date(`${dateStr}T10:00:00`);
    const dayEnd = new Date(`${dateStr}T20:00:00`);

    const masterColumnsHtml = activeMasters.map(m => {
        let gridCells = '';
        for (let step = 0; step < totalSteps; step++) {
            const mins = startMin + step * 15;
            const hh = String(Math.floor(mins / 60)).padStart(2, '0');
            const mm = String(mins % 60).padStart(2, '0');
            const timeLabel = `${hh}:${mm}`;
            const isHourStart = mm === '00';
            gridCells += `
                <div style="height:${ROW_HEIGHT_PX}px;"
                     onclick="openBookingModal(null, ${m.id}, '${dateStr}', '${timeLabel}')"
                     class="border-b ${isHourStart ? 'border-slate-200' : 'border-slate-100'} hover:bg-pink-50/70 cursor-pointer flex items-center justify-center group transition">
                    <span class="opacity-0 group-hover:opacity-100 text-[10px] text-pink-600 font-semibold">+ ${timeLabel}</span>
                </div>
            `;
        }

        let offBlocks = '';
        allTimeOffs.filter(o => o.master_id === m.id).forEach(o => {
            const os = new Date(o.start_datetime.replace(' ', 'T') + ':00');
            const oe = new Date(o.end_datetime.replace(' ', 'T') + ':00');
            if (os < dayEnd && oe > dayStart) {
                const effStart = os < dayStart ? dayStart : os;
                const effEnd = oe > dayEnd ? dayEnd : oe;
                const topMin = (effStart.getHours() * 60 + effStart.getMinutes()) - startMin;
                const durMin = (effEnd - effStart) / 60000;
                const topPx = Math.max(0, (topMin / 15) * ROW_HEIGHT_PX);
                const heightPx = Math.max(24, (durMin / 15) * ROW_HEIGHT_PX);
                offBlocks += `
                <div style="top:${topPx}px; height:${heightPx}px;"
                     class="absolute left-1.5 right-1.5 gantt-striped border border-rose-300 rounded-xl p-2 flex items-center justify-center text-xs font-bold text-rose-900 pointer-events-none z-10">
                    🏖 ${o.type === 'vacation' ? 'Отпуск' : 'Отгул'} ${o.reason ? '(' + o.reason + ')' : ''}
                </div>`;
            }
        });

        let appBlocks = '';
        ganttAppointments.filter(a => {
            if (a.status === 'cancelled') return false;
            const mIds = a.master_ids && a.master_ids.length ? a.master_ids : [a.master_id];
            return mIds.includes(m.id);
        }).forEach(a => {
            const tPart = a.appointment_datetime.split(' ')[1];
            const sMin = timeToMinutes(tPart);
            const topPx = ((sMin - startMin) / 15) * ROW_HEIGHT_PX;
            const heightPx = Math.max(ROW_HEIGHT_PX - 2, (a.service_duration / 15) * ROW_HEIGHT_PX - 2);
            const endMin = sMin + a.service_duration;
            const endTime = `${String(Math.floor(endMin/60)).padStart(2,'0')}:${String(endMin%60).padStart(2,'0')}`;

            const mNames = a.masters_names || [a.master_name];
            const isJoint = mNames.length > 1;
            let boxStyle = 'bg-amber-400 hover:bg-amber-500 text-slate-900 border-amber-600';
            let statusIcon = '⏳';

            if (a.status === 'paid') {
                boxStyle = 'bg-purple-600 hover:bg-purple-700 text-white border-purple-900';
                statusIcon = '💳';
            } else if (a.status === 'confirmed') {
                boxStyle = isJoint
                    ? 'bg-indigo-600 hover:bg-indigo-700 text-white border-indigo-800'
                    : 'bg-emerald-500 hover:bg-emerald-600 text-white border-emerald-700';
                statusIcon = '✅';
            }

            const paidBadge = a.status === 'paid'
                ? `<span class="bg-white/20 px-1.5 py-0.5 rounded text-[10px]">${a.final_paid} ₽</span>`
                : '';

            appBlocks += `
            <div onclick='openBookingModal(${JSON.stringify(a)})'
                 style="top:${topPx + 1}px; height:${heightPx}px;"
                 class="absolute left-1.5 right-1.5 ${boxStyle} border rounded-xl px-2.5 py-1.5 shadow-md cursor-pointer overflow-hidden z-20 flex flex-col justify-between transition">
                <div>
                    <div class="flex items-center justify-between gap-1 text-[10px] font-bold">
                        <span>${tPart}–${endTime}</span>
                        <span>${statusIcon} #${a.id} ${paidBadge}</span>
                    </div>
                    <div class="font-bold text-xs truncate mt-0.5">${a.client_name} ${a.client_username ? '(@'+a.client_username+')' : ''}</div>
                    <div class="text-[11px] opacity-95 truncate">${a.service_name}</div>
                    ${isJoint ? `<div class="text-[10px] font-semibold bg-black/20 rounded px-1.5 py-0.5 mt-0.5 truncate">🤝 Мастера (${mNames.length}): ${mNames.join(' + ')}</div>` : ''}
                    ${a.comment ? `<div class="text-[10px] italic opacity-90 truncate mt-0.5">💬 ${a.comment}</div>` : ''}
                </div>
            </div>`;
        });

        return `
        <div style="height:${totalHeightPx}px;" class="flex-1 min-w-[210px] border-r border-slate-200 relative">
            ${gridCells}
            ${offBlocks}
            ${appBlocks}
        </div>`;
    }).join('');

    bodyEl.innerHTML = `
        <div class="w-24 shrink-0 border-r border-slate-200 bg-slate-50/50">${timeRowsHtml}</div>
        ${masterColumnsHtml}
        ${getRedTimeLineHtml()}
    `;
}

setInterval(() => {
    const dateStr = document.getElementById('ganttDate')?.value;
    if (dateStr) renderVerticalGanttChart(dateStr);
}, 60000);

// ==================== МОДАЛЬНОЕ ОКНО ЗАПИСИ (ЛЮБОЕ КОЛ-ВО МАСТЕРОВ + @USERNAME) ====================
function getSelectedBookingMasterIds() {
    return Array.from(document.querySelectorAll('.booking-master-cb:checked')).map(cb => Number(cb.value));
}

function toggleAllBookingMasters(selectAll) {
    const cbs = document.querySelectorAll('.booking-master-cb');
    cbs.forEach((cb, idx) => {
        cb.checked = selectAll ? true : (idx === 0);
    });
    loadModalSlots();
}

async function openBookingModal(existingApp = null, prefillMasterId = null, prefillDate = null, prefillTime = null) {
    currentPayingApp = existingApp;
    const activeMasters = allMasters.filter(m => m.is_active);

    const selectedIds = existingApp
        ? (existingApp.master_ids && existingApp.master_ids.length ? existingApp.master_ids : [existingApp.master_id])
        : (prefillMasterId ? [Number(prefillMasterId)] : (activeMasters[0] ? [activeMasters[0].id] : []));

    document.getElementById('modalMastersCheckboxes').innerHTML = activeMasters.map(m => `
        <label class="flex items-center gap-2 text-xs bg-white px-2.5 py-1.5 rounded-lg border border-slate-200 cursor-pointer">
            <input type="checkbox" value="${m.id}" onchange="loadModalSlots()" class="booking-master-cb"
                   ${selectedIds.includes(m.id) ? 'checked' : ''}>
            <span class="font-medium truncate">👩‍🎨 ${m.name}</span>
        </label>
    `).join('');

    document.getElementById('modalService').innerHTML = allServices.map(s =>
        `<option value="${s.id}">${s.category}: ${s.name} (${s.price} ₽, ${s.duration_minutes} мин)</option>`
    ).join('');

    if (existingApp) {
        document.getElementById('modalBookingId').value = existingApp.id;
        document.getElementById('bookingModalTitle').textContent = `✏️ Запись #${existingApp.id}`;
        document.getElementById('modalService').value = existingApp.service_id;
        const [dPart, tPart] = existingApp.appointment_datetime.split(' ');
        document.getElementById('modalDate').value = dPart;
        document.getElementById('modalClientName').value = existingApp.client_name;
        document.getElementById('modalClientPhone').value = existingApp.client_phone;
        document.getElementById('modalClientUsername').value = existingApp.client_username ? '@' + existingApp.client_username.replace(/^@/, '') : '';
        document.getElementById('modalComment').value = existingApp.comment || '';
        document.getElementById('modalStatus').value = existingApp.status;
        document.getElementById('modalStatusBlock').classList.remove('hidden');
        document.getElementById('openPayFromEditBtn').classList.toggle('hidden', existingApp.status === 'paid' || existingApp.status === 'cancelled');
        document.getElementById('bookingModal').classList.remove('hidden');
        await loadModalSlots(tPart);
    } else {
        document.getElementById('modalBookingId').value = '';
        document.getElementById('bookingModalTitle').textContent = '➕ Новая запись клиента';
        document.getElementById('modalDate').value = prefillDate || document.getElementById('ganttDate').value || getTodayStr();
        document.getElementById('modalClientName').value = '';
        document.getElementById('modalClientPhone').value = '';
        document.getElementById('modalClientUsername').value = '';
        document.getElementById('modalComment').value = '';
        document.getElementById('modalStatusBlock').classList.add('hidden');
        document.getElementById('openPayFromEditBtn').classList.add('hidden');
        document.getElementById('bookingModal').classList.remove('hidden');
        await loadModalSlots(prefillTime);
    }
}

function closeBookingModal() {
    document.getElementById('bookingModal').classList.add('hidden');
}

function openPaymentFromBookingModal() {
    if (!currentPayingApp) return;
    closeBookingModal();
    openPaymentModal(currentPayingApp);
}

async function loadModalSlots(preferredTime = null) {
    const service_id = document.getElementById('modalService').value;
    const masterIds = getSelectedBookingMasterIds();
    const date = document.getElementById('modalDate').value;
    const exclude_id = document.getElementById('modalBookingId').value;
    const sel = document.getElementById('modalTime');

    if (!service_id || !masterIds.length || !date) {
        sel.innerHTML = '<option value="">Выберите хотя бы 1 мастера</option>';
        return;
    }

    const params = new URLSearchParams({
        master_ids: masterIds.join(','),
        service_id,
        date,
        exclude_id
    });
    const slots = await apiFetch('/api/slots?' + params.toString());
    let list = [...slots];
    if (preferredTime && !list.includes(preferredTime)) list.unshift(preferredTime);

    if (!list.length) {
        sel.innerHTML = '<option value="">Нет общих свободных слотов у выбранных мастеров</option>';
    } else {
        sel.innerHTML = list.map(t => `<option value="${t}">${t}</option>`).join('');
        if (preferredTime) sel.value = preferredTime;
    }
}

async function submitBookingModal() {
    const id = document.getElementById('modalBookingId').value;
    const date = document.getElementById('modalDate').value;
    const time = document.getElementById('modalTime').value;
    const masterIds = getSelectedBookingMasterIds();

    if (!masterIds.length) {
        return alert('Выберите хотя бы одного мастера!');
    }

    const payload = {
        master_ids: masterIds,
        master_id: masterIds[0],
        service_id: document.getElementById('modalService').value,
        date,
        time,
        appointment_datetime: `${date} ${time}`,
        client_name: document.getElementById('modalClientName').value.trim(),
        client_phone: document.getElementById('modalClientPhone').value.trim(),
        client_username: document.getElementById('modalClientUsername').value.trim(),
        comment: document.getElementById('modalComment').value.trim(),
        status: document.getElementById('modalStatus').value
    };
    if (!time || !payload.client_name || !payload.client_phone) {
        return alert('Заполните имя, телефон и время');
    }

    if (id) {
        await apiFetch(`/api/appointments/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
        await apiFetch('/api/appointments', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeBookingModal();
    await Promise.all([loadAppointments(), onGanttDateChange()]);
}

async function quickStatus(id, status) {
    await apiFetch(`/api/appointments/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status })
    });
    await Promise.all([loadAppointments(), onGanttDateChange()]);
}

// ==================== КАССА: РАСЧЕТ КЛИЕНТА ====================
function openPaymentModal(app) {
    currentPayingApp = app;
    selectedPayMethod = 'cash';
    document.getElementById('payPointsInput').value = 0;
    document.getElementById('payDiscountInput').value = 0;

    const bonusAvail = Number(app.client_bonus_balance || 0);
    const mNames = (app.masters_names && app.masters_names.length) ? app.masters_names.join(' + ') : app.master_name;
    document.getElementById('payInfoSummary').innerHTML = `
        <div><b>Запись #${app.id}:</b> ${app.client_name} ${app.client_username ? '(@'+app.client_username+')' : ''} (${app.client_phone})</div>
        <div><b>Услуга:</b> ${app.service_name} — <b>${app.service_price} ₽</b></div>
        <div><b>Мастера:</b> ${mNames}</div>
        <div class="text-purple-700 font-bold">🎁 Доступно баллов у клиента: ${bonusAvail} шт. (1 балл = 1 ₽)</div>
    `;
    selectPayMethod('cash');
    document.getElementById('paymentModal').classList.remove('hidden');
}

function closePaymentModal() {
    document.getElementById('paymentModal').classList.add('hidden');
}

function selectPayMethod(method) {
    selectedPayMethod = method;
    document.getElementById('payBtn-cash').className = method === 'cash'
        ? 'py-2.5 px-3 rounded-xl border text-xs font-bold bg-purple-600 text-white border-purple-600'
        : 'py-2.5 px-3 rounded-xl border text-xs font-bold bg-white text-slate-700 border-slate-300';
    document.getElementById('payBtn-cashless').className = method === 'cashless'
        ? 'py-2.5 px-3 rounded-xl border text-xs font-bold bg-purple-600 text-white border-purple-600'
        : 'py-2.5 px-3 rounded-xl border text-xs font-bold bg-white text-slate-700 border-slate-300';
    recalcPaymentPreview();
}

function useMaxPoints() {
    if (!currentPayingApp) return;
    const fullPrice = Number(currentPayingApp.service_price || 0);
    const disc = Math.max(0, Number(document.getElementById('payDiscountInput').value || 0));
    const avail = Number(currentPayingApp.client_bonus_balance || 0);
    document.getElementById('payPointsInput').value = Math.max(0, Math.min(avail, fullPrice - disc));
    recalcPaymentPreview();
}

function recalcPaymentPreview() {
    if (!currentPayingApp) return;
    const fullPrice = Number(currentPayingApp.service_price || 0);
    const avail = Number(currentPayingApp.client_bonus_balance || 0);
    const disc = Math.max(0, Number(document.getElementById('payDiscountInput').value || 0));
    let pts = Math.max(0, Number(document.getElementById('payPointsInput').value || 0));
    pts = Math.min(pts, avail, Math.max(0, fullPrice - disc));

    const finalPaid = Math.max(0, fullPrice - disc - pts);
    const earnedPts = selectedPayMethod === 'cash' ? Math.round(fullPrice * 0.05) : 0;
    const masterShare = (fullPrice * 0.40).toFixed(2);
    const salonShare = (finalPaid - fullPrice * 0.40).toFixed(2);

    document.getElementById('payCalcPreview').innerHTML = `
        <div class="flex justify-between"><span>Стоимость по прайсу (100%):</span><b>${fullPrice} ₽</b></div>
        <div class="flex justify-between text-slate-600"><span>Списано баллов / скидка:</span><span>-${pts} балл. / -${disc} ₽</span></div>
        <div class="flex justify-between text-sm font-bold text-purple-900 border-t border-purple-200 pt-1">
            <span>К оплате клиентом:</span><span>${finalPaid} ₽ (${selectedPayMethod === 'cashless' ? 'Безнал' : 'Наличные'})</span>
        </div>
        <div class="flex justify-between text-emerald-700 font-semibold">
            <span>Начисление баллов (5% при наличных):</span><span>+${earnedPts} баллов</span>
        </div>
        <div class="border-t border-purple-200 pt-1 mt-1 text-[11px] text-slate-600 flex justify-between">
            <span>👩‍🎨 Мастерам (40% от прайса): <b>${masterShare} ₽</b></span>
            <span>🏢 Доля салона: <b>${salonShare} ₽</b></span>
        </div>
    `;
}

async function submitPaymentModal() {
    if (!currentPayingApp) return;
    const payload = {
        payment_method: selectedPayMethod,
        points_to_spend: Number(document.getElementById('payPointsInput').value || 0),
        discount_amount: Number(document.getElementById('payDiscountInput').value || 0)
    };
    await apiFetch(`/api/appointments/${currentPayingApp.id}/pay`, {
        method: 'POST',
        body: JSON.stringify(payload)
    });
    closePaymentModal();
    await Promise.all([loadAppointments(), onGanttDateChange()]);
}

// ==================== ТАБЛИЦА ЗАПИСЕЙ ====================
async function loadAppointments() {
    const date = document.getElementById('filterDate').value;
    const masterId = document.getElementById('filterMaster').value;
    const status = document.getElementById('filterStatus').value;
    const params = new URLSearchParams({ date, master_id: masterId, status });
    allAppointments = await apiFetch('/api/appointments?' + params.toString());
    renderAppointments();
}

function renderAppointments() {
    const q = document.getElementById('searchClient').value.toLowerCase().trim();
    const filtered = allAppointments.filter(a =>
        !q ||
        a.client_name.toLowerCase().includes(q) ||
        a.client_phone.toLowerCase().includes(q) ||
        (a.client_username || '').toLowerCase().includes(q) ||
        (a.comment || '').toLowerCase().includes(q)
    );
    const tbody = document.getElementById('appointmentsTableBody');
    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="py-6 text-center text-slate-400">Записей не найдено</td></tr>';
        return;
    }
    tbody.innerHTML = filtered.map(a => {
        let badge = '';
        let rowBg = 'hover:bg-slate-50';
        if (a.status === 'paid') {
            badge = `<span class="px-2.5 py-1 rounded-full text-xs font-bold bg-purple-100 text-purple-800">💳 Рассчитан (${a.payment_method === 'cashless' ? 'Безнал' : 'Нал'})</span>`;
            rowBg = 'bg-purple-50/40 hover:bg-purple-50';
        } else if (a.status === 'confirmed') {
            badge = '<span class="px-2 py-1 rounded-full text-xs bg-emerald-100 text-emerald-800">✅ Подтверждена</span>';
        } else if (a.status === 'pending') {
            badge = '<span class="px-2 py-1 rounded-full text-xs bg-amber-100 text-amber-800">⏳ Ждет звонка</span>';
        } else {
            badge = '<span class="px-2 py-1 rounded-full text-xs bg-rose-100 text-rose-800">❌ Отменена</span>';
        }

        const priceLine = a.status === 'paid'
            ? `<div class="text-xs font-bold text-purple-700">Оплачено: ${a.final_paid} ₽ (Мастерам: ${a.master_share} ₽)</div>`
            : `<div class="text-xs text-slate-500">Прайс: ${a.service_price} ₽</div>`;

        const mNames = a.masters_names && a.masters_names.length ? a.masters_names : [a.master_name];

        return `
        <tr class="${rowBg}">
            <td class="py-3 px-4 font-mono text-xs">#${a.id}</td>
            <td class="py-3 px-4 font-semibold">${a.appointment_datetime}</td>
            <td class="py-3 px-4">
                <div class="font-medium">${a.client_name} ${a.client_username ? `<span class="text-xs font-mono text-indigo-600">@${a.client_username}</span>` : ''}</div>
                <a href="tel:${a.client_phone}" class="text-xs text-pink-600 hover:underline">📞 ${a.client_phone}</a>
                <div class="text-[11px] text-purple-600 font-semibold">🎁 Бонусы: ${a.client_bonus_balance || 0}</div>
            </td>
            <td class="py-3 px-4">
                <div>${a.service_name} <span class="text-xs text-slate-400">(${a.service_duration} мин)</span></div>
                ${priceLine}
            </td>
            <td class="py-3 px-4">
                <div class="text-xs font-semibold">${mNames.join(' + ')}</div>
                ${mNames.length > 1 ? `<div class="text-[11px] text-indigo-600 font-medium">🤝 Совместно (${mNames.length} маст.)</div>` : ''}
            </td>
            <td class="py-3 px-4 text-xs text-slate-600 max-w-xs">${a.comment ? `💬 ${a.comment}` : '—'}</td>
            <td class="py-3 px-4">${badge}</td>
            <td class="py-3 px-4 text-right space-x-1">
                ${a.status !== 'paid' && a.status !== 'cancelled' ? `<button onclick='openPaymentModal(${JSON.stringify(a)})' class="text-xs bg-purple-600 hover:bg-purple-700 text-white font-semibold px-2.5 py-1 rounded-lg">💳 Расчёт</button>` : ''}
                <button onclick='openBookingModal(${JSON.stringify(a)})' class="text-xs bg-slate-100 hover:bg-slate-200 px-2.5 py-1 rounded-lg">✏️</button>
                ${a.status === 'pending' ? `<button onclick="quickStatus(${a.id}, 'confirmed')" class="text-xs bg-emerald-600 text-white px-2.5 py-1 rounded-lg">✅</button>` : ''}
                ${a.status !== 'cancelled' && a.status !== 'paid' ? `<button onclick="quickStatus(${a.id}, 'cancelled')" class="text-xs bg-rose-100 text-rose-700 px-2.5 py-1 rounded-lg">❌</button>` : ''}
            </td>
        </tr>`;
    }).join('');
}

// ==================== КЛИЕНТЫ, ИСТОРИЯ, БОНУСЫ ВРУЧНУЮ И СВЯЗЬ В ЛЮБОЕ ВРЕМЯ ====================
function changeClientsSort(sortBy) {
    currentClientsSort = sortBy;
    ['last_visit', 'visits', 'total_spent'].forEach(s => {
        document.getElementById('csort-' + s).className = s === sortBy
            ? 'px-3 py-1.5 rounded-xl text-xs font-semibold bg-pink-600 text-white'
            : 'px-3 py-1.5 rounded-xl text-xs font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200';
    });
    loadClients();
}

async function loadClients() {
    allClients = await apiFetch('/api/clients?sort_by=' + currentClientsSort);
    renderClientsTable();
}

function renderClientsTable() {
    const q = (document.getElementById('clientSearchInput').value || '').toLowerCase().trim();
    const list = allClients.filter(c =>
        !q ||
        c.client_name.toLowerCase().includes(q) ||
        (c.client_phone || '').toLowerCase().includes(q) ||
        (c.username || '').toLowerCase().includes(q)
    );
    const tbody = document.getElementById('clientsTableBody');
    if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="py-6 text-center text-slate-400">Клиентов пока нет</td></tr>';
        return;
    }

    tbody.innerHTML = list.map(c => {
        const contactKey = c.username ? '@' + c.username : (c.client_phone || String(c.tg_id || ''));
        return `
        <tr class="hover:bg-slate-50 ${c.is_blacklisted ? 'bg-rose-50/50' : ''}">
            <td class="py-3 px-4">
                <div class="font-bold text-slate-900">${c.client_name} ${c.is_blacklisted ? '<span class="text-xs text-rose-600 font-bold">🚫 ЧС</span>' : ''}</div>
                <div class="text-xs text-slate-500">${c.client_phone || '—'}</div>
            </td>
            <td class="py-3 px-4 font-mono text-xs">${c.username ? '@' + c.username : '—'}</td>
            <td class="py-3 px-4 font-semibold">${c.visits_count} виз. <span class="text-xs text-slate-400">(всего: ${c.total_appointments})</span></td>
            <td class="py-3 px-4 font-bold text-emerald-700">${c.total_spent} ₽</td>
            <td class="py-3 px-4 font-semibold">${c.avg_check} ₽</td>
            <td class="py-3 px-4 text-xs text-slate-600 max-w-xs">${c.masters}</td>
            <td class="py-3 px-4 text-xs font-mono">${c.last_visit || '—'}</td>
            <td class="py-3 px-4 font-bold text-purple-700">🎁 ${c.bonus_points}</td>
            <td class="py-3 px-4 text-right space-x-1">
                <button onclick='openClientHistoryModal(${JSON.stringify(c)})' class="text-xs bg-slate-100 hover:bg-slate-200 px-2 py-1 rounded-lg font-medium">📜 История</button>
                <button onclick='openBonusAdjustModal(${JSON.stringify(c)})' class="text-xs bg-purple-100 hover:bg-purple-200 text-purple-800 px-2 py-1 rounded-lg font-semibold">🎁 +/-</button>
                <button onclick="openContactClientModal('${contactKey.replace(/'/g, "\\'")}')" class="text-xs bg-indigo-50 hover:bg-indigo-100 text-indigo-700 px-2 py-1 rounded-lg font-semibold">✉️ Написать</button>
                ${c.username && !c.is_blacklisted ? `<button onclick="quickBanClient('${c.username}', ${c.tg_id || 0})" class="text-xs bg-rose-50 hover:bg-rose-100 text-rose-700 px-2 py-1 rounded-lg">🚫 ЧС</button>` : ''}
            </td>
        </tr>`;
    }).join('');
}

function openBonusAdjustModal(client) {
    currentBonusClient = client;
    document.getElementById('bonusModalClientInfo').innerHTML = `
        <div><b>Клиент:</b> ${client.client_name} ${client.username ? '(@'+client.username+')' : ''}</div>
        <div><b>Телефон:</b> ${client.client_phone || '—'}</div>
        <div class="text-purple-700 font-bold mt-1">Текущий баланс: ${client.bonus_points} баллов</div>
    `;
    document.getElementById('bonusDeltaInput').value = '';
    document.getElementById('bonusReasonInput').value = '';
    document.getElementById('bonusAdjustModal').classList.remove('hidden');
}

async function submitBonusAdjustModal() {
    if (!currentBonusClient) return;
    const delta = Number(document.getElementById('bonusDeltaInput').value || 0);
    const reason = document.getElementById('bonusReasonInput').value.trim();
    if (!delta) return alert('Введите количество баллов со знаком + или -');

    await apiFetch('/api/clients/bonus', {
        method: 'POST',
        body: JSON.stringify({
            tg_id: currentBonusClient.tg_id,
            client_phone: currentBonusClient.client_phone,
            client_name: currentBonusClient.client_name,
            username: currentBonusClient.username,
            delta,
            reason
        })
    });
    document.getElementById('bonusAdjustModal').classList.add('hidden');
    await Promise.all([loadClients(), loadAppointments()]);
}

function openContactClientModal(prefillIdentifier = '') {
    document.getElementById('contactClientIdentifier').value = prefillIdentifier || '';
    document.getElementById('contactClientText').value = '';
    document.getElementById('contactClientModal').classList.remove('hidden');
}

async function submitContactClientModal() {
    const identifier = document.getElementById('contactClientIdentifier').value.trim();
    const text = document.getElementById('contactClientText').value.trim();
    if (!identifier || !text) return alert('Укажите клиента и текст сообщения');

    const res = await apiFetch('/api/clients/contact', {
        method: 'POST',
        body: JSON.stringify({ identifier, text })
    });
    document.getElementById('contactClientModal').classList.add('hidden');
    switchTab('tickets');
    if (res.ticket) {
        await openTicketChat(res.ticket);
    }
}

function openClientHistoryModal(c) {
    document.getElementById('clientHistoryTitle').textContent = `👤 История клиента: ${c.client_name} (${c.client_phone || 'без телефона'})`;
    document.getElementById('clientHistoryStats').innerHTML = `
        <div class="bg-slate-50 p-3 rounded-xl border border-slate-200">
            <div class="text-[11px] text-slate-500">Посещаемость</div>
            <div class="text-base font-bold">${c.visits_count} визитов</div>
        </div>
        <div class="bg-emerald-50 p-3 rounded-xl border border-emerald-200">
            <div class="text-[11px] text-emerald-700">Общая сумма</div>
            <div class="text-base font-bold text-emerald-800">${c.total_spent} ₽</div>
        </div>
        <div class="bg-indigo-50 p-3 rounded-xl border border-indigo-200">
            <div class="text-[11px] text-indigo-700">Средний чек</div>
            <div class="text-base font-bold text-indigo-800">${c.avg_check} ₽</div>
        </div>
        <div class="bg-purple-50 p-3 rounded-xl border border-purple-200">
            <div class="text-[11px] text-purple-700">Бонусный баланс</div>
            <div class="text-base font-bold text-purple-800">🎁 ${c.bonus_points} баллов</div>
        </div>
    `;
    document.getElementById('clientHistoryTableBody').innerHTML = (c.history || []).map(h => `
        <tr>
            <td class="py-2 px-3 font-mono">#${h.id}</td>
            <td class="py-2 px-3 font-semibold">${h.datetime}</td>
            <td class="py-2 px-3">${h.service_name}</td>
            <td class="py-2 px-3">${h.master_name}</td>
            <td class="py-2 px-3">${h.status}</td>
            <td class="py-2 px-3">
                ${h.status === 'paid'
                    ? `<b>${h.final_paid} ₽</b> (${h.payment_method === 'cashless' ? 'Безнал' : 'Нал'}, -${h.points_spent} балл., +${h.points_earned} балл.)`
                    : `${h.full_price} ₽`}
            </td>
            <td class="py-2 px-3 text-slate-500">${h.comment || '—'}</td>
        </tr>
    `).join('') || '<tr><td colspan="7" class="py-4 text-center text-slate-400">Записей нет</td></tr>';
    document.getElementById('clientHistoryModal').classList.remove('hidden');
}

async function quickBanClient(username, tgId) {
    const reason = prompt(`Укажите причину блокировки @${username}:`, 'Нарушение правил записи');
    if (reason === null) return;
    await apiFetch('/api/blacklist', {
        method: 'POST',
        body: JSON.stringify({ username, tg_id: tgId, reason })
    });
    await loadClients();
}

// ==================== ЧЕРНЫЙ СПИСОК ПО @USERNAME ====================
async function loadBlacklist() {
    const list = await apiFetch('/api/blacklist');
    const tbody = document.getElementById('blacklistTableBody');
    if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center text-slate-400">Чёрный список пуст</td></tr>';
        return;
    }
    tbody.innerHTML = list.map(b => `
        <tr class="hover:bg-slate-50">
            <td class="py-3 px-4 font-bold text-rose-700">@${b.username}</td>
            <td class="py-3 px-4 font-mono text-xs">${b.tg_id || '—'}</td>
            <td class="py-3 px-4 text-xs">${b.reason || '—'}</td>
            <td class="py-3 px-4 text-xs text-slate-400">${b.created_at}</td>
            <td class="py-3 px-4 text-right">
                <button onclick="removeBlacklistEntry('${b.username}')" class="text-xs bg-emerald-50 hover:bg-emerald-100 text-emerald-700 px-3 py-1 rounded-lg font-semibold">
                    Разблокировать
                </button>
            </td>
        </tr>
    `).join('');
}

async function addBlacklistEntry() {
    const username = document.getElementById('blUsername').value.trim();
    const reason = document.getElementById('blReason').value.trim();
    if (!username) return alert('Введите @username');
    await apiFetch('/api/blacklist', {
        method: 'POST',
        body: JSON.stringify({ username, reason })
    });
    document.getElementById('blUsername').value = '';
    document.getElementById('blReason').value = '';
    await loadBlacklist();
}

async function removeBlacklistEntry(username) {
    await apiFetch(`/api/blacklist/${encodeURIComponent(username)}`, { method: 'DELETE' });
    await loadBlacklist();
}

// ==================== НЕДЕЛЬНЫЙ ФИНАНСОВЫЙ ОТЧЕТ (40% МАСТЕР / 60% САЛОН) ====================
function setCurrentWeekReport() {
    const now = new Date();
    const day = now.getDay() || 7;
    const monday = new Date(now);
    monday.setDate(now.getDate() - day + 1);
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);

    document.getElementById('repStartDate').value = `${monday.getFullYear()}-${String(monday.getMonth()+1).padStart(2,'0')}-${String(monday.getDate()).padStart(2,'0')}`;
    document.getElementById('repEndDate').value = `${sunday.getFullYear()}-${String(sunday.getMonth()+1).padStart(2,'0')}-${String(sunday.getDate()).padStart(2,'0')}`;
    loadWeeklyReport();
}

function shiftReportWeek(days) {
    const sInp = document.getElementById('repStartDate');
    const eInp = document.getElementById('repEndDate');
    const s = new Date(sInp.value + 'T00:00:00');
    const e = new Date(eInp.value + 'T00:00:00');
    s.setDate(s.getDate() + days);
    e.setDate(e.getDate() + days);
    sInp.value = `${s.getFullYear()}-${String(s.getMonth()+1).padStart(2,'0')}-${String(s.getDate()).padStart(2,'0')}`;
    eInp.value = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
    loadWeeklyReport();
}

async function loadWeeklyReport() {
    if (!document.getElementById('repStartDate').value) {
        return setCurrentWeekReport();
    }
    const start_date = document.getElementById('repStartDate').value;
    const end_date = document.getElementById('repEndDate').value;
    const rep = await apiFetch(`/api/reports/weekly?start_date=${start_date}&end_date=${end_date}`);

    document.getElementById('reportSummaryCards').innerHTML = `
        <div class="bg-white p-4 rounded-2xl border border-slate-200">
            <div class="text-xs text-slate-500">Оплаченных визитов</div>
            <div class="text-lg font-bold mt-1">${rep.count}</div>
        </div>
        <div class="bg-white p-4 rounded-2xl border border-slate-200">
            <div class="text-xs text-slate-500">По прайсу (100%)</div>
            <div class="text-lg font-bold mt-1">${rep.total_full_price} ₽</div>
        </div>
        <div class="bg-white p-4 rounded-2xl border border-slate-200">
            <div class="text-xs text-slate-500">Фактически в кассе</div>
            <div class="text-lg font-bold text-purple-700 mt-1">${rep.total_final_paid} ₽</div>
            <div class="text-[11px] text-slate-400">Нал: ${rep.total_cash} ₽ | Безнал: ${rep.total_cashless} ₽</div>
        </div>
        <div class="bg-white p-4 rounded-2xl border border-slate-200">
            <div class="text-xs text-slate-500">Списано баллов / скидок</div>
            <div class="text-lg font-bold text-amber-600 mt-1">${rep.total_points_spent + rep.total_discounts} ₽</div>
            <div class="text-[11px] text-slate-400">Баллы: ${rep.total_points_spent} | Скидки: ${rep.total_discounts} ₽</div>
        </div>
        <div class="bg-emerald-50 p-4 rounded-2xl border border-emerald-200">
            <div class="text-xs text-emerald-800 font-semibold">👩‍🎨 Мастерам (40% от прайса)</div>
            <div class="text-lg font-bold text-emerald-900 mt-1">${rep.total_master_share} ₽</div>
        </div>
        <div class="bg-pink-50 p-4 rounded-2xl border border-pink-200">
            <div class="text-xs text-pink-800 font-semibold">🏢 Доля салона (остаток)</div>
            <div class="text-lg font-bold text-pink-900 mt-1">${rep.total_salon_share} ₽</div>
        </div>
    `;

    document.getElementById('reportMastersBody').innerHTML = (rep.masters_breakdown || []).map(m => `
        <tr class="hover:bg-slate-50">
            <td class="py-3 px-4 font-bold">👩‍🎨 ${m.master_name}</td>
            <td class="py-3 px-4">${m.services_count}</td>
            <td class="py-3 px-4 font-semibold">${m.full_services_sum} ₽</td>
            <td class="py-3 px-4 font-bold text-emerald-700">${m.master_40_share} ₽</td>
        </tr>
    `).join('') || '<tr><td colspan="4" class="py-5 text-center text-slate-400">За выбранную неделю нет оплаченных записей</td></tr>';

    document.getElementById('reportAppointmentsBody').innerHTML = (rep.appointments || []).map(a => `
        <tr class="hover:bg-slate-50">
            <td class="py-2.5 px-4 font-mono">${a.appointment_datetime}</td>
            <td class="py-2.5 px-4 font-medium">${a.client_name}</td>
            <td class="py-2.5 px-4">${a.service_name}</td>
            <td class="py-2.5 px-4">${(a.masters_names || [a.master_name]).join(' + ')}</td>
            <td class="py-2.5 px-4">${a.payment_method === 'cashless' ? '💳 Безнал' : '💵 Нал'}</td>
            <td class="py-2.5 px-4 font-semibold">${a.full_price} ₽</td>
            <td class="py-2.5 px-4 text-amber-700">-${(a.points_spent || 0) + (a.discount_amount || 0)} ₽</td>
            <td class="py-2.5 px-4 font-bold text-purple-700">${a.final_paid} ₽</td>
            <td class="py-2.5 px-4 font-bold text-emerald-700">${a.master_share} ₽</td>
            <td class="py-2.5 px-4 font-bold text-pink-700">${a.salon_share} ₽</td>
        </tr>
    `).join('') || '<tr><td colspan="10" class="py-5 text-center text-slate-400">Нет данных</td></tr>';
}

// ==================== КАТЕГОРИИ, УСЛУГИ, МАСТЕРА, ТИКЕТЫ, АДМИНЫ ====================
async function loadCategoriesAndServices() {
    [allCategories, allServices] = await Promise.all([
        apiFetch('/api/categories'),
        apiFetch('/api/services')
    ]);

    document.getElementById('masterCategoriesCheckboxes').innerHTML = allCategories.map(c => `
        <label class="flex items-center gap-1.5 text-xs">
            <input type="checkbox" value="${c.name}" class="master-cat-cb" checked> ${c.name}
        </label>
    `).join('');

    document.getElementById('srvCategory').innerHTML = allCategories.map(c =>
        `<option value="${c.name}">${c.name}</option>`
    ).join('');

    document.getElementById('categoriesList').innerHTML = allCategories.map(c => `
        <div class="flex items-center gap-1.5 bg-slate-100 px-3 py-1.5 rounded-xl text-xs">
            <span class="font-semibold">${c.name}</span>
            <button onclick="renameCategory(${c.id}, '${c.name.replace(/'/g, "\\'")}')" class="text-slate-500 hover:text-pink-600">✏️</button>
            <button onclick="deleteCategory(${c.id})" class="text-slate-400 hover:text-rose-600">✕</button>
        </div>
    `).join('');

    document.getElementById('servicesTableBody').innerHTML = allServices.map(s => `
        <tr class="hover:bg-slate-50">
            <td class="py-2.5 px-4 font-medium text-pink-600">${s.category}</td>
            <td class="py-2.5 px-4">${s.name}</td>
            <td class="py-2.5 px-4 font-semibold">${s.price} ₽</td>
            <td class="py-2.5 px-4 text-slate-500">${s.duration_minutes} мин</td>
            <td class="py-2.5 px-4 text-right space-x-2">
                <button onclick='editService(${JSON.stringify(s)})' class="text-xs text-slate-600 hover:text-pink-600">✏️ Изм.</button>
                <button onclick="deleteService(${s.id})" class="text-xs text-rose-600 hover:underline">Удалить</button>
            </td>
        </tr>
    `).join('');
}

async function addCategory() {
    const name = document.getElementById('newCatName').value.trim();
    if (!name) return;
    await apiFetch('/api/categories', { method: 'POST', body: JSON.stringify({ name }) });
    document.getElementById('newCatName').value = '';
    await loadCategoriesAndServices();
}

async function renameCategory(id, oldName) {
    const name = prompt('Новое название категории:', oldName);
    if (!name || name.trim() === oldName) return;
    await apiFetch(`/api/categories/${id}`, { method: 'PUT', body: JSON.stringify({ name: name.trim() }) });
    await Promise.all([loadCategoriesAndServices(), loadMasters()]);
}

async function deleteCategory(id) {
    if (!confirm('Удалить категорию и её услуги?')) return;
    await apiFetch(`/api/categories/${id}`, { method: 'DELETE' });
    await loadCategoriesAndServices();
}

function editService(s) {
    document.getElementById('editSrvId').value = s.id;
    document.getElementById('srvFormTitle').textContent = `✏️ Изменить услугу #${s.id}`;
    document.getElementById('srvCategory').value = s.category;
    document.getElementById('srvName').value = s.name;
    document.getElementById('srvPrice').value = s.price;
    document.getElementById('srvDuration').value = s.duration_minutes;
}

function resetServiceForm() {
    document.getElementById('editSrvId').value = '';
    document.getElementById('srvFormTitle').textContent = '➕ Добавить / Изменить услугу';
    document.getElementById('srvName').value = '';
    document.getElementById('srvPrice').value = 2000;
    document.getElementById('srvDuration').value = 60;
}

async function saveServiceForm() {
    const id = document.getElementById('editSrvId').value;
    const payload = {
        category: document.getElementById('srvCategory').value,
        name: document.getElementById('srvName').value.trim(),
        price: document.getElementById('srvPrice').value,
        duration_minutes: document.getElementById('srvDuration').value
    };
    if (!payload.name) return alert('Укажите название услуги');
    if (id) await apiFetch(`/api/services/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
    else await apiFetch('/api/services', { method: 'POST', body: JSON.stringify(payload) });
    resetServiceForm();
    await loadCategoriesAndServices();
}

async function deleteService(id) {
    if (!confirm('Удалить услугу?')) return;
    await apiFetch(`/api/services/${id}`, { method: 'DELETE' });
    await loadCategoriesAndServices();
}

async function loadMasters() {
    [allMasters, allTimeOffs] = await Promise.all([
        apiFetch('/api/masters'),
        apiFetch('/api/time-offs')
    ]);

    document.getElementById('filterMaster').innerHTML = '<option value="">Все мастера</option>' +
        allMasters.map(m => `<option value="${m.id}">${m.name}</option>`).join('');

    document.getElementById('timeOffMaster').innerHTML = allMasters.map(m =>
        `<option value="${m.id}">${m.name}</option>`
    ).join('');

    document.getElementById('mastersTableBody').innerHTML = allMasters.map(m => `
        <tr class="hover:bg-slate-50">
            <td class="py-3 px-4">
                <div class="font-semibold">${m.name}</div>
                <div class="text-xs text-slate-400">${m.specialization}</div>
            </td>
            <td class="py-3 px-4 font-mono text-xs">${m.tg_id ? `ID: ${m.tg_id} ✅` : '—'}</td>
            <td class="py-3 px-4 text-xs">${(m.categories || []).join(', ')}</td>
            <td class="py-3 px-4">${m.is_active ? 'Активен' : 'Скрыт'}</td>
            <td class="py-3 px-4 text-right space-x-2">
                <button onclick='editMaster(${JSON.stringify(m)})' class="text-xs text-slate-700 hover:text-pink-600">✏️ Изм.</button>
                <button onclick="deleteMaster(${m.id})" class="text-xs text-rose-600 hover:underline">Удалить</button>
            </td>
        </tr>
    `).join('');

    document.getElementById('timeOffsTableBody').innerHTML = allTimeOffs.map(o => `
        <tr class="hover:bg-slate-50">
            <td class="py-2.5 px-4 font-medium">${o.master_name}</td>
            <td class="py-2.5 px-4 text-xs">${o.type === 'vacation' ? '✈️ Отпуск' : '🕒 Отгул'}</td>
            <td class="py-2.5 px-4 font-mono text-xs">${o.start_datetime} — ${o.end_datetime}</td>
            <td class="py-2.5 px-4 text-xs text-slate-500">${o.reason || '—'}</td>
            <td class="py-2.5 px-4 text-right space-x-2">
                <button onclick='editTimeOff(${JSON.stringify(o)})' class="text-xs text-slate-700 hover:text-pink-600">✏️ Изм.</button>
                <button onclick="deleteTimeOff(${o.id})" class="text-xs text-rose-600 hover:underline">Удалить</button>
            </td>
        </tr>
    `).join('');
}

function editMaster(m) {
    document.getElementById('editMasterId').value = m.id;
    document.getElementById('masterName').value = m.name;
    document.getElementById('masterSpec').value = m.specialization;
    document.getElementById('masterTgId').value = m.tg_id || '';
    document.getElementById('masterActive').checked = !!m.is_active;
    document.querySelectorAll('.master-cat-cb').forEach(cb => {
        cb.checked = (m.categories || []).includes(cb.value);
    });
}

function resetMasterForm() {
    document.getElementById('editMasterId').value = '';
    document.getElementById('masterName').value = '';
    document.getElementById('masterSpec').value = '';
    document.getElementById('masterTgId').value = '';
    document.getElementById('masterActive').checked = true;
}

async function saveMasterForm() {
    const id = document.getElementById('editMasterId').value;
    const payload = {
        name: document.getElementById('masterName').value.trim(),
        specialization: document.getElementById('masterSpec').value.trim(),
        tg_id: document.getElementById('masterTgId').value.trim(),
        is_active: document.getElementById('masterActive').checked ? 1 : 0,
        categories: Array.from(document.querySelectorAll('.master-cat-cb:checked')).map(cb => cb.value)
    };
    if (!payload.name) return alert('Укажите имя мастера');
    if (id) await apiFetch(`/api/masters/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
    else await apiFetch('/api/masters', { method: 'POST', body: JSON.stringify(payload) });
    resetMasterForm();
    await Promise.all([loadMasters(), onGanttDateChange()]);
}

async function deleteMaster(id) {
    if (!confirm('Удалить мастера?')) return;
    await apiFetch(`/api/masters/${id}`, { method: 'DELETE' });
    await Promise.all([loadMasters(), onGanttDateChange()]);
}

function editTimeOff(o) {
    document.getElementById('editTimeOffId').value = o.id;
    document.getElementById('timeOffMaster').value = o.master_id;
    document.getElementById('timeOffType').value = o.type;
    document.getElementById('timeOffStart').value = o.start_datetime;
    document.getElementById('timeOffEnd').value = o.end_datetime;
    document.getElementById('timeOffReason').value = o.reason || '';
}

function resetTimeOffForm() {
    document.getElementById('editTimeOffId').value = '';
    document.getElementById('timeOffStart').value = `${getTodayStr()} 10:00`;
    document.getElementById('timeOffEnd').value = `${getTodayStr()} 20:00`;
    document.getElementById('timeOffReason').value = '';
}

async function saveTimeOffForm() {
    const id = document.getElementById('editTimeOffId').value;
    const payload = {
        master_id: document.getElementById('timeOffMaster').value,
        type: document.getElementById('timeOffType').value,
        start_datetime: document.getElementById('timeOffStart').value.trim(),
        end_datetime: document.getElementById('timeOffEnd').value.trim(),
        reason: document.getElementById('timeOffReason').value.trim()
    };
    if (id) await apiFetch(`/api/time-offs/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
    else await apiFetch('/api/time-offs', { method: 'POST', body: JSON.stringify(payload) });
    resetTimeOffForm();
    await Promise.all([loadMasters(), onGanttDateChange()]);
}

async function deleteTimeOff(id) {
    if (!confirm('Удалить отгул/отпуск?')) return;
    await apiFetch(`/api/time-offs/${id}`, { method: 'DELETE' });
    await Promise.all([loadMasters(), onGanttDateChange()]);
}

// ==================== ТИКЕТЫ, РАССЫЛКА, АДМИНЫ, ЛОГИ ====================
async function loadTickets() {
    const tickets = await apiFetch('/api/tickets');
    document.getElementById('ticketsList').innerHTML = tickets.map(t => `
        <div onclick='openTicketChat(${JSON.stringify(t)})'
             class="p-3 rounded-xl border ${activeTicketId === t.id ? 'border-pink-500 bg-pink-50/40' : 'border-slate-200 hover:bg-slate-50'} cursor-pointer">
            <div class="flex justify-between items-center">
                <span class="font-bold text-xs">#${t.id} • ${t.client_name}</span>
                <span class="text-[10px] font-bold">${t.status}</span>
            </div>
            <div class="text-xs text-slate-500">@${t.client_username || 'нет'}</div>
        </div>
    `).join('') || '<div class="text-xs text-slate-400 text-center py-6">Диалогов пока нет</div>';
}

async function openTicketChat(t) {
    activeTicketId = t.id;
    await loadTickets();
    document.getElementById('ticketChatHeader').innerHTML = `
        <div class="font-bold text-sm">Тикет #${t.id} — ${t.client_name} ${t.client_username ? '(@'+t.client_username+')' : ''}</div>
        <div class="flex gap-2">
            ${t.status === 'open' ? `<button onclick="takeTicketWeb(${t.id})" class="text-xs bg-emerald-600 text-white px-3 py-1 rounded-lg">✋ Взять</button>` : ''}
            ${t.status !== 'closed' ? `<button onclick="closeTicketWeb(${t.id})" class="text-xs bg-rose-100 text-rose-700 px-3 py-1 rounded-lg">🔒 Закрыть</button>` : ''}
        </div>
    `;
    document.getElementById('ticketInputArea').classList.toggle('hidden', t.status === 'closed');
    await loadTicketMessages(t.id);
}

async function loadTicketMessages(ticketId) {
    const msgs = await apiFetch(`/api/tickets/${ticketId}/messages`);
    const box = document.getElementById('ticketMessagesBox');
    box.innerHTML = msgs.map(m => `
        <div class="flex ${m.sender_role === 'admin' ? 'justify-end' : 'justify-start'}">
            <div class="max-w-md px-3.5 py-2 rounded-2xl text-xs ${m.sender_role === 'admin' ? 'bg-pink-600 text-white' : 'bg-slate-100 text-slate-800'}">
                <div class="font-bold opacity-80">${m.sender_name}</div>
                <div>${m.text}</div>
            </div>
        </div>
    `).join('');
    box.scrollTop = box.scrollHeight;
}

async function takeTicketWeb(id) {
    await apiFetch(`/api/tickets/${id}/take`, { method: 'POST', body: JSON.stringify({ admin_name: getAdminName() }) });
    const tickets = await apiFetch('/api/tickets');
    const updated = tickets.find(x => x.id === id);
    if (updated) await openTicketChat(updated);
}

async function sendTicketReply() {
    if (!activeTicketId) return;
    const inp = document.getElementById('ticketReplyInput');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    await apiFetch(`/api/tickets/${activeTicketId}/messages`, {
        method: 'POST',
        body: JSON.stringify({ text, admin_name: getAdminName() })
    });
    await loadTicketMessages(activeTicketId);
}

async function closeTicketWeb(id) {
    await apiFetch(`/api/tickets/${id}/close`, { method: 'POST' });
    activeTicketId = null;
    document.getElementById('ticketInputArea').classList.add('hidden');
    await loadTickets();
}

async function sendBroadcast() {
    const text = document.getElementById('bcText').value.trim();
    const mediaType = document.getElementById('bcMediaType').value;
    const fileInput = document.getElementById('bcFile');
    if (!text && !fileInput.files.length) return alert('Введите текст или файл');

    const fd = new FormData();
    fd.append('text', text);
    fd.append('media_type', mediaType);
    if (fileInput.files.length) fd.append('file', fileInput.files[0]);

    const res = await apiFetch('/api/broadcast', { method: 'POST', body: fd });
    alert(`Рассылка завершена! Доставлено: ${res.sent} из ${res.total}`);
}

async function loadAdmins() {
    const admins = await apiFetch('/api/admins');
    document.getElementById('adminsList').innerHTML = admins.map(a => `
        <div class="py-2.5 flex justify-between items-center">
            <div><div class="font-medium">${a.name}</div><div class="text-xs text-slate-400">ID: ${a.tg_id}</div></div>
            <button onclick="deleteAdmin(${a.tg_id})" class="text-xs text-rose-600">Удалить</button>
        </div>
    `).join('');
}

async function addAdmin() {
    const tg_id = document.getElementById('newAdminTgId').value.trim();
    const name = document.getElementById('newAdminName').value.trim();
    if (!tg_id || !name) return;
    await apiFetch('/api/admins', { method: 'POST', body: JSON.stringify({ tg_id, name }) });
    await loadAdmins();
    await loadLogs();
}

async function deleteAdmin(tgId) {
    await apiFetch(`/api/admins/${tgId}`, { method: 'DELETE' });
    await loadAdmins();
}

async function loadLogs() {
    const logs = await apiFetch('/api/logs');
    document.getElementById('logsTableBody').innerHTML = logs.map(l => `
        <tr>
            <td class="py-2 px-3 font-mono text-slate-400">${l.created_at}</td>
            <td class="py-2 px-3 font-semibold">${l.admin_name}</td>
            <td class="py-2 px-3 text-pink-600 font-medium">${l.action}</td>
            <td class="py-2 px-3 text-slate-600">${l.details}</td>
        </tr>
    `).join('');
}

async function initApp() {
    document.getElementById('currentAdminBadge').textContent = '👤 ' + getAdminName();
    document.getElementById('ganttDate').value = getTodayStr();
    resetTimeOffForm();
    await Promise.all([loadCategoriesAndServices(), loadMasters(), loadAppointments()]);
    await onGanttDateChange();
}

window.addEventListener('DOMContentLoaded', initApp);
</script>
</body>
</html>
"""


async def index_page(request: web.Request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def start_web_admin(
        bot: Optional[Bot], reminder_func=None, host: str = "127.0.0.1", port: int = 8080
):
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app["bot"] = bot
    app["reminder_func"] = reminder_func

    # Главная страница CRM с вертикальной диаграммой Ганта
    app.router.add_get("/", index_page)

    # REST API Записей и Кассы
    app.router.add_get("/api/appointments", api_get_appointments)
    app.router.add_post("/api/appointments", api_create_appointment)
    app.router.add_patch("/api/appointments/{id}", api_update_appointment)
    app.router.add_post("/api/appointments/{id}/pay", api_pay_appointment)
    app.router.add_get("/api/slots", api_get_slots)

    # REST API Клиентов, Ручного управления бонусами, Связи с клиентом, ЧС и Недельного отчета
    app.router.add_get("/api/clients", api_get_clients)
    app.router.add_post("/api/clients/bonus", api_adjust_client_bonus)
    app.router.add_post("/api/clients/contact", api_contact_client_anytime)
    app.router.add_get("/api/blacklist", api_get_blacklist)
    app.router.add_post("/api/blacklist", api_add_blacklist)
    app.router.add_delete("/api/blacklist/{target}", api_remove_blacklist)
    app.router.add_get("/api/reports/weekly", api_get_weekly_report)

    # REST API Категорий, Услуг, Мастеров, Отгулов, Тикетов и Рассылки
    app.router.add_get("/api/categories", api_get_categories)
    app.router.add_post("/api/categories", api_add_category)
    app.router.add_put("/api/categories/{id}", api_update_category)
    app.router.add_delete("/api/categories/{id}", api_delete_category)

    app.router.add_get("/api/services", api_get_services)
    app.router.add_post("/api/services", api_add_service)
    app.router.add_put("/api/services/{id}", api_update_service)
    app.router.add_delete("/api/services/{id}", api_delete_service)

    app.router.add_get("/api/masters", api_get_masters)
    app.router.add_post("/api/masters", api_add_master)
    app.router.add_put("/api/masters/{id}", api_update_master)
    app.router.add_delete("/api/masters/{id}", api_delete_master)

    app.router.add_get("/api/time-offs", api_get_time_offs)
    app.router.add_post("/api/time-offs", api_add_time_off)
    app.router.add_put("/api/time-offs/{id}", api_update_time_off)
    app.router.add_delete("/api/time-offs/{id}", api_delete_time_off)

    app.router.add_get("/api/admins", api_get_admins)
    app.router.add_post("/api/admins", api_add_admin)
    app.router.add_delete("/api/admins/{tg_id}", api_delete_admin)
    app.router.add_get("/api/logs", api_get_logs)

    app.router.add_get("/api/tickets", api_get_tickets)
    app.router.add_get("/api/tickets/{id}/messages", api_get_ticket_messages)
    app.router.add_post("/api/tickets/{id}/take", api_take_ticket)
    app.router.add_post("/api/tickets/{id}/messages", api_send_ticket_message)
    app.router.add_post("/api/tickets/{id}/close", api_close_ticket)
    app.router.add_post("/api/broadcast", api_broadcast)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info(f"🌐 CRM и Вертикальная диаграмма Ганта запущены: http://{host}:{port}")
    return runner