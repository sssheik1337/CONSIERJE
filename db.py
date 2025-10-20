import secrets
import sqlite3
import string
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    started_at INTEGER,            -- unix ts начала (для пробника)
    expires_at INTEGER,            -- unix ts конца подписки
    auto_renew INTEGER NOT NULL DEFAULT 0,
    paid_only INTEGER NOT NULL DEFAULT 0,  -- 1 = без пробного
    bypass INTEGER NOT NULL DEFAULT 0      -- 1 = не кикать (вайтлист)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    code_type TEXT NOT NULL,
    expires_at INTEGER,
    usage_limit INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    is_used INTEGER NOT NULL DEFAULT 0,
    extension_days INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    redeemed_by INTEGER,
    redeemed_at INTEGER
);
"""

class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
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
        bypass: bool = False,
    ):
        u = await self.get_user(user_id)
        if u:
            return
        started_at = now_ts
        if paid_only:
            # без пробного — сразу подписка на месяц (заглушка до оплаты)
            expires_at = now_ts + int(timedelta(days=30).total_seconds())
        else:
            expires_at = now_ts + int(timedelta(days=trial_days).total_seconds())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users(user_id, started_at, expires_at, auto_renew, paid_only, bypass) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, started_at, expires_at, 1 if auto_renew_default else 0, 1 if paid_only else 0, 1 if bypass else 0),
            )
            await db.commit()

    async def set_bypass(self, user_id: int, bypass: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET bypass=? WHERE user_id=?", (1 if bypass else 0, user_id))
            await db.commit()

    async def set_paid_only(self, user_id: int, paid_only: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET paid_only=? WHERE user_id=?", (1 if paid_only else 0, user_id))
            await db.commit()

    async def set_auto_renew(self, user_id: int, flag: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET auto_renew=? WHERE user_id=?", (1 if flag else 0, user_id))
            await db.commit()

    async def set_trial_days_global(self, days: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO settings(key, value) VALUES('trial_days', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))
            await db.commit()

    async def get_trial_days_global(self, default_days: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key='trial_days'")
            row = await cur.fetchone()
            return int(row["value"]) if row else default_days

    async def generate_promo_codes(
        self,
        code_type: str,
        amount: int,
        extension_days: int,
        expires_at: Optional[int] = None,
        usage_limit: int = 1,
    ) -> List[str]:
        """Создать указанное количество промокодов и вернуть список их значений."""
        alphabet = string.ascii_uppercase + string.digits
        codes: List[str] = []
        async with aiosqlite.connect(self.path) as db:
            for _ in range(amount):
                while True:
                    code = "".join(secrets.choice(alphabet) for _ in range(10))
                    try:
                        await db.execute(
                            """
                            INSERT INTO promo_codes(code, code_type, expires_at, usage_limit, extension_days)
                            VALUES(?, ?, ?, ?, ?)
                            """,
                            (code, code_type, expires_at, usage_limit, extension_days),
                        )
                        codes.append(code)
                        break
                    except sqlite3.IntegrityError:
                        # Повторим генерацию, если код уже существует
                        continue
            await db.commit()
        return codes

    async def get_promo_code(self, code: str) -> Optional[aiosqlite.Row]:
        """Получить промокод без проверки ограничений."""
        normalized = code.upper()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM promo_codes WHERE code=?", (normalized,))
            return await cur.fetchone()

    async def validate_promo_code(self, code: str, now_ts: int) -> Optional[aiosqlite.Row]:
        """Проверить ограничения промокода и вернуть запись, если её можно использовать."""
        promo = await self.get_promo_code(code)
        if not promo:
            return None
        if promo["is_used"]:
            return None
        if promo["expires_at"] and promo["expires_at"] < now_ts:
            return None
        if promo["usage_limit"] is not None and promo["used_count"] >= promo["usage_limit"]:
            return None
        return promo

    async def mark_promo_redeemed(self, code: str, user_id: int, now_ts: int):
        """Отметить промокод как использованный конкретным пользователем."""
        normalized = code.upper()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE promo_codes
                SET used_count = used_count + 1,
                    is_used = CASE WHEN used_count + 1 >= usage_limit THEN 1 ELSE 0 END,
                    redeemed_by = ?,
                    redeemed_at = ?
                WHERE code=?
                """,
                (user_id, now_ts, normalized),
            )
            await db.commit()

    async def set_trial_period(
        self,
        user_id: int,
        now_ts: int,
        trial_days: int,
        auto_renew_default: bool,
    ):
        """Назначить или обновить пробный период пользователю."""
        expires_at = now_ts + int(timedelta(days=trial_days).total_seconds())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            if row:
                await db.execute(
                    """
                    UPDATE users
                    SET started_at=?, expires_at=?, auto_renew=?, paid_only=0
                    WHERE user_id=?
                    """,
                    (now_ts, expires_at, row["auto_renew"], user_id),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO users(user_id, started_at, expires_at, auto_renew, paid_only, bypass)
                    VALUES (?, ?, ?, ?, 0, 0)
                    """,
                    (user_id, now_ts, expires_at, 1 if auto_renew_default else 0),
                )
            await db.commit()

    async def extend_subscription(self, user_id: int, months: int):
        # продлить от текущего expires_at, не от now
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            if not row:
                return
            expires_at = row["expires_at"]
            delta = int(timedelta(days=30*months).total_seconds())
            new_exp = max(expires_at, int(datetime.utcnow().timestamp())) + delta
            await db.execute("UPDATE users SET expires_at=? WHERE user_id=?", (new_exp, user_id))
            await db.commit()

    async def list_expired(self, now_ts: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE expires_at<? AND bypass=0", (now_ts,))
            return await cur.fetchall()
