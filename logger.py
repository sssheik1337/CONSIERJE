"""Инициализация централизованного логгера проекта."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

_LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
_LOG_PATH = os.getenv("LOG_PATH", "./payments.log") or "./payments.log"
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
formatter = logging.Formatter(_FORMAT)

console_present = False
file_present = False
handlers_to_remove: list[logging.Handler] = []
for handler in list(root_logger.handlers):
    handler.setLevel(_TARGET_LEVEL)
    handler.setFormatter(formatter)
    if isinstance(handler, logging.FileHandler):
        current_path = getattr(handler, "baseFilename", None)
        if current_path and os.path.abspath(current_path) == os.path.abspath(_LOG_PATH):
            file_present = True
        else:
            handlers_to_remove.append(handler)
    elif isinstance(handler, logging.StreamHandler):
        console_present = True

for handler in handlers_to_remove:
    root_logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:  # noqa: BLE001
        pass

if not console_present:
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(_TARGET_LEVEL)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

if not file_present:
    file_handler = logging.FileHandler(_LOG_PATH)
    file_handler.setLevel(_TARGET_LEVEL)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

logger = logging.getLogger("concierge")
logger.setLevel(_TARGET_LEVEL)

__all__ = ["logger"]
