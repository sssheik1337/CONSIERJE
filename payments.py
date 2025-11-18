from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from config import config
from db import DB
from t_pay import TBankApiError, TBankHttpError, get_payment_state, init_payment

logger = logging.getLogger(__name__)

_payment_db: Optional[DB] = None

SBP_NOTE = "Оплата через СБП не продлевается автоматически."


def set_db(database: DB) -> None:
    """Задать экземпляр базы данных для работы с платежами."""

    global _payment_db
    _payment_db = database


def _get_db() -> DB:
    """Вернуть используемую базу данных."""

    if _payment_db is not None:
        return _payment_db
    return DB(config.DB_PATH)


def _value_contains_sbp(value: Any) -> bool:
    """Понять, содержит ли значение признаки оплаты через СБП."""

    if value is None:
        return False
    if isinstance(value, str):
        return "sbp" in value.lower()
    if isinstance(value, Mapping):
        return any(_value_contains_sbp(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_value_contains_sbp(item) for item in value)
    return False


def detect_payment_type(payload: Mapping[str, Any] | None) -> str:
    """Определить тип платежа по данным ответа T-Bank."""

    if not isinstance(payload, Mapping):
        return "card"
    candidates = [
        payload.get("PaymentMethod"),
        payload.get("paymentMethod"),
        payload.get("PaymentType"),
        payload.get("paymentType"),
        payload.get("PayType"),
        payload.get("payType"),
    ]
    for candidate in candidates:
        if _value_contains_sbp(candidate):
            return "sbp"
    return "card"


async def disable_auto_renew_for_sbp(
    db: DB, user_id: int, note: str | None = None
) -> None:
    """Отключить автопродление после оплаты через СБП и записать лог."""

    if user_id <= 0:
        return
    message = note or "Оплата через СБП подтверждена, автопродление отключено."
    had_auto = False
    try:
        user_row = await db.get_user(user_id)
    except Exception:  # noqa: BLE001
        user_row = None
    if user_row is not None and hasattr(user_row, "keys"):
        try:
            had_auto = bool(user_row["auto_renew"])
        except (KeyError, TypeError, ValueError):
            had_auto = False
    await db.set_auto_renew(user_id, False)
    if had_auto:
        await db.log_payment_attempt(
            user_id,
            "SBP_CONFIRMED",
            message,
            payment_type="sbp",
        )


async def create_payment(
    user_id: int,
    months: int,
    amount: int,
    db: Optional[DB] = None,
    *,
    payment_method: str = "card",
    force_recurrent: Optional[bool] = None,
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

    normalized_method = (payment_method or "card").strip().lower()
    if normalized_method not in {"card", "sbp"}:
        normalized_method = "card"
    if normalized_method == "sbp":
        try:
            await db_instance.set_auto_renew(user_id, False)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Не удалось сразу отключить автопродление перед оплатой через СБП для %s",
                user_id,
            )
    user_row = await db_instance.get_user(user_id)
    auto_recurrent = False
    customer_key_value: Optional[str] = None
    if user_row is not None:
        try:
            auto_recurrent = bool(user_row["auto_renew"])
        except (KeyError, TypeError, ValueError):
            auto_recurrent = False
        try:
            stored_key_raw = user_row["customer_key"]
        except (KeyError, TypeError, ValueError):
            stored_key_raw = None
        stored_key = str(stored_key_raw).strip() if stored_key_raw not in (None, "") else ""
        if stored_key:
            customer_key_value = stored_key

    effective_recurrent = force_recurrent
    if effective_recurrent is None:
        effective_recurrent = normalized_method == "card" and auto_recurrent

    if effective_recurrent and not customer_key_value:
        customer_key_value = str(user_id)
        try:
            await db_instance.set_customer_key(user_id, customer_key_value)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось сохранить CustomerKey для пользователя %s", user_id)

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
            customer_key=customer_key_value if effective_recurrent else None,
            recurrent="Y" if effective_recurrent else None,
            success_url=config.T_PAY_SUCCESS_URL or None,
            fail_url=config.T_PAY_FAIL_URL or None,
            notification_url=config.TINKOFF_NOTIFY_URL or None,
        )
    except (TBankHttpError, TBankApiError) as err:
        logging.error("Некорректный ответ Init: %s", err)
        raise RuntimeError(str(err)) from err
    except Exception as err:  # noqa: BLE001
        logging.error("Неожиданная ошибка при создании платежа: %s", err)
        raise RuntimeError(str(err)) from err

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

    logging.info("Payment created: order=%s, payment_id=%s", order_id, response.get("PaymentId"))

    await db_instance.add_payment(
        user_id=user_id,
        payment_id=str(payment_id),
        order_id=order_id,
        amount=amount_minor,
        months=resolved_months,
        method=normalized_method,
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

    logger.info("Ручная проверка оплаты: запрос статуса payment_id=%s", payment_id)
    try:
        response = await get_payment_state(payment_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить состояние платежа", exc_info=err)
        raise RuntimeError("Ошибка при обращении к T-Bank") from err

    if not response:
        return False

    status = (response.get("Status") or "").upper()
    logger.info(
        "Ручная проверка оплаты: получен статус %s для payment_id=%s",
        status or "неизвестно",
        payment_id,
    )
    db_instance = db or _get_db()
    if status:
        await db_instance.set_payment_status(payment_id, status)
    if status == "CONFIRMED":
        payment_row = await db_instance.get_payment_by_payment_id(payment_id)
        stored_method = ""
        user_id = 0
        if payment_row is not None:
            try:
                stored_method = str(payment_row["method"] or "").strip().lower()
            except (KeyError, TypeError, ValueError):
                stored_method = ""
            try:
                user_id = int(payment_row["user_id"] or 0)
            except (KeyError, TypeError, ValueError):
                user_id = 0
        payment_type = detect_payment_type(response)
        try:
            await db_instance.set_payment_method(payment_id, payment_type)
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "Не удалось записать тип оплаты %s для платежа %s: %s",
                payment_type,
                payment_id,
                err,
            )
        is_sbp = payment_type == "sbp" or stored_method == "sbp"
        if is_sbp and user_id > 0:
            await disable_auto_renew_for_sbp(
                db_instance,
                user_id,
                note="Оплата через СБП подтверждена вручную, автопродление отключено.",
            )
        elif user_id > 0:
            rebill_id = response.get("RebillId") or response.get("rebillId")
            if rebill_id:
                try:
                    await db_instance.set_rebill_id(user_id, str(rebill_id))
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        "Не удалось сохранить RebillId %s для пользователя %s: %s",
                        rebill_id,
                        user_id,
                        err,
                    )
            customer_key = response.get("CustomerKey") or response.get("customerKey")
            if customer_key:
                try:
                    await db_instance.set_customer_key(user_id, str(customer_key))
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        "Не удалось сохранить CustomerKey %s для пользователя %s: %s",
                        customer_key,
                        user_id,
                        err,
                    )
            try:
                await db_instance.set_rebill_parent_payment(user_id, str(payment_id))
            except Exception as err:  # noqa: BLE001
                logger.debug(
                    "Не удалось сохранить родительский платёж %s для пользователя %s: %s",
                    payment_id,
                    user_id,
                    err,
                )
    return status == "CONFIRMED"


__all__ = [
    "SBP_NOTE",
    "apply_successful_payment",
    "check_payment_status",
    "create_payment",
    "detect_payment_type",
    "disable_auto_renew_for_sbp",
    "set_db",
]
