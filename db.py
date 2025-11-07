import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    started_at INTEGER,
    expires_at INTEGER,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    paid_only INTEGER NOT NULL DEFAULT 0,
    accepted_legal INTEGER NOT NULL DEFAULT 0,
    accepted_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    months INTEGER PRIMARY KEY,
    price  INTEGER NOT NULL CHECK(price>=0),
    CHECK(months>=1)
);

CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    used_by INTEGER,
    used_at INTEGER
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    order_id TEXT,
    payment_id TEXT,
    amount INTEGER,
    months INTEGER,
    status TEXT DEFAULT 'PENDING',
    created_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_payments_payment_id ON payments(payment_id);
CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT,
    order_id TEXT,
    status TEXT,
    terminal_key TEXT,
    raw_json TEXT,
    headers_json TEXT,
    received_at INTEGER,
    processed INTEGER DEFAULT 0
);
"""

COUPON_CODE_PATTERN = re.compile(r"^[A-Z0-9\-]{4,32}$")


class DB:
    def __init__(self, path: str):
        self.path = path

    @staticmethod
    def _normalize_code(raw: str) -> str:
        """Нормализовать промокод к верхнему регистру без лишних пробелов."""

        return (raw or "").upper().strip()

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            for ddl in (
                "ALTER TABLE users ADD COLUMN accepted_legal INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN accepted_at INTEGER",
                "ALTER TABLE payments ADD COLUMN order_id TEXT",
            ):
                try:
                    await db.execute(ddl)
                except aiosqlite.OperationalError as err:
                    message = str(err).lower()
                    if "duplicate column name" in message:
                        continue
                    logging.exception("Ошибка при миграции таблиц", exc_info=err)
                except Exception as err:  # noqa: BLE001
                    logging.exception("Не удалось обновить схему базы данных", exc_info=err)
            try:
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_payments_payment_id ON payments(payment_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)"
                )
            except Exception as err:  # noqa: BLE001
                logging.exception("Не удалось создать индексы платежей", exc_info=err)
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            return await cur.fetchone()

    async def upsert_user(
        self,
        user_id: int,
        now_ts: int,
        trial_days: int,
        auto_renew_default: bool,
        paid_only: bool,
    ) -> None:
        existing = await self.get_user(user_id)
        if existing:
            return

        started_at = now_ts
        if paid_only:
            expires_at = 0
        else:
            expires_at = now_ts + int(timedelta(days=trial_days).total_seconds())

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users(user_id, started_at, expires_at, auto_renew, paid_only)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    started_at,
                    expires_at,
                    1 if auto_renew_default else 0,
                    1 if paid_only else 0,
                ),
            )
            await db.commit()

    async def set_paid_only(self, user_id: int, paid_only: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET paid_only=? WHERE user_id=?",
                (1 if paid_only else 0, user_id),
            )
            await db.commit()

    async def set_auto_renew(self, user_id: int, flag: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET auto_renew=? WHERE user_id=?",
                (1 if flag else 0, user_id),
            )
            await db.commit()

    async def set_accepted_legal(
        self, user_id: int, flag: bool, ts: Optional[int] = None
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            if flag:
                ts_value = ts if ts is not None else int(datetime.utcnow().timestamp())
                await db.execute(
                    "UPDATE users SET accepted_legal=1, accepted_at=? WHERE user_id=?",
                    (ts_value, user_id),
                )
            else:
                await db.execute(
                    "UPDATE users SET accepted_legal=0, accepted_at=NULL WHERE user_id=?",
                    (user_id,),
                )
            await db.commit()

    async def has_accepted_legal(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT accepted_legal FROM users WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return False
        return bool(row["accepted_legal"])

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
        if row is None:
            return None
        return row["value"]

    async def set_target_chat_username(self, name: str) -> None:
        await self.set_setting("target_chat_username", name)

    async def get_target_chat_username(self) -> Optional[str]:
        return await self.get_setting("target_chat_username")

    async def set_target_chat_id(self, chat_id: int) -> None:
        await self.set_setting("target_chat_id", str(chat_id))

    async def get_target_chat_id(self) -> Optional[int]:
        value = await self.get_setting("target_chat_id")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    async def get_all_prices(self) -> List[Tuple[int, int]]:
        """Получить все тарифы, выполняя мягкую миграцию из настроек при необходимости."""

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT months, price FROM prices ORDER BY months ASC"
            )
            rows = await cur.fetchall()
        if rows:
            return [(int(row["months"]), int(row["price"])) for row in rows]

        raw = await self.get_setting("prices")
        if raw is None:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        entries: List[Tuple[int, int]] = []
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                try:
                    months = int(key)
                    price = int(value)
                except (TypeError, ValueError):
                    continue
                if months < 1 or price < 0:
                    continue
                entries.append((months, price))
        if not entries:
            return []
        entries.sort(key=lambda item: item[0])
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO prices(months, price) VALUES(?, ?)", entries
            )
            await db.execute("DELETE FROM settings WHERE key=?", ("prices",))
            await db.commit()
        return entries

    async def upsert_price(self, months: int, price: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO prices(months, price) VALUES(?, ?)",
                (months, price),
            )
            await db.commit()

    async def delete_price(self, months: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM prices WHERE months=?", (months,))
            await db.commit()
            return cur.rowcount > 0

    async def get_prices_dict(self) -> dict[int, int]:
        prices = await self.get_all_prices()
        return {months: price for months, price in prices}

    async def add_payment(
        self,
        user_id: int,
        payment_id: str,
        order_id: str,
        amount: int,
        months: int,
        status: str = "PENDING",
    ) -> None:
        """Сохранить платёж в базе данных."""

        normalized_status = status.upper() if status else "PENDING"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM payments WHERE payment_id=? OR order_id=?",
                (payment_id, order_id),
            )
            row = await cur.fetchone()
            if row:
                await db.execute(
                    """
                    UPDATE payments
                    SET user_id=?, order_id=?, payment_id=?, amount=?, months=?, status=?
                    WHERE id=?
                    """,
                    (user_id, order_id, payment_id, amount, months, normalized_status, row["id"]),
                )
            else:
                created_at = int(time.time())
                await db.execute(
                    """
                    INSERT INTO payments (user_id, order_id, payment_id, amount, months, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        order_id,
                        payment_id,
                        amount,
                        months,
                        normalized_status,
                        created_at,
                    ),
                )
            await db.commit()

    async def set_payment_status(self, payment_id: str, status: str) -> bool:
        """Обновить статус платежа."""

        normalized_status = status.upper() if status else ""
        if not normalized_status:
            return False
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE payments SET status=? WHERE payment_id=?",
                (normalized_status, payment_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_payment_by_payment_id(
        self, payment_id: str
    ) -> Optional[aiosqlite.Row]:
        """Получить платёж по идентификатору PaymentId."""

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM payments WHERE payment_id=?",
                (payment_id,),
            )
            return await cur.fetchone()

    async def get_payment_by_order_id(self, order_id: str) -> Optional[aiosqlite.Row]:
        """Получить платёж по идентификатору заказа мерчанта."""

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM payments WHERE order_id=?",
                (order_id,),
            )
            return await cur.fetchone()

    async def get_payment_by_id(self, payment_id: str) -> Optional[aiosqlite.Row]:
        """Обратная совместимость: вернуть платёж по PaymentId."""

        return await self.get_payment_by_payment_id(payment_id)

    async def get_latest_payment(
        self, user_id: int, status: Optional[str] = None
    ) -> Optional[aiosqlite.Row]:
        """Вернуть последний платёж пользователя, опционально по статусу."""

        query = "SELECT * FROM payments WHERE user_id=?"
        params: List[object] = [user_id]
        if status:
            query += " AND status=?"
            params.append(status.upper())
        query += " ORDER BY created_at DESC LIMIT 1"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(query, params)
            return await cur.fetchone()

    async def log_webhook_event(
        self,
        payment_id: str,
        order_id: str,
        status: str,
        terminal_key: str,
        raw: dict,
        headers: dict,
        received_at: int,
        processed: int = 0,
    ) -> int:
        """Сохранить событие вебхука и вернуть его идентификатор."""

        raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        headers_json = json.dumps(headers, ensure_ascii=False, separators=(",", ":"))
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                INSERT INTO webhook_events (
                    payment_id, order_id, status, terminal_key, raw_json, headers_json, received_at, processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id or "",
                    order_id or "",
                    status.upper() if status else "",
                    terminal_key or "",
                    raw_json,
                    headers_json,
                    received_at,
                    processed,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def mark_webhook_processed(self, event_id: int) -> bool:
        """Отметить событие вебхука как обработанное."""

        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE webhook_events SET processed=1 WHERE id=?",
                (event_id,),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_trial_days_global(self, days: int) -> None:
        await self.set_setting("trial_days", str(days))

    async def get_trial_days_global(self, default: int) -> int:
        value = await self.get_setting("trial_days")
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def set_auto_renew_default(self, flag: bool) -> None:
        await self.set_setting("auto_renew_default", "1" if flag else "0")

    async def get_auto_renew_default(self, fallback: bool) -> bool:
        value = await self.get_setting("auto_renew_default")
        if value is None:
            return fallback
        return value in {"1", "true", "True", "TRUE"}

    async def extend_subscription(self, user_id: int, months: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            if row is None:
                return
            expires_at = row["expires_at"] or 0
            now_ts = int(datetime.utcnow().timestamp())
            delta = int(timedelta(days=30 * months).total_seconds())
            new_exp = max(expires_at, now_ts) + delta
            await db.execute(
                "UPDATE users SET expires_at=? WHERE user_id=?",
                (new_exp, user_id),
            )
            await db.commit()

    async def list_expired(self, now_ts: int) -> List[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE expires_at<?", (now_ts,))
            return await cur.fetchall()

    async def gen_coupons(self, kind: str, count: int) -> List[str]:
        if count <= 0:
            return []
        codes: List[str] = []
        async with aiosqlite.connect(self.path) as db:
            while len(codes) < count:
                code = secrets.token_urlsafe(8)
                try:
                    await db.execute(
                        "INSERT INTO coupons(code, kind, used_by, used_at) VALUES(?, ?, NULL, NULL)",
                        (code, kind),
                    )
                    codes.append(code)
                except aiosqlite.IntegrityError:
                    continue
            await db.commit()
        return codes

    async def create_coupon(self, code: str, kind: str) -> Tuple[bool, str]:
        """Создать ручной промокод, проходя валидацию и проверку на уникальность."""

        normalized = self._normalize_code(code)
        if not normalized or not COUPON_CODE_PATTERN.match(normalized):
            return False, "Промокод должен состоять из 4-32 символов (A-Z, 0-9, -)"

        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO coupons(code, kind, used_by, used_at) VALUES(?, ?, NULL, NULL)",
                    (normalized, kind),
                )
                await db.commit()
                return True, normalized
            except aiosqlite.IntegrityError:
                return False, "Такой промокод уже существует"
