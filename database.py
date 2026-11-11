import aiosqlite
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Union

DB_PATH = "beauty_salon.db"

WORK_START_HOUR = 10
WORK_END_HOUR = 20
SLOT_STEP_MINUTES = 15

RU_MONTHS = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "мая",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}


def format_ru_date(date_str: str) -> str:
    """Форматирует дату YYYY-MM-DD в вид '1 янв', '15 сен'."""
    if not date_str:
        return ""
    clean = date_str.strip().split(" ")[0]
    try:
        dt = datetime.strptime(clean, "%Y-%m-%d")
        return f"{dt.day} {RU_MONTHS.get(dt.month, '')}"
    except Exception:
        return date_str


def format_ru_datetime(dt_str: str) -> str:
    """Форматирует дату и время YYYY-MM-DD HH:MM в вид '1 янв, 10:00'."""
    if not dt_str:
        return ""
    parts = dt_str.strip().split(" ")
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else ""
    try:
        dt = datetime.strptime(date_part, "%Y-%m-%d")
        d_formatted = f"{dt.day} {RU_MONTHS.get(dt.month, '')}"
        return f"{d_formatted}, {time_part}" if time_part else d_formatted
    except Exception:
        return dt_str


async def init_db(initial_admin_ids: Optional[List[int]] = None):
    """Инициализация всех таблиц БД, безопасная миграция и первичное заполнение."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                bonus_points INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            await db.execute("ALTER TABLE users ADD COLUMN bonus_points INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                tg_id INTEGER DEFAULT NULL,
                reason TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                tg_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                on_shift INTEGER DEFAULT 0,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            await db.execute("ALTER TABLE admins ADD COLUMN on_shift INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS masters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                specialization TEXT NOT NULL,
                tg_id INTEGER DEFAULT NULL,
                is_active INTEGER DEFAULT 1
            )
            """
        )
        try:
            await db.execute("ALTER TABLE masters ADD COLUMN tg_id INTEGER DEFAULT NULL")
        except Exception:
            pass

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 60
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS master_services (
                master_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                PRIMARY KEY (master_id, service_id),
                FOREIGN KEY (master_id) REFERENCES masters(id) ON DELETE CASCADE,
                FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS master_time_off (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'day_off',
                start_datetime TEXT NOT NULL,
                end_datetime TEXT NOT NULL,
                reason TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (master_id) REFERENCES masters(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_tg_id INTEGER NOT NULL DEFAULT 0,
                client_username TEXT DEFAULT '',
                client_name TEXT NOT NULL,
                client_phone TEXT NOT NULL,
                master_id INTEGER NOT NULL,
                second_master_id INTEGER DEFAULT NULL,
                service_id INTEGER NOT NULL,
                appointment_datetime TEXT NOT NULL,
                comment TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                reminded_24h INTEGER DEFAULT 0,
                payment_method TEXT DEFAULT NULL,
                full_price INTEGER DEFAULT 0,
                discount_amount INTEGER DEFAULT 0,
                points_spent INTEGER DEFAULT 0,
                points_earned INTEGER DEFAULT 0,
                final_paid INTEGER DEFAULT 0,
                master_share REAL DEFAULT 0,
                salon_share REAL DEFAULT 0,
                paid_at TEXT DEFAULT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (master_id) REFERENCES masters(id),
                FOREIGN KEY (second_master_id) REFERENCES masters(id),
                FOREIGN KEY (service_id) REFERENCES services(id)
            )
            """
        )
        migrations = [
            "ALTER TABLE appointments ADD COLUMN client_username TEXT DEFAULT ''",
            "ALTER TABLE appointments ADD COLUMN second_master_id INTEGER DEFAULT NULL",
            "ALTER TABLE appointments ADD COLUMN comment TEXT DEFAULT ''",
            "ALTER TABLE appointments ADD COLUMN payment_method TEXT DEFAULT NULL",
            "ALTER TABLE appointments ADD COLUMN full_price INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN discount_amount INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN points_spent INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN points_earned INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN final_paid INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN master_share REAL DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN salon_share REAL DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN paid_at TEXT DEFAULT NULL",
        ]
        for mig in migrations:
            try:
                await db.execute(mig)
            except Exception:
                pass

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS appointment_masters (
                appointment_id INTEGER NOT NULL,
                master_id INTEGER NOT NULL,
                PRIMARY KEY (appointment_id, master_id),
                FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
                FOREIGN KEY (master_id) REFERENCES masters(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO appointment_masters (appointment_id, master_id)
            SELECT id, master_id FROM appointments WHERE master_id IS NOT NULL
            """
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO appointment_masters (appointment_id, master_id)
            SELECT id, second_master_id FROM appointments WHERE second_master_id IS NOT NULL
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_name TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_tg_id INTEGER NOT NULL,
                client_username TEXT DEFAULT '',
                client_name TEXT DEFAULT '',
                admin_tg_id INTEGER DEFAULT NULL,
                admin_name TEXT DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
            )
            """
        )

        if initial_admin_ids:
            for adm_id in initial_admin_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO admins (tg_id, name, on_shift) VALUES (?, ?, 0)",
                    (adm_id, f"Администратор #{adm_id}"),
                )

        async with db.execute("SELECT COUNT(*) FROM services") as cursor:
            srv_count = (await cursor.fetchone())[0]

        if srv_count == 0:
            default_cats = [("Маникюр",), ("Педикюр",), ("Брови",), ("Стрижки",)]
            await db.executemany(
                "INSERT OR IGNORE INTO categories (name) VALUES (?)", default_cats
            )

            initial_services = [
                ("Маникюр", "Маникюр с покрытием гель-лак", 2200, 90),
                ("Маникюр", "Гигиенический маникюр", 1300, 60),
                ("Маникюр", "Наращивание ногтей", 3500, 120),
                ("Педикюр", "SMART-педикюр с покрытием", 2800, 90),
                ("Педикюр", "Экспресс-педикюр (пальчики)", 1900, 60),
                ("Брови", "Коррекция и окрашивание бровей", 1500, 60),
                ("Брови", "Ламинирование бровей + уход", 2200, 60),
                ("Стрижки", "Женская стрижка и укладка", 2500, 60),
                ("Стрижки", "Мужская стрижка", 1500, 60),
                ("Стрижки", "Стрижка кончиков", 1200, 45),
            ]
            await db.executemany(
                "INSERT INTO services (category, name, price, duration_minutes) VALUES (?, ?, ?, ?)",
                initial_services,
            )

            initial_masters = [
                ("Анна Смирнова", "Топ-мастер ногтевого сервиса (Маникюр, Педикюр)"),
                ("Елена Волкова", "Мастер маникюра и педикюра"),
                ("Мария Соколова", "Бровист-ламимейкер (Брови)"),
                ("Ольга Морозова", "Стилист-парикмахер (Стрижки)"),
            ]
            await db.executemany(
                "INSERT INTO masters (name, specialization) VALUES (?, ?)",
                initial_masters,
            )

            async with db.execute("SELECT id, category FROM services") as cur:
                services_rows = await cur.fetchall()

            links = []
            for s_id, cat in services_rows:
                if cat in ("Маникюр", "Педикюр"):
                    links.append((1, s_id))
                    links.append((2, s_id))
                elif cat == "Брови":
                    links.append((3, s_id))
                elif cat == "Стрижки":
                    links.append((4, s_id))

            await db.executemany(
                "INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?, ?)",
                links,
            )
        else:
            async with db.execute("SELECT DISTINCT category FROM services") as cur:
                existing_cats = await cur.fetchall()
            for (c_name,) in existing_cats:
                if c_name:
                    await db.execute(
                        "INSERT OR IGNORE INTO categories (name) VALUES (?)", (c_name,)
                    )

        await db.commit()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ МАСТЕРОВ ЗАПИСИ ====================
def _normalize_master_ids(
        master_ids: Union[int, List[int], None],
        second_master_id: Optional[int] = None,
) -> List[int]:
    result: List[int] = []
    if isinstance(master_ids, int):
        if master_ids > 0:
            result.append(master_ids)
    elif isinstance(master_ids, (list, tuple, set)):
        for m in master_ids:
            if str(m).strip().isdigit() and int(m) > 0 and int(m) not in result:
                result.append(int(m))
    if second_master_id and str(second_master_id).strip().isdigit():
        sm = int(second_master_id)
        if sm > 0 and sm not in result:
            result.append(sm)
    return result


async def _enrich_appointments_with_masters(
        conn: aiosqlite.Connection, apps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not apps:
        return apps
    app_ids = [a["id"] for a in apps]
    placeholders = ",".join("?" for _ in app_ids)
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
            f"""
        SELECT am.appointment_id, m.id AS master_id, m.name AS master_name, m.tg_id AS master_tg_id
        FROM appointment_masters am
        JOIN masters m ON am.master_id = m.id
        WHERE am.appointment_id IN ({placeholders})
        ORDER BY am.rowid ASC
        """,
            app_ids,
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    by_app: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        by_app.setdefault(r["appointment_id"], []).append(r)

    for a in apps:
        m_list = by_app.get(a["id"], [])
        if not m_list and a.get("master_id"):
            m_list = [
                {
                    "master_id": a["master_id"],
                    "master_name": a.get("master_name", f"Мастер #{a['master_id']}"),
                    "master_tg_id": a.get("master_tg_id"),
                }
            ]
        a["master_ids"] = [m["master_id"] for m in m_list]
        a["masters_names"] = [m["master_name"] for m in m_list]
        a["master_tg_ids"] = [m["master_tg_id"] for m in m_list if m.get("master_tg_id")]
        a["master_name"] = " + ".join(a["masters_names"]) if a["masters_names"] else "—"
        a["second_master_name"] = (
            ", ".join(a["masters_names"][1:]) if len(a["masters_names"]) > 1 else None
        )
    return apps


# ==================== ЛОГИ АДМИНИСТРАТОРОВ ====================
async def log_admin_action(admin_name: str, action: str, details: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_logs (admin_name, action, details) VALUES (?, ?, ?)",
            (admin_name, action, details),
        )
        await db.commit()


async def get_admin_logs(limit: int = 200) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ==================== ПОЛЬЗОВАТЕЛЬСКИЕ ДАННЫЕ, ПОИСК, БОНУСЫ И ЧС ====================
def _synthetic_tg_id_from_phone(phone: str, username: str = "") -> int:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits:
        return -int(digits[-10:])
    clean_u = (username or "").strip().lstrip("@").lower()
    if clean_u:
        return -abs(hash(clean_u) % 1000000000)
    return -999999


async def upsert_user(tg_id: int, username: str = "", full_name: str = "", phone: str = ""):
    if not tg_id:
        return
    clean_user = (username or "").strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        if tg_id > 0 and (clean_user or phone):
            async with db.execute(
                    """
                    SELECT tg_id, bonus_points, full_name, phone, username
                    FROM users
                    WHERE tg_id < 0 AND (
                        (? != '' AND LOWER(username) = LOWER(?))
                        OR (? != '' AND phone = ?)
                    )
                    LIMIT 1
                    """,
                    (clean_user, clean_user, phone, phone),
            ) as s_cur:
                synth_row = await s_cur.fetchone()
            if synth_row:
                synth_id, synth_pts, synth_name, synth_phone, synth_uname = synth_row
                await db.execute("DELETE FROM users WHERE tg_id = ?", (synth_id,))
                await db.execute(
                    """
                    INSERT OR IGNORE INTO users (tg_id, username, full_name, phone, bonus_points)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        tg_id,
                        clean_user or synth_uname,
                        full_name or synth_name,
                        phone or synth_phone,
                        synth_pts or 0,
                    ),
                )
                await db.execute(
                    "UPDATE appointments SET client_tg_id = ? WHERE client_tg_id = ?",
                    (tg_id, synth_id),
                )

        async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            existing = await cur.fetchone()
        if not existing:
            await db.execute(
                "INSERT INTO users (tg_id, username, full_name, phone, bonus_points) VALUES (?, ?, ?, ?, 0)",
                (tg_id, clean_user, full_name or "", phone or ""),
            )
        else:
            await db.execute(
                """
                UPDATE users
                SET username = CASE WHEN ? != '' THEN ? ELSE username END,
                    full_name = CASE WHEN ? != '' THEN ? ELSE full_name END,
                    phone = CASE WHEN ? != '' THEN ? ELSE phone END
                WHERE tg_id = ?
                """,
                (clean_user, clean_user, full_name, full_name, phone, phone, tg_id),
            )
        await db.commit()


async def find_user_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """Ищет клиента по @username, телефону или Telegram ID."""
    q = (identifier or "").strip()
    if not q:
        return None
    clean_u = q.lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if q.lstrip("-").isdigit():
            async with db.execute(
                    "SELECT * FROM users WHERE tg_id = ? OR phone LIKE ? LIMIT 1",
                    (int(q), f"%{q}%"),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)

        async with db.execute(
                """
                SELECT * FROM users
                WHERE LOWER(username) = ? OR phone = ?
                ORDER BY tg_id DESC LIMIT 1
                """,
                (clean_u, q),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)

        async with db.execute(
                """
                SELECT client_tg_id AS tg_id, client_username AS username,
                       client_name AS full_name, client_phone AS phone
                FROM appointments
                WHERE LOWER(client_username) = ? OR client_phone = ?
                ORDER BY id DESC LIMIT 1
                """,
                (clean_u, q),
        ) as a_cur:
            row = await a_cur.fetchone()
            if row:
                d = dict(row)
                d["bonus_points"] = 0
                return d
    return None


async def get_client_bonus_balance(
        client_tg_id: int = 0, client_phone: str = "", client_username: str = ""
) -> int:
    clean_u = (client_username or "").strip().lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        if client_tg_id and client_tg_id > 0:
            async with db.execute(
                    "SELECT bonus_points FROM users WHERE tg_id = ?", (client_tg_id,)
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    return int(row[0] or 0)
        if clean_u:
            async with db.execute(
                    "SELECT bonus_points FROM users WHERE LOWER(username) = ? ORDER BY tg_id DESC LIMIT 1",
                    (clean_u,),
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    return int(row[0] or 0)
        if client_phone:
            async with db.execute(
                    "SELECT bonus_points FROM users WHERE phone = ? ORDER BY tg_id DESC LIMIT 1",
                    (client_phone.strip(),),
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    return int(row[0] or 0)
    return 0


async def adjust_client_bonus_balance(
        client_tg_id: int,
        client_phone: str,
        client_name: str,
        delta: int,
        client_username: str = "",
) -> int:
    phone = (client_phone or "").strip()
    name = (client_name or "").strip()
    uname = (client_username or "").strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        target_tg_id = None
        current_pts = 0

        if client_tg_id and client_tg_id != 0:
            async with db.execute(
                    "SELECT tg_id, bonus_points FROM users WHERE tg_id = ?", (client_tg_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    target_tg_id, current_pts = row[0], int(row[1] or 0)

        if target_tg_id is None and uname:
            async with db.execute(
                    "SELECT tg_id, bonus_points FROM users WHERE LOWER(username) = LOWER(?) ORDER BY tg_id DESC LIMIT 1",
                    (uname,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    target_tg_id, current_pts = row[0], int(row[1] or 0)

        if target_tg_id is None and phone:
            async with db.execute(
                    "SELECT tg_id, bonus_points FROM users WHERE phone = ? ORDER BY tg_id DESC LIMIT 1",
                    (phone,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    target_tg_id, current_pts = row[0], int(row[1] or 0)

        new_pts = max(0, current_pts + int(delta))
        if target_tg_id is not None:
            await db.execute(
                """
                UPDATE users
                SET bonus_points = ?,
                    phone = CASE WHEN phone = '' AND ? != '' THEN ? ELSE phone END,
                    username = CASE WHEN username = '' AND ? != '' THEN ? ELSE username END,
                    full_name = CASE WHEN full_name = '' AND ? != '' THEN ? ELSE full_name END
                WHERE tg_id = ?
                """,
                (new_pts, phone, phone, uname, uname, name, name, target_tg_id),
            )
        else:
            uid = (
                client_tg_id
                if (client_tg_id and client_tg_id != 0)
                else _synthetic_tg_id_from_phone(phone, uname)
            )
            await db.execute(
                "INSERT OR REPLACE INTO users (tg_id, username, full_name, phone, bonus_points) VALUES (?, ?, ?, ?, ?)",
                (uid, uname, name, phone, new_pts),
            )
        await db.commit()
        return new_pts


async def get_all_users() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id > 0 ORDER BY created_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_notify_tg_ids() -> List[int]:
    """Возвращает список Telegram ID для оповещений о старте/остановке бота (админы не на смене исключаются)."""
    ids = set()
    off_shift_admins = set()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tg_id FROM admins WHERE on_shift = 0") as off_cur:
            for (tid,) in await off_cur.fetchall():
                if tid:
                    off_shift_admins.add(int(tid))

        for query in (
                "SELECT tg_id FROM users WHERE tg_id > 0",
                "SELECT tg_id FROM admins WHERE tg_id > 0 AND on_shift = 1",
                "SELECT tg_id FROM masters WHERE tg_id IS NOT NULL AND tg_id > 0",
        ):
            async with db.execute(query) as cur:
                for (tid,) in await cur.fetchall():
                    if tid and int(tid) > 0 and int(tid) not in off_shift_admins:
                        ids.add(int(tid))
    return list(ids)


# ==================== ЧЕРНЫЙ СПИСОК ПО @USERNAME ====================
async def get_blacklist() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM blacklist ORDER BY id DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_to_blacklist(username: str, tg_id: Optional[int] = None, reason: str = ""):
    clean_username = username.strip().lstrip("@").lower()
    if not clean_username:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        if not tg_id:
            async with db.execute(
                    "SELECT tg_id FROM users WHERE LOWER(username) = ? AND tg_id > 0 LIMIT 1",
                    (clean_username,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    tg_id = row[0]
        await db.execute(
            "INSERT OR REPLACE INTO blacklist (username, tg_id, reason) VALUES (?, ?, ?)",
            (clean_username, tg_id, reason.strip()),
        )
        await db.commit()


async def remove_from_blacklist(username_or_id: str):
    clean = str(username_or_id).strip().lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        if clean.isdigit():
            await db.execute(
                "DELETE FROM blacklist WHERE id = ? OR tg_id = ?",
                (int(clean), int(clean)),
            )
        else:
            await db.execute("DELETE FROM blacklist WHERE LOWER(username) = ?", (clean,))
        await db.commit()


async def is_user_blacklisted(
        tg_id: Optional[int] = None, username: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    clean_username = (username or "").strip().lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if clean_username:
            async with db.execute(
                    "SELECT * FROM blacklist WHERE LOWER(username) = ?", (clean_username,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        if tg_id and tg_id > 0:
            async with db.execute(
                    "SELECT * FROM blacklist WHERE tg_id = ?", (tg_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
    return None


# ==================== ИСТОРИЯ И АНАЛИТИКА КЛИЕНТОВ ====================
async def get_clients_summary(sort_by: str = "last_visit") -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as u_cur:
            users_rows = [dict(r) for r in await u_cur.fetchall()]

        async with db.execute("SELECT LOWER(username) AS uname, tg_id FROM blacklist") as b_cur:
            bl_rows = await b_cur.fetchall()
            bl_usernames = {r["uname"] for r in bl_rows if r["uname"]}
            bl_tg_ids = {r["tg_id"] for r in bl_rows if r["tg_id"]}

        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       s.name AS service_name,
                       s.price AS service_price
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                ORDER BY a.appointment_datetime DESC
                """
        ) as a_cur:
            all_apps = [dict(r) for r in await a_cur.fetchall()]

        await _enrich_appointments_with_masters(db, all_apps)

    users_by_tg = {u["tg_id"]: u for u in users_rows if u["tg_id"] != 0}
    users_by_phone = {u["phone"]: u for u in users_rows if u.get("phone")}
    users_by_uname = {
        u["username"].lstrip("@").lower(): u
        for u in users_rows
        if u.get("username")
    }

    clients_map: Dict[str, Dict[str, Any]] = {}

    for u in users_rows:
        uname = (u.get("username") or "").lstrip("@")
        key = (
            f"tg_{u['tg_id']}"
            if u["tg_id"] > 0
            else (f"un_{uname.lower()}" if uname else f"ph_{u['phone']}")
        )
        clients_map[key] = {
            "client_key": key,
            "tg_id": u["tg_id"],
            "username": uname,
            "client_name": u.get("full_name") or uname or "Клиент",
            "client_phone": u.get("phone") or "",
            "bonus_points": int(u.get("bonus_points") or 0),
            "visits_count": 0,
            "total_appointments": 0,
            "total_spent": 0,
            "avg_check": 0,
            "masters_set": set(),
            "last_visit": "",
            "is_blacklisted": (uname.lower() in bl_usernames)
                              or (u["tg_id"] > 0 and u["tg_id"] in bl_tg_ids),
            "history": [],
        }

    for a in all_apps:
        tg_id = a.get("client_tg_id") or 0
        phone = (a.get("client_phone") or "").strip()
        a_uname = (a.get("client_username") or "").strip().lstrip("@")

        key = None
        if tg_id > 0 and f"tg_{tg_id}" in clients_map:
            key = f"tg_{tg_id}"
        elif a_uname and a_uname.lower() in users_by_uname:
            matched_u = users_by_uname[a_uname.lower()]
            key = (
                f"tg_{matched_u['tg_id']}"
                if matched_u["tg_id"] > 0
                else f"un_{a_uname.lower()}"
            )
        elif phone and phone in users_by_phone:
            matched_u = users_by_phone[phone]
            m_uname = (matched_u.get("username") or "").lstrip("@")
            key = (
                f"tg_{matched_u['tg_id']}"
                if matched_u["tg_id"] > 0
                else (f"un_{m_uname.lower()}" if m_uname else f"ph_{matched_u['phone']}")
            )
        elif tg_id > 0:
            key = f"tg_{tg_id}"
        elif a_uname:
            key = f"un_{a_uname.lower()}"
        else:
            key = f"ph_{phone}"

        if key not in clients_map:
            u_obj = (
                    users_by_tg.get(tg_id)
                    or users_by_uname.get(a_uname.lower())
                    or users_by_phone.get(phone)
                    or {}
            )
            uname = a_uname or (u_obj.get("username") or "").lstrip("@")
            clients_map[key] = {
                "client_key": key,
                "tg_id": tg_id or u_obj.get("tg_id") or 0,
                "username": uname,
                "client_name": a["client_name"],
                "client_phone": phone,
                "bonus_points": int(u_obj.get("bonus_points") or 0),
                "visits_count": 0,
                "total_appointments": 0,
                "total_spent": 0,
                "avg_check": 0,
                "masters_set": set(),
                "last_visit": "",
                "is_blacklisted": (uname.lower() in bl_usernames)
                                  or (tg_id > 0 and tg_id in bl_tg_ids),
                "history": [],
            }

        c = clients_map[key]
        if a["client_name"]:
            c["client_name"] = a["client_name"]
        if phone and not c["client_phone"]:
            c["client_phone"] = phone
        if a_uname and not c["username"]:
            c["username"] = a_uname

        c["total_appointments"] += 1
        if a["appointment_datetime"] > c["last_visit"]:
            c["last_visit"] = a["appointment_datetime"]

        if a["status"] in ("confirmed", "paid"):
            c["visits_count"] += 1
            for m_name in a.get("masters_names", [a["master_name"]]):
                c["masters_set"].add(m_name)

        if a["status"] == "paid":
            paid_val = int(a["final_paid"] if a.get("final_paid") is not None else a["service_price"])
            c["total_spent"] += paid_val

        c["history"].append(
            {
                "id": a["id"],
                "datetime": a["appointment_datetime"],
                "service_name": a["service_name"],
                "master_name": a["master_name"],
                "status": a["status"],
                "payment_method": a.get("payment_method"),
                "full_price": a.get("full_price") or a["service_price"],
                "points_spent": a.get("points_spent") or 0,
                "points_earned": a.get("points_earned") or 0,
                "final_paid": a.get("final_paid") if a["status"] == "paid" else a["service_price"],
                "comment": a.get("comment") or "",
            }
        )

    result = []
    for c in clients_map.values():
        paid_visits = sum(1 for h in c["history"] if h["status"] == "paid")
        c["avg_check"] = int(round(c["total_spent"] / paid_visits)) if paid_visits > 0 else 0
        c["masters"] = ", ".join(sorted(c["masters_set"])) if c["masters_set"] else "—"
        del c["masters_set"]
        result.append(c)

    if sort_by == "visits":
        result.sort(key=lambda x: (x["visits_count"], x["total_spent"]), reverse=True)
    elif sort_by == "total_spent":
        result.sort(key=lambda x: (x["total_spent"], x["visits_count"]), reverse=True)
    else:
        result.sort(key=lambda x: x["last_visit"] or "", reverse=True)

    return result


# ==================== РАСЧЁТ КЛИЕНТА (НАЛИЧНЫЙ / БЕЗНАЛИЧНЫЙ + БОНУСЫ + 40%/60%) ====================
async def process_appointment_payment(
        appointment_id: int,
        payment_method: str,
        points_to_spend: int = 0,
        discount_amount: int = 0,
) -> Optional[Dict[str, Any]]:
    details = await get_appointment_details(appointment_id)
    if not details:
        return None

    full_price = int(details["service_price"])
    discount_amount = max(0, int(discount_amount or 0))
    available_points = await get_client_bonus_balance(
        details["client_tg_id"], details["client_phone"], details.get("client_username", "")
    )

    max_spendable = max(0, full_price - discount_amount)
    points_spent = max(0, min(int(points_to_spend or 0), available_points, max_spendable))
    final_paid = max(0, full_price - discount_amount - points_spent)

    if payment_method == "cash":
        points_earned = int(round(full_price * 0.05))
    else:
        points_earned = 0

    master_share = round(full_price * 0.40, 2)
    salon_share = round(final_paid - master_share, 2)
    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE appointments
            SET status = 'paid',
                payment_method = ?,
                full_price = ?,
                discount_amount = ?,
                points_spent = ?,
                points_earned = ?,
                final_paid = ?,
                master_share = ?,
                salon_share = ?,
                paid_at = ?
            WHERE id = ?
            """,
            (
                payment_method,
                full_price,
                discount_amount,
                points_spent,
                points_earned,
                final_paid,
                master_share,
                salon_share,
                paid_at,
                appointment_id,
            ),
        )
        await db.commit()

    new_bonus_balance = await adjust_client_bonus_balance(
        details["client_tg_id"],
        details["client_phone"],
        details["client_name"],
        points_earned - points_spent,
        details.get("client_username", ""),
        )

    updated = await get_appointment_details(appointment_id)
    updated["client_bonus_balance"] = new_bonus_balance
    return updated


# ==================== ОТЧЕТНОСТЬ ПО НЕДЕЛЯМ (40% МАСТЕР / 60% САЛОН) ====================
async def get_weekly_financial_report(start_date: str, end_date: str) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       s.name AS service_name,
                       s.price AS service_price
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                WHERE a.status = 'paid'
                  AND substr(a.appointment_datetime, 1, 10) BETWEEN ? AND ?
                ORDER BY a.appointment_datetime ASC
                """,
                (start_date, end_date),
        ) as cur:
            paid_apps = [dict(r) for r in await cur.fetchall()]

        await _enrich_appointments_with_masters(db, paid_apps)

    total_full_price = 0
    total_final_paid = 0
    total_cash = 0
    total_cashless = 0
    total_points_spent = 0
    total_discounts = 0
    total_master_share = 0.0
    total_salon_share = 0.0

    masters_summary: Dict[str, Dict[str, Any]] = {}

    for a in paid_apps:
        fp = int(a["full_price"] or a["service_price"])
        paid = int(a["final_paid"] if a["final_paid"] is not None else fp)
        pts = int(a["points_spent"] or 0)
        disc = int(a["discount_amount"] or 0)
        m_share = float(a["master_share"] if a["master_share"] else round(fp * 0.40, 2))
        s_share = float(a["salon_share"] if a["salon_share"] is not None else round(paid - m_share, 2))

        total_full_price += fp
        total_final_paid += paid
        total_points_spent += pts
        total_discounts += disc
        total_master_share += m_share
        total_salon_share += s_share

        if a.get("payment_method") == "cashless":
            total_cashless += paid
        else:
            total_cash += paid

        m_names = a.get("masters_names") or [a["master_name"]]
        count_m = max(1, len(m_names))
        per_master_share = round(m_share / count_m, 2)
        per_master_fp = round(fp / count_m, 2)

        for m_name in m_names:
            if m_name not in masters_summary:
                masters_summary[m_name] = {
                    "master_name": m_name,
                    "services_count": 0,
                    "full_services_sum": 0.0,
                    "master_40_share": 0.0,
                }
            masters_summary[m_name]["services_count"] += 1
            masters_summary[m_name]["full_services_sum"] = round(
                masters_summary[m_name]["full_services_sum"] + per_master_fp, 2
            )
            masters_summary[m_name]["master_40_share"] = round(
                masters_summary[m_name]["master_40_share"] + per_master_share, 2
            )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "count": len(paid_apps),
        "total_full_price": total_full_price,
        "total_final_paid": total_final_paid,
        "total_cash": total_cash,
        "total_cashless": total_cashless,
        "total_points_spent": total_points_spent,
        "total_discounts": total_discounts,
        "total_master_share": round(total_master_share, 2),
        "total_salon_share": round(total_salon_share, 2),
        "masters_breakdown": list(masters_summary.values()),
        "appointments": paid_apps,
    }


# ==================== АДМИНИСТРАТОРЫ И СМЕНЫ ====================
async def get_admins(only_on_shift: bool = False) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM admins"
        if only_on_shift:
            query += " WHERE on_shift = 1"
        query += " ORDER BY added_at ASC"
        async with db.execute(query) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_admin_by_tg_id(tg_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM admins WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def is_admin(tg_id: int) -> bool:
    return (await get_admin_by_tg_id(tg_id)) is not None


async def set_admin_shift(tg_id: int, on_shift: bool) -> bool:
    val = 1 if on_shift else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE admins SET on_shift = ? WHERE tg_id = ?",
            (val, tg_id),
        )
        await db.commit()
    return on_shift


async def toggle_admin_shift(tg_id: int) -> bool:
    adm = await get_admin_by_tg_id(tg_id)
    if not adm:
        return False
    new_state = not bool(adm.get("on_shift"))
    await set_admin_shift(tg_id, new_state)
    return new_state


async def add_admin(tg_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT on_shift FROM admins WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
            cur_shift = int(row[0]) if row else 0
        await db.execute(
            "INSERT OR REPLACE INTO admins (tg_id, name, on_shift) VALUES (?, ?, ?)",
            (tg_id, name.strip(), cur_shift),
        )
        await db.commit()


async def remove_admin(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE tg_id = ?", (tg_id,))
        await db.commit()


# ==================== КАТЕГОРИИ И УСЛУГИ ====================
async def get_categories_full() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories ORDER BY id ASC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_categories() -> List[str]:
    rows = await get_categories_full()
    return [r["name"] for r in rows]


async def add_category(name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)", (name.strip(),)
        )
        await db.commit()
        return cur.lastrowid


async def update_category(cat_id: int, new_name: str):
    new_name = new_name.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)) as cur:
            row = await cur.fetchone()
        if row:
            old_name = row[0]
            await db.execute(
                "UPDATE categories SET name = ? WHERE id = ?", (new_name, cat_id)
            )
            await db.execute(
                "UPDATE services SET category = ? WHERE category = ?",
                (new_name, old_name),
            )
            await db.commit()


async def delete_category(cat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)) as cur:
            row = await cur.fetchone()
        if row:
            cat_name = row[0]
            async with db.execute(
                    "SELECT id FROM services WHERE category = ?", (cat_name,)
            ) as s_cur:
                srv_ids = [r[0] for r in await s_cur.fetchall()]
            for sid in srv_ids:
                await db.execute(
                    "DELETE FROM master_services WHERE service_id = ?", (sid,)
                )
            await db.execute("DELETE FROM services WHERE category = ?", (cat_name,))
            await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            await db.commit()


async def get_all_services() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM services ORDER BY category ASC, id ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_services_by_category(category: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM services WHERE category = ?", (category,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_service_by_id(service_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM services WHERE id = ?", (service_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_service(category: str, name: str, price: int, duration_minutes: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)", (category.strip(),)
        )
        cur = await db.execute(
            "INSERT INTO services (category, name, price, duration_minutes) VALUES (?, ?, ?, ?)",
            (category.strip(), name.strip(), price, duration_minutes),
        )
        service_id = cur.lastrowid

        async with db.execute(
                """
                SELECT DISTINCT ms.master_id
                FROM master_services ms
                JOIN services s ON ms.service_id = s.id
                WHERE s.category = ?
                """,
                (category.strip(),),
        ) as m_cur:
            for (m_id,) in await m_cur.fetchall():
                await db.execute(
                    "INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?, ?)",
                    (m_id, service_id),
                )
        await db.commit()
        return service_id


async def update_service(
        service_id: int, category: str, name: str, price: int, duration_minutes: int
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)", (category.strip(),)
        )
        await db.execute(
            """
            UPDATE services
            SET category = ?, name = ?, price = ?, duration_minutes = ?
            WHERE id = ?
            """,
            (category.strip(), name.strip(), price, duration_minutes, service_id),
        )
        await db.commit()


async def delete_service(service_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM master_services WHERE service_id = ?", (service_id,)
        )
        await db.execute("DELETE FROM services WHERE id = ?", (service_id,))
        await db.commit()


# ==================== МАСТЕРА И ОТГУЛЫ ====================
async def get_all_masters(only_active: bool = True) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM masters"
        if only_active:
            query += " WHERE is_active = 1"
        query += " ORDER BY id ASC"
        async with db.execute(query) as cur:
            masters = [dict(r) for r in await cur.fetchall()]

        for m in masters:
            async with db.execute(
                    """
                    SELECT DISTINCT s.category
                    FROM services s
                    JOIN master_services ms ON s.id = ms.service_id
                    WHERE ms.master_id = ?
                    """,
                    (m["id"],),
            ) as c_cur:
                m["categories"] = [r[0] for r in await c_cur.fetchall()]
        return masters


async def get_master_by_id(master_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not master_id:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM masters WHERE id = ?", (master_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            m = dict(row)
        async with db.execute(
                """
                SELECT DISTINCT s.category
                FROM services s
                JOIN master_services ms ON s.id = ms.service_id
                WHERE ms.master_id = ?
                """,
                (master_id,),
        ) as c_cur:
            m["categories"] = [r[0] for r in await c_cur.fetchall()]
        return m


async def get_master_by_tg_id(tg_id: int) -> Optional[Dict[str, Any]]:
    if not tg_id:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM masters WHERE tg_id = ? AND is_active = 1", (tg_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_masters_for_service(service_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT m.* FROM masters m
                JOIN master_services ms ON m.id = ms.master_id
                WHERE ms.service_id = ? AND m.is_active = 1
                """,
                (service_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_master(
        name: str,
        specialization: str,
        categories: List[str],
        tg_id: Optional[int] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO masters (name, specialization, tg_id) VALUES (?, ?, ?)",
            (name.strip(), specialization.strip(), tg_id),
        )
        master_id = cur.lastrowid

        for cat in categories:
            async with db.execute(
                    "SELECT id FROM services WHERE category = ?", (cat,)
            ) as s_cur:
                for (s_id,) in await s_cur.fetchall():
                    await db.execute(
                        "INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?, ?)",
                        (master_id, s_id),
                    )
        await db.commit()
        return master_id


async def update_master(
        master_id: int,
        name: str,
        specialization: str,
        categories: List[str],
        tg_id: Optional[int] = None,
        is_active: int = 1,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE masters
            SET name = ?, specialization = ?, tg_id = ?, is_active = ?
            WHERE id = ?
            """,
            (name.strip(), specialization.strip(), tg_id, is_active, master_id),
        )
        await db.execute(
            "DELETE FROM master_services WHERE master_id = ?", (master_id,)
        )
        for cat in categories:
            async with db.execute(
                    "SELECT id FROM services WHERE category = ?", (cat,)
            ) as s_cur:
                for (s_id,) in await s_cur.fetchall():
                    await db.execute(
                        "INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?, ?)",
                        (master_id, s_id),
                    )
        await db.commit()


async def delete_master(master_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM master_services WHERE master_id = ?", (master_id,))
        await db.execute("DELETE FROM master_time_off WHERE master_id = ?", (master_id,))
        await db.execute("DELETE FROM appointment_masters WHERE master_id = ?", (master_id,))
        await db.execute("DELETE FROM masters WHERE id = ?", (master_id,))
        await db.commit()


async def get_master_time_offs(master_id: Optional[int] = None) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT t.*, m.name AS master_name
            FROM master_time_off t
            JOIN masters m ON t.master_id = m.id
        """
        params = []
        if master_id:
            query += " WHERE t.master_id = ?"
            params.append(master_id)
        query += " ORDER BY t.start_datetime DESC"
        async with db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_master_time_off(
        master_id: int,
        off_type: str,
        start_datetime: str,
        end_datetime: str,
        reason: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO master_time_off (master_id, type, start_datetime, end_datetime, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (master_id, off_type, start_datetime.strip(), end_datetime.strip(), reason.strip()),
        )
        await db.commit()
        return cur.lastrowid


async def update_master_time_off(
        off_id: int,
        master_id: int,
        off_type: str,
        start_datetime: str,
        end_datetime: str,
        reason: str = "",
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE master_time_off
            SET master_id = ?, type = ?, start_datetime = ?, end_datetime = ?, reason = ?
            WHERE id = ?
            """,
            (master_id, off_type, start_datetime.strip(), end_datetime.strip(), reason.strip(), off_id),
        )
        await db.commit()


async def delete_master_time_off(off_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM master_time_off WHERE id = ?", (off_id,))
        await db.commit()


# ==================== СЛОТЫ (ШАГ 15 МИНУТ + ЛЮБОЕ КОЛ-ВО МАСТЕРОВ) ====================
async def _get_busy_intervals_for_masters(
        master_ids: List[int],
        date_str: str,
        exclude_appointment_id: Optional[int] = None,
):
    busy_intervals = []
    valid_ids = [int(mid) for mid in master_ids if mid]
    if not valid_ids:
        return busy_intervals

    placeholders = ",".join("?" for _ in valid_ids)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = f"""
            SELECT DISTINCT a.id, a.appointment_datetime, s.duration_minutes
            FROM appointments a
            JOIN services s ON a.service_id = s.id
            LEFT JOIN appointment_masters am ON a.id = am.appointment_id
            WHERE (
                am.master_id IN ({placeholders})
                OR a.master_id IN ({placeholders})
                OR a.second_master_id IN ({placeholders})
            )
              AND a.appointment_datetime LIKE ?
              AND a.status IN ('pending', 'confirmed', 'paid')
        """
        params: List[Any] = valid_ids + valid_ids + valid_ids + [f"{date_str}%"]
        if exclude_appointment_id:
            query += " AND a.id != ?"
            params.append(exclude_appointment_id)

        async with db.execute(query, params) as cur:
            for row in await cur.fetchall():
                s_dt = datetime.strptime(row["appointment_datetime"], "%Y-%m-%d %H:%M")
                e_dt = s_dt + timedelta(minutes=row["duration_minutes"])
                busy_intervals.append((s_dt, e_dt))

        off_query = f"SELECT start_datetime, end_datetime FROM master_time_off WHERE master_id IN ({placeholders})"
        async with db.execute(off_query, valid_ids) as o_cur:
            for off in await o_cur.fetchall():
                try:
                    o_start = datetime.strptime(off["start_datetime"], "%Y-%m-%d %H:%M")
                    o_end = datetime.strptime(off["end_datetime"], "%Y-%m-%d %H:%M")
                    busy_intervals.append((o_start, o_end))
                except Exception:
                    pass

    return busy_intervals


async def get_available_slots(
        master_ids: Union[int, List[int]],
        date_str: str,
        duration_minutes: int = 60,
        exclude_appointment_id: Optional[int] = None,
        second_master_id: Optional[int] = None,
) -> List[str]:
    norm_ids = _normalize_master_ids(master_ids, second_master_id)
    busy_intervals = await _get_busy_intervals_for_masters(
        norm_ids, date_str, exclude_appointment_id
    )

    now = datetime.now()
    work_start_dt = datetime.strptime(
        f"{date_str} {WORK_START_HOUR:02d}:00", "%Y-%m-%d %H:%M"
    )
    work_end_dt = datetime.strptime(
        f"{date_str} {WORK_END_HOUR:02d}:00", "%Y-%m-%d %H:%M"
    )

    available_slots = []
    total_work_minutes = (WORK_END_HOUR - WORK_START_HOUR) * 60

    for offset in range(0, total_work_minutes, SLOT_STEP_MINUTES):
        slot_dt = work_start_dt + timedelta(minutes=offset)
        slot_end_dt = slot_dt + timedelta(minutes=duration_minutes)

        if slot_dt <= now + timedelta(minutes=10):
            continue
        if slot_end_dt > work_end_dt:
            continue

        overlap = False
        for b_start, b_end in busy_intervals:
            if slot_dt < b_end and slot_end_dt > b_start:
                overlap = True
                break

        if not overlap:
            available_slots.append(slot_dt.strftime("%H:%M"))

    return available_slots


async def is_slot_free_for_master(
        master_ids: Union[int, List[int]],
        appointment_datetime: str,
        duration_minutes: int,
        exclude_appointment_id: Optional[int] = None,
        second_master_id: Optional[int] = None,
) -> bool:
    start_dt = datetime.strptime(appointment_datetime, "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    date_str = start_dt.strftime("%Y-%m-%d")

    work_start_dt = datetime.strptime(
        f"{date_str} {WORK_START_HOUR:02d}:00", "%Y-%m-%d %H:%M"
    )
    work_end_dt = datetime.strptime(
        f"{date_str} {WORK_END_HOUR:02d}:00", "%Y-%m-%d %H:%M"
    )
    if start_dt < work_start_dt or end_dt > work_end_dt:
        return False

    norm_ids = _normalize_master_ids(master_ids, second_master_id)
    busy_intervals = await _get_busy_intervals_for_masters(
        norm_ids, date_str, exclude_appointment_id
    )
    for b_start, b_end in busy_intervals:
        if start_dt < b_end and end_dt > b_start:
            return False

    return True


# ==================== ЗАПИСИ КЛИЕНТОВ ====================
async def create_appointment(
        client_tg_id: int,
        client_name: str,
        client_phone: str,
        master_id: int,
        service_id: int,
        appointment_datetime: str,
        second_master_id: Optional[int] = None,
        comment: str = "",
        master_ids: Optional[List[int]] = None,
        client_username: str = "",
) -> int:
    all_m_ids = _normalize_master_ids(master_ids or master_id, second_master_id)
    primary_master = all_m_ids[0]
    second_m = all_m_ids[1] if len(all_m_ids) > 1 else None

    clean_uname = (client_username or "").strip().lstrip("@")
    clean_phone = (client_phone or "").strip()
    clean_name = (client_name or "").strip()

    srv = await get_service_by_id(service_id)
    full_price = int(srv["price"]) if srv else 0

    async with aiosqlite.connect(DB_PATH) as db:
        resolved_tg_id = int(client_tg_id or 0)
        if resolved_tg_id <= 0 and (clean_uname or clean_phone):
            async with db.execute(
                    """
                    SELECT tg_id, username FROM users
                    WHERE (? != '' AND LOWER(username) = LOWER(?))
                       OR (? != '' AND phone = ?)
                    ORDER BY tg_id DESC LIMIT 1
                    """,
                    (clean_uname, clean_uname, clean_phone, clean_phone),
            ) as u_cur:
                u_row = await u_cur.fetchone()
                if u_row:
                    resolved_tg_id = int(u_row[0])
                    if not clean_uname and u_row[1]:
                        clean_uname = u_row[1]

        cur = await db.execute(
            """
            INSERT INTO appointments (
                client_tg_id, client_username, client_name, client_phone,
                master_id, second_master_id, service_id,
                appointment_datetime, comment, status, full_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                resolved_tg_id,
                clean_uname,
                clean_name,
                clean_phone,
                primary_master,
                second_m,
                service_id,
                appointment_datetime,
                comment.strip(),
                full_price,
            ),
        )
        app_id = cur.lastrowid

        for mid in all_m_ids:
            await db.execute(
                "INSERT OR IGNORE INTO appointment_masters (appointment_id, master_id) VALUES (?, ?)",
                (app_id, mid),
            )
        await db.commit()

    uid_for_upsert = (
        resolved_tg_id
        if resolved_tg_id != 0
        else _synthetic_tg_id_from_phone(clean_phone, clean_uname)
    )
    await upsert_user(
        tg_id=uid_for_upsert,
        username=clean_uname,
        full_name=clean_name,
        phone=clean_phone,
    )
    return app_id


async def update_appointment_full(
        appointment_id: int,
        client_name: str,
        client_phone: str,
        master_id: int,
        second_master_id: Optional[int],
        service_id: int,
        appointment_datetime: str,
        comment: str,
        status: Optional[str] = None,
        master_ids: Optional[List[int]] = None,
        client_username: Optional[str] = None,
):
    all_m_ids = _normalize_master_ids(master_ids or master_id, second_master_id)
    primary_master = all_m_ids[0]
    second_m = all_m_ids[1] if len(all_m_ids) > 1 else None

    srv = await get_service_by_id(service_id)
    full_price = int(srv["price"]) if srv else 0
    clean_uname = (client_username or "").strip().lstrip("@") if client_username is not None else None

    async with aiosqlite.connect(DB_PATH) as db:
        if status:
            await db.execute(
                """
                UPDATE appointments
                SET client_name = ?,
                    client_phone = ?,
                    client_username = CASE WHEN ? IS NOT NULL THEN ? ELSE client_username END,
                    master_id = ?,
                    second_master_id = ?,
                    service_id = ?,
                    appointment_datetime = ?,
                    comment = ?,
                    status = ?,
                    full_price = ?
                WHERE id = ?
                """,
                (
                    client_name.strip(),
                    client_phone.strip(),
                    clean_uname,
                    clean_uname or "",
                    primary_master,
                    second_m,
                    service_id,
                    appointment_datetime.strip(),
                    comment.strip(),
                    status,
                    full_price,
                    appointment_id,
                ),
            )
        else:
            await db.execute(
                """
                UPDATE appointments
                SET client_name = ?,
                    client_phone = ?,
                    client_username = CASE WHEN ? IS NOT NULL THEN ? ELSE client_username END,
                    master_id = ?,
                    second_master_id = ?,
                    service_id = ?,
                    appointment_datetime = ?,
                    comment = ?,
                    full_price = ?
                WHERE id = ?
                """,
                (
                    client_name.strip(),
                    client_phone.strip(),
                    clean_uname,
                    clean_uname or "",
                    primary_master,
                    second_m,
                    service_id,
                    appointment_datetime.strip(),
                    comment.strip(),
                    full_price,
                    appointment_id,
                ),
            )

        await db.execute(
            "DELETE FROM appointment_masters WHERE appointment_id = ?", (appointment_id,)
        )
        for mid in all_m_ids:
            await db.execute(
                "INSERT OR IGNORE INTO appointment_masters (appointment_id, master_id) VALUES (?, ?)",
                (appointment_id, mid),
            )
        await db.commit()


async def get_appointment_details(appointment_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       m1.tg_id AS master_tg_id,
                       s.name AS service_name,
                       s.category AS service_category,
                       s.price AS service_price,
                       s.duration_minutes AS service_duration
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                WHERE a.id = ?
                """,
                (appointment_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
        await _enrich_appointments_with_masters(db, [d])

    d["client_bonus_balance"] = await get_client_bonus_balance(
        d["client_tg_id"], d["client_phone"], d.get("client_username", "")
    )
    return d


async def get_client_appointments(client_tg_id: int) -> List[Dict[str, Any]]:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       s.name AS service_name,
                       s.category AS service_category,
                       s.price AS service_price,
                       s.duration_minutes AS service_duration
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                WHERE a.client_tg_id = ?
                  AND a.status IN ('pending', 'confirmed')
                  AND a.appointment_datetime >= ?
                ORDER BY a.appointment_datetime ASC
                """,
                (client_tg_id, now_str),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return await _enrich_appointments_with_masters(db, rows)


async def get_appointments_by_date(date_str: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       m1.tg_id AS master_tg_id,
                       s.name AS service_name,
                       s.price AS service_price,
                       s.duration_minutes AS service_duration
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                WHERE a.appointment_datetime LIKE ?
                  AND a.status IN ('pending', 'confirmed', 'paid')
                ORDER BY a.appointment_datetime ASC
                """,
                (f"{date_str}%",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return await _enrich_appointments_with_masters(db, rows)


async def get_all_appointments_for_admin(
        filter_mode: str = "upcoming", limit: int = 100
) -> List[Dict[str, Any]]:
    """Возвращает список записей для просмотра и редактирования администратором в боте."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")

    query = """
        SELECT a.*,
               m1.name AS master_name,
               m1.tg_id AS master_tg_id,
               s.name AS service_name,
               s.category AS service_category,
               s.price AS service_price,
               s.duration_minutes AS service_duration
        FROM appointments a
        JOIN masters m1 ON a.master_id = m1.id
        JOIN services s ON a.service_id = s.id
    """
    params: List[Any] = []

    if filter_mode == "today":
        query += " WHERE a.appointment_datetime LIKE ? AND a.status != 'cancelled' ORDER BY a.appointment_datetime ASC"
        params.append(f"{today_str}%")
    elif filter_mode == "tomorrow":
        query += " WHERE a.appointment_datetime LIKE ? AND a.status != 'cancelled' ORDER BY a.appointment_datetime ASC"
        params.append(f"{tomorrow_str}%")
    elif filter_mode == "all":
        query += " ORDER BY a.appointment_datetime DESC LIMIT ?"
        params.append(limit)
    else:  # upcoming
        query += " WHERE a.appointment_datetime >= ? AND a.status IN ('pending', 'confirmed', 'paid') ORDER BY a.appointment_datetime ASC LIMIT ?"
        params.extend([f"{today_str} 00:00", limit])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return await _enrich_appointments_with_masters(db, rows)


async def get_master_appointments(
        master_id: int, date_str: Optional[str] = None, only_confirmed: bool = False
) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT DISTINCT a.*,
                   m1.name AS master_name,
                   s.name AS service_name,
                   s.price AS service_price,
                   s.duration_minutes AS service_duration
            FROM appointments a
            JOIN masters m1 ON a.master_id = m1.id
            JOIN services s ON a.service_id = s.id
            LEFT JOIN appointment_masters am ON a.id = am.appointment_id
            WHERE (am.master_id = ? OR a.master_id = ? OR a.second_master_id = ?)
        """
        params: List[Any] = [master_id, master_id, master_id]
        if only_confirmed:
            query += " AND a.status IN ('confirmed', 'paid')"
        else:
            query += " AND a.status IN ('pending', 'confirmed', 'paid')"

        if date_str:
            query += " AND a.appointment_datetime LIKE ?"
            params.append(f"{date_str}%")
        else:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            query += " AND a.appointment_datetime >= ?"
            params.append(now_str)

        query += " ORDER BY a.appointment_datetime ASC"
        async with db.execute(query, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return await _enrich_appointments_with_masters(db, rows)


async def update_appointment_status(appointment_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE appointments SET status = ? WHERE id = ?",
            (status, appointment_id),
        )
        await db.commit()


async def get_appointments_for_24h_reminder() -> List[Dict[str, Any]]:
    now = datetime.now()
    window_start = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    window_end = (now + timedelta(hours=25)).strftime("%Y-%m-%d %H:%M")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT a.*,
                       m1.name AS master_name,
                       s.name AS service_name,
                       s.price AS service_price
                FROM appointments a
                JOIN masters m1 ON a.master_id = m1.id
                JOIN services s ON a.service_id = s.id
                WHERE a.status IN ('pending', 'confirmed')
                  AND a.reminded_24h = 0
                  AND a.appointment_datetime BETWEEN ? AND ?
                """,
                (window_start, window_end),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return await _enrich_appointments_with_masters(db, rows)


async def mark_appointment_reminded(appointment_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE appointments SET reminded_24h = 1 WHERE id = ?",
            (appointment_id,),
        )
        await db.commit()


# ==================== СИСТЕМА ТИКЕТОВ И ИНИЦИАЦИЯ ДИАЛОГА АДМИНОМ ====================
async def create_support_ticket(
        client_tg_id: int, client_username: str, client_name: str
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO support_tickets (client_tg_id, client_username, client_name, status)
            VALUES (?, ?, ?, 'open')
            """,
            (client_tg_id, client_username or "", client_name or ""),
        )
        await db.commit()
        return cur.lastrowid


async def open_or_activate_admin_dialog(
        client_tg_id: int,
        client_username: str,
        client_name: str,
        admin_tg_id: Optional[int],
        admin_name: str,
) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT * FROM support_tickets
                WHERE client_tg_id = ? AND status IN ('open', 'active')
                ORDER BY id DESC LIMIT 1
                """,
                (client_tg_id,),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            ticket_id = existing["id"]
            await db.execute(
                """
                UPDATE support_tickets
                SET status = 'active',
                    admin_tg_id = COALESCE(?, admin_tg_id),
                    admin_name = ?
                WHERE id = ?
                """,
                (admin_tg_id, admin_name, ticket_id),
            )
        else:
            cur2 = await db.execute(
                """
                INSERT INTO support_tickets (
                    client_tg_id, client_username, client_name, admin_tg_id, admin_name, status
                ) VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (
                    client_tg_id,
                    (client_username or "").lstrip("@"),
                    client_name or client_username or f"Клиент #{client_tg_id}",
                    admin_tg_id,
                    admin_name,
                ),
            )
            ticket_id = cur2.lastrowid

        await db.commit()
        async with db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)) as f_cur:
            return dict(await f_cur.fetchone())


async def get_client_open_ticket(client_tg_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT * FROM support_tickets
                WHERE client_tg_id = ? AND status IN ('open', 'active')
                ORDER BY id DESC LIMIT 1
                """,
                (client_tg_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_admin_active_ticket(admin_tg_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                """
                SELECT * FROM support_tickets
                WHERE admin_tg_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (admin_tg_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_ticket_by_id(ticket_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def take_support_ticket(
        ticket_id: int, admin_tg_id: Optional[int], admin_name: str
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT status FROM support_tickets WHERE id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row or row[0] == "closed":
                return False
        await db.execute(
            """
            UPDATE support_tickets
            SET status = 'active', admin_tg_id = ?, admin_name = ?
            WHERE id = ?
            """,
            (admin_tg_id, admin_name, ticket_id),
        )
        await db.commit()
        return True


async def close_support_ticket(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE support_tickets SET status = 'closed' WHERE id = ?",
            (ticket_id,),
        )
        await db.commit()


async def add_ticket_message(
        ticket_id: int, sender_role: str, sender_name: str, text: str
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ticket_messages (ticket_id, sender_role, sender_name, text)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, sender_role, sender_name, text),
        )
        await db.commit()


async def get_ticket_messages(ticket_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
                (ticket_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_tickets() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM support_tickets ORDER BY CASE status WHEN 'open' THEN 1 WHEN 'active' THEN 2 ELSE 3 END, id DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]