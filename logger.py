"""Инициализация централизованного логгера проекта."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_TARGET_LEVEL = _LEVELS.get(_LOG_LEVEL, logging.INFO)

root_logger = logging.getLogger()
root_logger.setLevel(_TARGET_LEVEL)

if not root_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    root_logger.addHandler(handler)
    file_handler = logging.FileHandler("payments.log")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root_logger.addHandler(file_handler)
else:
    formatter = logging.Formatter(_FORMAT)
    for handler in root_logger.handlers:
        handler.setLevel(_TARGET_LEVEL)
        handler.setFormatter(formatter)
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None)
               and os.path.basename(h.baseFilename) == "payments.log" for h in root_logger.handlers):
        file_handler = logging.FileHandler("payments.log")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

logger = logging.getLogger("concierge")
logger.setLevel(_TARGET_LEVEL)

__all__ = ["logger"]
