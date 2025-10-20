import json
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional, Tuple

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

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row["value"] if row else None

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
        await self.set_setting("trial_days", str(days))

    async def get_trial_days_global(self, default_days: int) -> int:
        value = await self.get_setting("trial_days")
        return int(value) if value is not None else default_days

    async def set_prices(self, prices: dict[int, int]):
        packed = json.dumps(prices)
        await self.set_setting("prices", packed)

    async def get_prices(self, default_prices: dict[int, int]) -> dict[int, int]:
        value = await self.get_setting("prices")
        if value is None:
            return default_prices
        try:
            unpacked = json.loads(value)
            return {int(k): int(v) for k, v in unpacked.items()}
        except (ValueError, json.JSONDecodeError):
            return default_prices

    async def set_auto_renew_default(self, flag: bool):
        await self.set_setting("auto_renew_default", "1" if flag else "0")

    async def get_auto_renew_default(self, default_flag: bool) -> bool:
        value = await self.get_setting("auto_renew_default")
        if value is None:
            return default_flag
        return value == "1"

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
