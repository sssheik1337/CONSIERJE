from dataclasses import dataclass, field
import os
from typing import Optional

from dotenv import load_dotenv

# Загружаем значения из файла окружения, явно переопределяя системные
load_dotenv(override=True)

# URL для уведомлений от T-Bank
TINKOFF_NOTIFY_URL: str = os.getenv("TINKOFF_NOTIFY_URL", "")


def _parse_ids(raw: str) -> set[int]:
    """Разбираем список идентификаторов администраторов."""

    return {int(part.strip()) for part in raw.split(",") if part.strip()} if raw else set()


def _env_int(name: str, default: int) -> int:
    """Считать целое значение из окружения с запасным вариантом."""

    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Считать число с плавающей точкой из окружения."""

    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _optional_env(name: str) -> Optional[str]:
    """Вернуть строку из окружения или None, если значение пустое."""

    raw = os.getenv(name)
    if not raw:
        return None
    return raw


@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    SUPER_ADMIN_IDS: set[int] = field(
        default_factory=lambda: _parse_ids(os.getenv("SUPER_ADMIN_IDS", ""))
    )
    ADMIN_LOGIN: str = os.getenv("ADMIN_LOGIN", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
    ADMIN_AUTH_FILE: str = os.getenv("ADMIN_AUTH_FILE", "./admins.json")

    DB_PATH: str = os.getenv("DB_PATH", "./concierge.sqlite3")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

    T_PAY_BASE_URL: str = os.getenv("T_PAY_BASE_URL", "https://securepay.tinkoff.ru/v2")
    T_PAY_TERMINAL_KEY: str = os.getenv("T_PAY_TERMINAL_KEY", "")
    T_PAY_PASSWORD: str = os.getenv("T_PAY_PASSWORD", "")
    T_PAY_SUCCESS_URL: str = os.getenv("T_PAY_SUCCESS_URL", "")
    T_PAY_FAIL_URL: str = os.getenv("T_PAY_FAIL_URL", "")
    T_PAY_API_TOKEN: str = os.getenv("T_PAY_API_TOKEN", "")

    LOG_LEVEL: str = (os.getenv("LOG_LEVEL") or "INFO").strip() or "INFO"
    LOG_PATH: str = (os.getenv("LOG_PATH") or "./payments.log").strip() or "./payments.log"

    TEST_RENEW_INTERVAL_MINUTES: Optional[int] = field(
        default_factory=lambda: _env_int("TEST_RENEW_INTERVAL_MINUTES", 0) or None
    )
    SBP_TEST_INTERVAL_MINUTES: Optional[int] = field(
        default_factory=lambda: _env_int("SBP_TEST_INTERVAL_MINUTES", 0) or None
    )
    BROADCAST_DELAY_SECONDS: float = field(
        default_factory=lambda: _env_float("BROADCAST_DELAY_SECONDS", 0.1)
    )

    TINKOFF_NOTIFY_URL: str = TINKOFF_NOTIFY_URL
    WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    WEBHOOK_PORT: int = _env_int("WEBHOOK_PORT", 8000)
    TINKOFF_WEBHOOK_SECRET: Optional[str] = _optional_env("TINKOFF_WEBHOOK_SECRET")


config = Config()
