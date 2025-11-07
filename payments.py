from __future__ import annotations

import logging
import time
from typing import Optional

from config import config
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


async def create_payment(
    user_id: int,
    months: int,
    amount: int,
    db: Optional[DB] = None,
) -> str:
    """Создать платёж через T-Bank и вернуть ссылку на оплату."""

    if user_id <= 0:
        raise ValueError("Некорректный идентификатор пользователя")
    if months <= 0:
        raise ValueError("Срок подписки должен быть положительным")
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    explicit_db = db is not None
    db_instance = db or _get_db()

    # Поддержка старого порядка аргументов: create_payment(user_id, price, months)
    resolved_months = months
    resolved_amount = amount
    if not explicit_db and resolved_amount < resolved_months:
        resolved_amount, resolved_months = resolved_months, resolved_amount

    order_id = f"{user_id}_{resolved_months}_{int(time.time())}"
    description = f"Подписка на {resolved_months} мес. (user {user_id})"

    # Если БД не передали явно, предполагаем, что сумма указана в рублях и переводим в копейки
    if explicit_db:
        amount_minor = resolved_amount
    else:
        amount_minor = resolved_amount * 100

    try:
        response = await init_payment(
            amount=amount_minor,
            order_id=order_id,
            description=description,
            success_url=config.T_PAY_SUCCESS_URL or None,
            fail_url=config.T_PAY_FAIL_URL or None,
            notification_url=config.TINKOFF_NOTIFY_URL or None,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось вызвать T-Bank Init", exc_info=err)
        raise RuntimeError("Не удалось создать платёж через T-Bank") from err

    if not response:
        raise RuntimeError("Не удалось создать платёж через T-Bank")

    payment_url = response.get("PaymentURL")
    payment_id = response.get("PaymentId")
    success_flag = response.get("Success")
    if not payment_url or not payment_id:
        message = response.get("Message") or response.get("Details") or ""
        logger.error("Некорректный ответ Init: %s", response)
        raise RuntimeError(message or "Не удалось создать платёж через T-Bank")
    if success_flag is False:
        error_text = response.get("Message") or response.get("Details") or "Ошибка создания платежа"
        logger.error("Создание платежа завершилось с ошибкой: %s", response)
        raise RuntimeError(error_text)

    await db_instance.add_payment(
        user_id=user_id,
        payment_id=str(payment_id),
        order_id=order_id,
        amount=amount_minor,
        months=resolved_months,
    )
    return str(payment_url)


async def apply_successful_payment(payment_id: str, db: DB) -> bool:
    """Идемпотентно применить успешный платёж и продлить подписку."""

    if not payment_id:
        return False

    payment = await db.get_payment_by_payment_id(payment_id)
    if payment is None:
        return False

    current_status = (payment["status"] or "").upper()
    if current_status == "CONFIRMED":
        return True

    user_id = int(payment["user_id"] or 0)
    months = int(payment["months"] or 0)
    if user_id <= 0 or months <= 0:
        logger.warning(
            "Пропущено применение платежа %s: неверные данные user_id=%s, months=%s",
            payment_id,
            user_id,
            months,
        )
        return False

    await db.set_payment_status(payment_id, "CONFIRMED")
    await db.extend_subscription(user_id, months)
    await db.set_paid_only(user_id, False)
    return True


async def check_payment_status(payment_id: str, db: Optional[DB] = None) -> bool:
    """Проверить статус платежа по идентификатору PaymentId."""

    if not payment_id:
        return False

    try:
        response = await get_payment_state(payment_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить состояние платежа", exc_info=err)
        raise RuntimeError("Ошибка при обращении к T-Bank") from err

    if not response:
        return False

    status = (response.get("Status") or "").upper()
    db_instance = db or _get_db()
    if status:
        await db_instance.set_payment_status(payment_id, status)
    return status == "CONFIRMED"


__all__ = [
    "apply_successful_payment",
    "check_payment_status",
    "create_payment",
    "set_db",
]
