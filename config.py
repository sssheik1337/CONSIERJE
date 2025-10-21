from dataclasses import dataclass, field
import os
from dotenv import load_dotenv

# Загружаем значения из файла окружения
load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    """Разбираем список идентификаторов администраторов."""

    return {int(part.strip()) for part in raw.split(",") if part.strip()} if raw else set()

@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    SUPER_ADMIN_IDS: set[int] = field(
        default_factory=lambda: _parse_ids(os.getenv("SUPER_ADMIN_IDS", ""))
    )

    DB_PATH: str = os.getenv("DB_PATH", "./concierge.sqlite3")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")


config = Config()
