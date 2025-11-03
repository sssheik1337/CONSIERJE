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

    DOCS_NEWSLETTER_URL: str = os.getenv("DOCS_NEWSLETTER_URL", "")
    DOCS_PD_CONSENT_URL: str = os.getenv("DOCS_PD_CONSENT_URL", "")
    DOCS_PD_POLICY_URL: str = os.getenv("DOCS_PD_POLICY_URL", "")
    DOCS_OFFER_URL: str = os.getenv("DOCS_OFFER_URL", "")


config = Config()


def get_docs_map() -> dict[str, str]:
    """Вернуть словарь ссылок на документы."""

    return {
        "newsletter": config.DOCS_NEWSLETTER_URL,
        "pd_consent": config.DOCS_PD_CONSENT_URL,
        "pd_policy": config.DOCS_PD_POLICY_URL,
        "offer": config.DOCS_OFFER_URL,
    }
