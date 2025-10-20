from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _parse_ids(s: str) -> set[int]:
    return {int(x.strip()) for x in s.split(",") if x.strip()} if s else set()

def _parse_prices(s: str) -> dict[int, int]:
    # формат "1:399,2:699"
    out = {}
    if not s:
        return out
    for part in s.split(","):
        m, p = part.split(":")
        out[int(m.strip())] = int(p.strip())
    return out

@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    TARGET_CHAT_ID: int = int(os.getenv("TARGET_CHAT_ID", "0"))
    SUPER_ADMIN_IDS: set[int] = _parse_ids(os.getenv("SUPER_ADMIN_IDS", ""))
    TRIAL_DAYS: int = int(os.getenv("TRIAL_DAYS", "3"))
    AUTO_RENEW_DEFAULT: bool = os.getenv("AUTO_RENEW_DEFAULT", "false").lower() == "true"
    PRICES: dict[int, int] = _parse_prices(os.getenv("PRICES", "1:399"))
    PAID_ONLY_IDS: set[int] = _parse_ids(os.getenv("PAID_ONLY_IDS", ""))
    ADMIN_BYPASS_IDS: set[int] = _parse_ids(os.getenv("ADMIN_BYPASS_IDS", ""))
    DB_PATH: str = os.getenv("DB_PATH", "./concierge.sqlite3")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

config = Config()
