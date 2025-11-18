import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
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
    accepted_at INTEGER,
    invite_issued INTEGER NOT NULL DEFAULT 0,
    trial_start INTEGER DEFAULT 0,
    trial_end INTEGER DEFAULT 0,
    rebill_id TEXT,
    rebill_parent_payment TEXT,
    customer_key TEXT,
    card_request_key TEXT
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

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    end_at INTEGER,
    updated_at INTEGER,
    rebill_id TEXT,
    customer_key TEXT,
    rebill_parent_payment TEXT
);

CREATE TABLE IF NOT EXISTS coupon_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    used_at INTEGER NOT NULL,
    kind TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coupon_usages_code ON coupon_usages(code);
CREATE INDEX IF NOT EXISTS idx_coupon_usages_user_kind ON coupon_usages(user_id, kind);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    order_id TEXT,
    payment_id TEXT,
    amount INTEGER,
    months INTEGER,
    status TEXT DEFAULT 'PENDING',
    created_at INTEGER,
    method TEXT DEFAULT 'card'
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

CREATE TABLE IF NOT EXISTS payment_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    status TEXT,
    created_at INTEGER,
    message TEXT,
    payment_type TEXT
);
"""

COUPON_CODE_PATTERN = re.compile(r"^[A-Z0-9\-]{4,32}$")


class DB:
    def __init__(self, path: str):
        self.path = path
        self._customer_key_prefix = "customer_registered:"

    @staticmethod
    def _normalize_code(raw: str) -> str:
        """Нормализовать промокод к верхнему регистру без лишних пробелов."""

        return (raw or "").upper().strip()

    @staticmethod
    def _safe_int(value: object) -> int:
        """Попытаться преобразовать значение в int, возвращая 0 при ошибке."""

        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _datetime_to_ts(dt: datetime) -> int:
        """Сконвертировать дату в таймстамп в секундах UTC."""

        if dt.tzinfo is None:
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        return int(dt.timestamp())

    async def _get_subscription_end_internal(
        self, conn: aiosqlite.Connection, user_id: int
    ) -> int:
        """Получить конец подписки для пользователя в рамках открытого соединения."""

        cur = await conn.execute(
            "SELECT end_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return 0
        return self._safe_int(row["end_at"])

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(SCHEMA)
            for ddl in (
                "ALTER TABLE users ADD COLUMN accepted_legal INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN accepted_at INTEGER",
                "ALTER TABLE users ADD COLUMN invite_issued INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN trial_start INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN trial_end INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN rebill_id TEXT",
                "ALTER TABLE users ADD COLUMN rebill_parent_payment TEXT",
                "ALTER TABLE payments ADD COLUMN order_id TEXT",
                "ALTER TABLE users ADD COLUMN customer_key TEXT",
                "ALTER TABLE users ADD COLUMN card_request_key TEXT",
                "CREATE TABLE IF NOT EXISTS payment_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, created_at INTEGER, message TEXT, payment_type TEXT)",
                "ALTER TABLE subscriptions ADD COLUMN rebill_id TEXT",
                "ALTER TABLE subscriptions ADD COLUMN customer_key TEXT",
                "ALTER TABLE subscriptions ADD COLUMN rebill_parent_payment TEXT",
                "ALTER TABLE payment_logs ADD COLUMN payment_type TEXT",
                "ALTER TABLE payments ADD COLUMN method TEXT",
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
                cur = await db.execute(
                    """
                    SELECT user_id, rebill_id, customer_key, rebill_parent_payment
                    FROM users
                    WHERE (rebill_id IS NOT NULL AND TRIM(rebill_id) <> '')
                       OR (customer_key IS NOT NULL AND TRIM(customer_key) <> '')
                       OR (rebill_parent_payment IS NOT NULL AND TRIM(rebill_parent_payment) <> '')
                    """
                )
                rows = await cur.fetchall()

                def _normalize(value: object) -> Optional[str]:
                    if value is None:
                        return None
                    text = str(value).strip()
                    return text or None

                for row in rows:
                    user_id = self._safe_int(row["user_id"])
                    if user_id <= 0:
                        continue
                    rebill_value = _normalize(row["rebill_id"])
                    customer_value = _normalize(row["customer_key"])
                    parent_value = _normalize(row["rebill_parent_payment"])
                    if not any((rebill_value, customer_value, parent_value)):
                        continue
                    stamp = int(time.time())
                    await db.execute(
                        """
                        INSERT INTO subscriptions(user_id, rebill_id, customer_key, rebill_parent_payment, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            rebill_id = COALESCE(excluded.rebill_id, subscriptions.rebill_id),
                            customer_key = COALESCE(excluded.customer_key, subscriptions.customer_key),
                            rebill_parent_payment = COALESCE(excluded.rebill_parent_payment, subscriptions.rebill_parent_payment),
                            updated_at = CASE
                                WHEN excluded.rebill_id IS NOT NULL OR excluded.customer_key IS NOT NULL OR excluded.rebill_parent_payment IS NOT NULL
                                    THEN excluded.updated_at
                                ELSE subscriptions.updated_at
                            END
                        """,
                        (user_id, rebill_value, customer_value, parent_value, stamp),
                    )

                await db.execute(
                    """
                    UPDATE users
                    SET rebill_id=NULL, customer_key=NULL, rebill_parent_payment=NULL
                    WHERE rebill_id IS NOT NULL
                       OR customer_key IS NOT NULL
                       OR rebill_parent_payment IS NOT NULL
                    """
                )
            except Exception as err:  # noqa: BLE001
                logging.exception(
                    "Не удалось перенести идентификаторы автоплатежей в таблицу subscriptions",
                    exc_info=err,
                )
            try:
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_payments_payment_id ON payments(payment_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)"
                )
            except Exception as err:  # noqa: BLE001
                logging.exception("Не удалось создать индексы платежей", exc_info=err)

            try:
                await db.execute(
                    """
                    UPDATE payments
                    SET method='card'
                    WHERE method IS NULL OR TRIM(method)=''
                    """
                )
            except Exception as err:  # noqa: BLE001
                logging.debug("Не удалось нормализовать method в payments", exc_info=err)

            try:
                cur = await db.execute(
                    """
                    SELECT user_id, expires_at, trial_start, trial_end
                    FROM users
                    WHERE expires_at IS NOT NULL AND expires_at > 0
                    """
                )
                rows = await cur.fetchall()
                now_stamp = int(time.time())
                for row in rows:
                    user_id = self._safe_int(row["user_id"])
                    expires_at = self._safe_int(row["expires_at"])
                    if user_id <= 0 or expires_at <= 0:
                        continue
                    pay_cur = await db.execute(
                        "SELECT 1 FROM payments WHERE user_id=? AND UPPER(status)='CONFIRMED' LIMIT 1",
                        (user_id,),
                    )
                    has_payment = await pay_cur.fetchone() is not None
                    if has_payment:
                        await db.execute(
                            """
                            INSERT INTO subscriptions(user_id, end_at, updated_at)
                            VALUES(?, ?, ?)
                            ON CONFLICT(user_id) DO UPDATE SET end_at=excluded.end_at, updated_at=excluded.updated_at
                            """,
                            (user_id, expires_at, now_stamp),
                        )
                        continue
                    trial_cur = await db.execute(
                        "SELECT 1 FROM coupon_usages WHERE user_id=? AND kind=? LIMIT 1",
                        (user_id, "trial"),
                    )
                    has_trial = await trial_cur.fetchone() is not None
                    if has_trial:
                        trial_end_current = (
                            self._safe_int(row["trial_end"]) if "trial_end" in row.keys() else 0
                        )
                        if not trial_end_current:
                            trial_start = (
                                self._safe_int(row["trial_start"]) if "trial_start" in row.keys() else 0
                            )
                            if not trial_start or trial_start > expires_at:
                                trial_start = expires_at
                            await db.execute(
                                "UPDATE users SET trial_start=?, trial_end=? WHERE user_id=?",
                                (trial_start, expires_at, user_id),
                            )
                    else:
                        await db.execute(
                            """
                            INSERT INTO subscriptions(user_id, end_at, updated_at)
                            VALUES(?, ?, ?)
                            ON CONFLICT(user_id) DO UPDATE SET end_at=excluded.end_at, updated_at=excluded.updated_at
                            """,
                            (user_id, expires_at, now_stamp),
                        )
            except Exception as err:  # noqa: BLE001
                logging.exception("Не удалось синхронизировать текущие сроки подписок", exc_info=err)

            try:
                cur = await db.execute(
                    "SELECT code, kind, used_by, used_at FROM coupons WHERE used_by IS NOT NULL AND used_at IS NOT NULL"
                )
                rows = await cur.fetchall()
                for row in rows:
                    if row["used_by"] is None:
                        continue
                    exists_cur = await db.execute(
                        "SELECT 1 FROM coupon_usages WHERE code=? AND user_id=? AND used_at=?",
                        (row["code"], row["used_by"], row["used_at"]),
                    )
                    if await exists_cur.fetchone() is not None:
                        continue
                    await db.execute(
                        "INSERT INTO coupon_usages(code, user_id, used_at, kind) VALUES(?, ?, ?, ?)",
                        (row["code"], row["used_by"], row["used_at"], row["kind"]),
                    )
            except Exception as err:  # noqa: BLE001
                logging.exception(
                    "Не удалось перенести историю использования промокодов", exc_info=err
                )
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.user_id,
                    u.started_at,
                    u.expires_at,
                    u.auto_renew,
                    u.paid_only,
                    u.accepted_legal,
                    u.accepted_at,
                    u.invite_issued,
                    u.trial_start,
                    u.trial_end,
                    COALESCE(s.rebill_id, u.rebill_id) AS rebill_id,
                    COALESCE(s.rebill_parent_payment, u.rebill_parent_payment) AS rebill_parent_payment,
                    COALESCE(s.customer_key, u.customer_key) AS customer_key,
                    u.card_request_key,
                    s.end_at AS subscription_end_at,
                    s.updated_at AS subscription_updated_at
                FROM users AS u
                LEFT JOIN subscriptions AS s ON s.user_id = u.user_id
                WHERE u.user_id=?
                """,
                (user_id,),
            )
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

    async def set_rebill_id(self, user_id: int, rebill_id: str) -> None:
        """Сохранить идентификатор RebillId для пользователя."""

        value = (rebill_id or "").strip() or None
        async with aiosqlite.connect(self.path) as db:
            stamp = int(time.time())
            await db.execute(
                """
                INSERT INTO subscriptions(user_id, rebill_id, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    rebill_id=excluded.rebill_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, value, stamp),
            )
            await db.execute(
                "UPDATE users SET rebill_id=NULL WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def set_rebill_parent_payment(self, user_id: int, payment_id: str) -> None:
        """Запомнить платеж как родительский для автосписаний."""

        value = (payment_id or "").strip() or None
        async with aiosqlite.connect(self.path) as db:
            stamp = int(time.time())
            await db.execute(
                """
                INSERT INTO subscriptions(user_id, rebill_parent_payment, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    rebill_parent_payment=excluded.rebill_parent_payment,
                    updated_at=excluded.updated_at
                """,
                (user_id, value, stamp),
            )
            await db.execute(
                "UPDATE users SET rebill_parent_payment=NULL WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def set_customer_key(self, user_id: int, customer_key: Optional[str]) -> None:
        """Сохранить идентификатор клиента для рекуррентных платежей."""

        value = (customer_key or "").strip() or None
        async with aiosqlite.connect(self.path) as db:
            stamp = int(time.time())
            await db.execute(
                """
                INSERT INTO subscriptions(user_id, customer_key, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    customer_key=excluded.customer_key,
                    updated_at=excluded.updated_at
                """,
                (user_id, value, stamp),
            )
            await db.execute(
                "UPDATE users SET customer_key=NULL WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def set_card_request_key(self, user_id: int, request_key: Optional[str]) -> None:
        """Сохранить или очистить RequestKey для привязки карты."""

        value = (request_key or "").strip() or None
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET card_request_key=? WHERE user_id=?",
                (value, user_id),
            )
            await db.commit()

    async def set_card_id(self, user_id: int, card_id: Optional[str]) -> None:
        """Сохранить идентификатор карты (CardId) в настройках."""

        key = f"card_id:{user_id}"
        value = (card_id or "").strip()
        if value:
            await self.set_setting(key, value)
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM settings WHERE key=?", (key,))
            await db.commit()

    async def get_card_id(self, user_id: int) -> Optional[str]:
        """Прочитать сохранённый CardId пользователя."""

        key = f"card_id:{user_id}"
        value = await self.get_setting(key)
        if value is None:
            return None
        text = value.strip()
        return text or None

    async def log_payment_attempt(
        self,
        user_id: int,
        status: str,
        message: str = "",
        *,
        payment_type: Optional[str] = None,
    ) -> None:
        """Записать результат автоплатежа в журнал с указанием типа платежа."""

        normalized_status = (status or "").strip().upper() or "UNKNOWN"
        note = (message or "").strip()
        stamp = int(time.time())
        normalized_type = "card"
        if payment_type:
            candidate = payment_type.strip().lower()
            if candidate == "sbp":
                normalized_type = "sbp"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO payment_logs(user_id, status, created_at, message, payment_type)
                VALUES(?, ?, ?, ?, ?)
                """,
                (user_id, normalized_status, stamp, note, normalized_type),
            )
            await db.commit()

    async def set_invite_issued(self, user_id: int, flag: bool) -> None:
        """Обновить признак выдачи одноразовой ссылки пользователю."""

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET invite_issued=? WHERE user_id=?",
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

    async def set_customer_registered(self, user_id: int, flag: bool) -> None:
        """Зафиксировать информацию о регистрации клиента в T-Bank."""

        key = f"{self._customer_key_prefix}{user_id}"
        if flag:
            await self.set_setting(key, "1")
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM settings WHERE key=?", (key,))
            await db.commit()

    async def is_customer_registered(self, user_id: int) -> bool:
        """Проверить, регистрировали ли клиента через AddCustomer."""

        key = f"{self._customer_key_prefix}{user_id}"
        value = await self.get_setting(key)
        return value == "1"

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
        *,
        method: Optional[str] = None,
    ) -> None:
        """Сохранить платёж в базе данных."""

        normalized_status = status.upper() if status else "PENDING"
        normalized_method = (method or "card").strip().lower() or "card"
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
                    SET user_id=?, order_id=?, payment_id=?, amount=?, months=?, status=?, method=?
                    WHERE id=?
                    """,
                    (
                        user_id,
                        order_id,
                        payment_id,
                        amount,
                        months,
                        normalized_status,
                        normalized_method,
                        row["id"],
                    ),
                )
            else:
                created_at = int(time.time())
                await db.execute(
                    """
                    INSERT INTO payments (user_id, order_id, payment_id, amount, months, status, created_at, method)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        order_id,
                        payment_id,
                        amount,
                        months,
                        normalized_status,
                        created_at,
                        normalized_method,
                    ),
                )
            await db.commit()

    async def set_payment_method(self, payment_id: str, method: Optional[str]) -> bool:
        """Обновить способ оплаты для конкретного платежа."""

        normalized_method = (method or "").strip().lower()
        if not normalized_method:
            normalized_method = "card"
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE payments SET method=? WHERE payment_id=?",
                (normalized_method, payment_id),
            )
            await db.commit()
            return cur.rowcount > 0

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

    async def get_subscription_end(self, user_id: int) -> Optional[int]:
        """Получить дату окончания платной подписки пользователя."""

        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT end_at FROM subscriptions WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        value = self._safe_int(row["end_at"])
        return value or None

    async def set_subscription_end(self, user_id: int, dt: datetime) -> None:
        """Принудительно обновить дату окончания подписки пользователя."""

        ts = self._datetime_to_ts(dt)
        stamp = int(time.time())
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                """
                INSERT INTO subscriptions(user_id, end_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET end_at=excluded.end_at, updated_at=excluded.updated_at
                """,
                (user_id, ts, stamp),
            )
            cur = await conn.execute(
                "SELECT trial_end, invite_issued FROM users WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is not None and hasattr(row, "keys"):
                trial_end = self._safe_int(row["trial_end"]) if "trial_end" in row.keys() else 0
                new_exp = max(trial_end, ts)
                await conn.execute(
                    "UPDATE users SET expires_at=? WHERE user_id=?",
                    (new_exp, user_id),
                )
            await conn.commit()

    async def set_trial_end(self, user_id: int, dt: datetime) -> None:
        """Принудительно установить окончание пробного периода пользователя."""

        ts = self._datetime_to_ts(dt)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT trial_start FROM users WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.commit()
                return
            trial_start = 0
            if hasattr(row, "keys") and "trial_start" in row.keys():
                trial_start = self._safe_int(row["trial_start"])
            new_start = trial_start if trial_start and trial_start <= ts else ts
            sub_end = await self._get_subscription_end_internal(conn, user_id)
            new_exp = max(ts, sub_end)
            await conn.execute(
                "UPDATE users SET trial_start=?, trial_end=?, expires_at=? WHERE user_id=?",
                (new_start, ts, new_exp, user_id),
            )
            await conn.commit()

    async def extend_subscription(self, user_id: int, months: int) -> None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT invite_issued, trial_end FROM users WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.commit()
                return
            invite_flag = 0
            trial_end = 0
            if hasattr(row, "keys"):
                if "invite_issued" in row.keys():
                    invite_flag = self._safe_int(row["invite_issued"])
                if "trial_end" in row.keys():
                    trial_end = self._safe_int(row["trial_end"])
            now_ts = int(datetime.utcnow().timestamp())
            current_sub_end = await self._get_subscription_end_internal(conn, user_id)
            base = max(current_sub_end, now_ts)
            delta = int(timedelta(days=30 * months).total_seconds())
            new_end = base + delta
            expired_before = current_sub_end <= now_ts
            new_invite_flag = 0 if expired_before else invite_flag
            stamp = int(time.time())
            await conn.execute(
                """
                INSERT INTO subscriptions(user_id, end_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET end_at=excluded.end_at, updated_at=excluded.updated_at
                """,
                (user_id, new_end, stamp),
            )
            new_expires = max(trial_end, new_end)
            await conn.execute(
                "UPDATE users SET invite_issued=?, expires_at=? WHERE user_id=?",
                (new_invite_flag, new_expires, user_id),
            )
            await conn.commit()

    async def list_expired(self, now_ts: int) -> List[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.user_id,
                    u.started_at,
                    u.expires_at,
                    u.auto_renew,
                    u.paid_only,
                    u.accepted_legal,
                    u.accepted_at,
                    u.invite_issued,
                    u.trial_start,
                    u.trial_end,
                    COALESCE(s.rebill_id, u.rebill_id) AS rebill_id,
                    COALESCE(s.rebill_parent_payment, u.rebill_parent_payment) AS rebill_parent_payment,
                    COALESCE(s.customer_key, u.customer_key) AS customer_key,
                    u.card_request_key,
                    s.end_at AS subscription_end_at,
                    s.updated_at AS subscription_updated_at
                FROM users AS u
                LEFT JOIN subscriptions AS s ON s.user_id = u.user_id
                WHERE u.expires_at<?
                """,
                (now_ts,),
            )
            return await cur.fetchall()

    async def use_coupon(self, code: str, user_id: int) -> tuple[bool, str, str | None]:
        """Попытаться применить промокод пользователя."""

        normalized = self._normalize_code(code)
        if not normalized:
            return False, "Промокод не должен быть пустым.", None

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM coupons WHERE code=?", (normalized,))
            row = await cur.fetchone()
            if row is None:
                return False, "Промокод недействителен или истёк.", None

            now_ts = int(time.time())
            keys = set(row.keys())
            expires_raw = None
            for candidate in ("expires_at", "valid_until", "valid_till"):
                if candidate in keys:
                    expires_raw = row[candidate]
                    break
            expires_at: Optional[int] = None
            if expires_raw not in (None, ""):
                try:
                    expires_at = int(expires_raw)
                except (TypeError, ValueError):
                    logging.warning("Некорректное значение срока действия промокода %s: %s", normalized, expires_raw)
            if expires_at is not None and expires_at < now_ts:
                return False, "Промокод недействителен или истёк.", row["kind"]

            kind = row["kind"]
            await db.execute(
                "UPDATE coupons SET used_by=NULL, used_at=? WHERE code=?",
                (now_ts, normalized),
            )
            await db.execute(
                "INSERT INTO coupon_usages(code, user_id, used_at, kind) VALUES(?, ?, ?, ?)",
                (normalized, user_id, now_ts, kind),
            )
            # Здесь можно расширить логику для разных типов купонов (скидки, бонусы и т.д.)
            await db.commit()

        logging.info("Промокод %s применён пользователем %s как тип %s", normalized, user_id, kind)
        return True, "Промокод успешно активирован.", kind

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
