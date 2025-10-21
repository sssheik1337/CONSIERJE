import json
import re
import secrets
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
    paid_only INTEGER NOT NULL DEFAULT 0
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
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[Tuple]:
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
        if not normalized:
            return False, "Промокод не должен быть пустым"
        if kind != "trial":
            return False, "Поддерживается только пробный промокод"
        if not COUPON_CODE_PATTERN.fullmatch(normalized):
            return False, "Разрешены латиница, цифры и дефис (4–32 символа)"
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO coupons(code, kind, used_by, used_at) VALUES(?, ?, NULL, NULL)",
                    (normalized, kind),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                return False, "Код уже существует"
        return True, normalized

    async def use_coupon(self, code: str, user_id: int) -> Tuple[bool, str, Optional[str]]:
        normalized = self._normalize_code(code)
        if not normalized:
            return False, "Нужно указать промокод.", None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT code, kind, used_by FROM coupons WHERE code=?",
                (normalized,),
            )
            row = await cur.fetchone()
            if row is None:
                return False, "Промокод не найден.", None
            if row["used_by"] is not None:
                return False, "Промокод уже использован.", row["kind"]
            now_ts = int(datetime.utcnow().timestamp())
            await db.execute(
                "UPDATE coupons SET used_by=?, used_at=? WHERE code=?",
                (user_id, now_ts, normalized),
            )
            await db.commit()
        return True, "Промокод успешно применён.", row["kind"]
