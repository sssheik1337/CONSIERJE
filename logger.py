"""Инициализация централизованного логгера проекта."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

logger = logging.getLogger("concierge")
logger.setLevel(_LEVELS.get(_LOG_LEVEL, logging.INFO))
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    )
    logger.addHandler(handler)

__all__ = ["logger"]
