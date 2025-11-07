from __future__ import annotations

import logging
import time
from typing import Optional

from config import TINKOFF_NOTIFY_URL, config
from db import DB
from t_pay import get_payment_state, init_payment

logger = logging.getLogger(__name__)

_payment_db: Optional[DB] = None


def set_db(database: DB) -> None:
    """Задать экземпляр базы данных для работы с платежами."""

    global _payment_db
    _payment_db = database


def _get_db() -> DB:
    """Вернуть используемую базу данных."""

    if _payment_db is not None:
        return _payment_db
    return DB(config.DB_PATH)


async def create_payment(user_id: int, amount: int, months: int) -> str:
    """Создать платёж через T-Bank и вернуть ссылку на оплату."""

    if amount <= 0:
        raise ValueError("Сумма платежа должна быть положительной")

    order_id = f"{user_id}_{months}_{int(time.time())}"
    description = f"Подписка на {months} мес. для пользователя {user_id}"
    success_url = config.T_PAY_SUCCESS_URL or None
    fail_url = config.T_PAY_FAIL_URL or None
    notify_url = TINKOFF_NOTIFY_URL or None

    try:
        response = await init_payment(
            amount=amount * 100,
            order_id=order_id,
            description=description,
            success_url=success_url,
            fail_url=fail_url,
            notification_url=notify_url,
            extra={"user_id": str(user_id), "months": str(months)},
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось вызвать T-Bank Init", exc_info=err)
        raise RuntimeError("Не удалось создать платёж через T-Bank") from err

    if not response:
        raise RuntimeError("Не удалось создать платёж через T-Bank")

    payment_url = response.get("PaymentURL")
    payment_id = response.get("PaymentId")
    status = (response.get("Status") or "PENDING").upper()
    success_flag = response.get("Success")
    if not payment_url or not payment_id:
        message = response.get("Message") or response.get("Details") or ""
        logger.error("Некорректный ответ Init: %s", response)
        raise RuntimeError(message or "Не удалось создать платёж через T-Bank")
    if success_flag is False:
        error_text = response.get("Message") or response.get("Details") or "Ошибка создания платежа"
        logger.error("Создание платежа завершилось с ошибкой: %s", response)
        raise RuntimeError(error_text)

    db_instance = _get_db()
    await db_instance.add_payment(
        user_id=user_id,
        payment_id=str(payment_id),
        amount=amount,
        months=months,
        status=status,
    )
    return str(payment_url)


async def check_payment_status(payment_id: str) -> bool:
    """Проверить статус платежа по идентификатору PaymentId."""

    try:
        response = await get_payment_state(payment_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить состояние платежа", exc_info=err)
        raise RuntimeError("Ошибка при обращении к T-Bank") from err

    if not response:
        return False

    status = (response.get("Status") or "").upper()
    db_instance = _get_db()
    if status:
        await db_instance.set_payment_status(payment_id, status)
    return status == "CONFIRMED"


__all__ = ["create_payment", "check_payment_status", "set_db"]
