import aiosqlite
import json
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any

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
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    per_user_limit INTEGER NOT NULL DEFAULT 1,
    trial_days INTEGER,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS promo_redemptions (
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    last_redeemed_at INTEGER,
    PRIMARY KEY (code, user_id),
    FOREIGN KEY (code) REFERENCES promo_codes(code) ON DELETE CASCADE
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

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Универсальный геттер настроек."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
        if row:
            return row["value"]
        if default is not None:
            await self.set_setting(key, default)
        return default

    async def set_setting(self, key: str, value: Optional[str]):
        """Универсальный сеттер настроек."""
        async with aiosqlite.connect(self.path) as db:
            if value is None:
                await db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                await db.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            await db.commit()

    async def get_target_chat_username(self) -> Optional[str]:
        value = await self.get_setting("target_chat_username")
        return value

    async def set_target_chat_username(self, username: Optional[str]):
        await self.set_setting("target_chat_username", username)

    async def get_target_chat_id(self) -> Optional[int]:
        value = await self.get_setting("target_chat_id")
        return int(value) if value is not None else None

    async def set_target_chat_id(self, chat_id: Optional[int]):
        await self.set_setting("target_chat_id", str(chat_id) if chat_id is not None else None)

    async def get_prices(self, default_prices: Dict[int, int]) -> Dict[int, int]:
        """Получить прайс-лист (ключи/значения — целые числа)."""
        default_json = json.dumps(default_prices)
        raw = await self.get_setting("prices", default_json)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return default_prices
        cleaned: Dict[int, int] = {}
        for key, value in data.items():
            try:
                cleaned[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return cleaned

    async def set_prices(self, prices: Dict[int, int]):
        await self.set_setting("prices", json.dumps(prices))

    async def get_trial_days(self, default_days: int) -> int:
        value = await self.get_setting("trial_days", str(default_days))
        try:
            return int(value) if value is not None else default_days
        except (TypeError, ValueError):
            return default_days

    async def set_trial_days(self, days: int):
        await self.set_setting("trial_days", str(days))

    async def get_auto_renew_default(self, default_flag: bool) -> bool:
        raw_default = "1" if default_flag else "0"
        value = await self.get_setting("auto_renew_default", raw_default)
        return str(value) in {"1", "true", "True"}

    async def set_auto_renew_default(self, flag: bool):
        await self.set_setting("auto_renew_default", "1" if flag else "0")

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

    async def grant_trial_days(
        self,
        user_id: int,
        days: int,
        now_ts: int,
        auto_renew_default: bool,
        bypass: bool,
    ):
        """Выдать или продлить пробный период на указанное количество дней."""

        delta = int(timedelta(days=days).total_seconds())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT expires_at, bypass FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            if row:
                current_exp = row["expires_at"] or now_ts
                new_exp = max(current_exp, now_ts) + delta
                await db.execute(
                    "UPDATE users SET expires_at=?, paid_only=0 WHERE user_id=?",
                    (new_exp, user_id),
                )
            else:
                started_at = now_ts
                expires_at = now_ts + delta
                await db.execute(
                    "INSERT INTO users(user_id, started_at, expires_at, auto_renew, paid_only, bypass) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        started_at,
                        expires_at,
                        1 if auto_renew_default else 0,
                        0,
                        1 if bypass else 0,
                    ),
                )
            await db.commit()

    async def create_promo_code(
        self,
        code: str,
        code_type: str,
        expires_at: Optional[int],
        max_uses: Optional[int],
        per_user_limit: int,
        trial_days: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        """Создать промокод с заданными параметрами."""

        normalized_code = code.strip().upper()
        if not normalized_code:
            raise ValueError("Код не может быть пустым.")
        if per_user_limit <= 0:
            raise ValueError("Лимит на пользователя должен быть положительным.")
        payload_json = json.dumps(payload) if payload else None
        created_at = int(datetime.utcnow().timestamp())
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO promo_codes(code, code_type, created_at, expires_at, max_uses, used_count, per_user_limit, trial_days, payload)
                    VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        normalized_code,
                        code_type,
                        created_at,
                        expires_at,
                        max_uses,
                        per_user_limit,
                        trial_days,
                        payload_json,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as err:
                raise ValueError("Такой промокод уже существует.") from err

    async def redeem_promo_code(
        self, code: str, user_id: int, now_ts: int
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Погасить промокод, проверяя сроки и лимиты."""

        normalized_code = code.strip().upper()
        if not normalized_code:
            return False, "Нужно указать промокод.", None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            cur = await db.execute(
                "SELECT * FROM promo_codes WHERE code=?", (normalized_code,)
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "Такого промокода нет.", None
            expires_at = row["expires_at"]
            if expires_at and expires_at < now_ts:
                await db.rollback()
                return False, "Срок действия промокода истёк.", None
            max_uses = row["max_uses"]
            used_count = row["used_count"] or 0
            if max_uses is not None and max_uses > 0 and used_count >= max_uses:
                await db.rollback()
                return False, "Промокод уже исчерпан.", None
            per_user_limit = row["per_user_limit"] or 0
            cur_usage = await db.execute(
                "SELECT uses FROM promo_redemptions WHERE code=? AND user_id=?",
                (normalized_code, user_id),
            )
            usage_row = await cur_usage.fetchone()
            if usage_row and per_user_limit and usage_row["uses"] >= per_user_limit:
                await db.rollback()
                return False, "Вы уже использовали этот промокод.", None
            if usage_row:
                new_uses = usage_row["uses"] + 1
                await db.execute(
                    "UPDATE promo_redemptions SET uses=?, last_redeemed_at=? WHERE code=? AND user_id=?",
                    (new_uses, now_ts, normalized_code, user_id),
                )
            else:
                await db.execute(
                    "INSERT INTO promo_redemptions(code, user_id, uses, last_redeemed_at) VALUES(?, ?, 1, ?)",
                    (normalized_code, user_id, now_ts),
                )
            await db.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?",
                (normalized_code,),
            )
            await db.commit()
        payload_raw = row["payload"]
        payload: Dict[str, Any] = {}
        if payload_raw:
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {}
        result: Dict[str, Any] = {
            "code": row["code"],
            "code_type": row["code_type"],
            "expires_at": expires_at,
            "max_uses": max_uses,
            "used_count": used_count + 1,
            "per_user_limit": per_user_limit,
            "trial_days": row["trial_days"],
            "payload": payload,
        }
        return True, "", result
