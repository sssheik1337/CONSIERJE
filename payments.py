from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from config import config
from db import DB
from logger import logger
from t_pay import (
    TBankApiError,
    TBankHttpError,
    add_customer,
    charge_qr,
    get_add_account_qr_state,
    get_customer,
    get_payment_state,
    get_qr,
    init_payment,
)

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


def _normalize_amount_inputs(
    months: int, amount: int, *, explicit_db: bool
) -> tuple[int, int, int]:
    """Проверить значения срока и суммы, вернуть нормализованные данные."""

    resolved_months = months
    resolved_amount = amount
    if not explicit_db and resolved_amount < resolved_months:
        resolved_amount, resolved_months = resolved_months, resolved_amount
    if resolved_months <= 0:
        raise ValueError("Срок подписки должен быть положительным")
    if resolved_amount <= 0:
        raise ValueError("Сумма должна быть положительной")
    amount_minor = resolved_amount if explicit_db else resolved_amount * 100
    return resolved_months, resolved_amount, amount_minor


def _build_order_id(prefix: str, user_id: int, months: int) -> str:
    """Сформировать order_id с учётом пользователя и срока."""

    return f"{prefix}_{user_id}_{months}_{int(time.time())}"


def _extract_row_text(row: Mapping[str, Any] | None, key: str) -> Optional[str]:
    """Безопасно получить строковое значение из строки БД."""

    if row is None:
        return None
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:  # noqa: BLE001
            value = None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    mapping: Mapping[str, Any]
    if isinstance(row, Mapping):
        mapping = row
    elif hasattr(row, "keys"):
        try:
            mapping = {column: row[column] for column in row.keys()}
        except Exception:  # noqa: BLE001
            mapping = {}
    else:
        try:
            mapping = dict(row)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            mapping = {}

    value = mapping.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _ensure_customer_key(
    db: DB, user_id: int, *, user_row: Mapping[str, Any] | None = None
) -> tuple[Optional[str], Mapping[str, Any] | None]:
    """Убедиться, что у пользователя сохранён CustomerKey."""

    if user_id <= 0:
        return None, user_row
    if user_row is None:
        user_row = await db.get_user(user_id)
    customer_key = _extract_row_text(user_row, "customer_key")
    if not customer_key:
        customer_key = str(user_id)
        try:
            await db.set_customer_key(user_id, customer_key)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось сохранить CustomerKey для пользователя %s", user_id)
    return customer_key, user_row


async def _ensure_customer_registered(
    db: DB,
    user_id: int,
    customer_key: str,
    *,
    user_row: Mapping[str, Any] | None = None,
) -> None:
    """Проверить наличие клиента в T-Bank и зарегистрировать при необходимости."""

    if user_id <= 0 or not customer_key:
        return
    try:
        already_marked = await db.is_customer_registered(user_id)
    except AttributeError:
        already_marked = False
    if already_marked:
        return

    # Сначала пытаемся получить клиента — если существует, просто запоминаем флаг.
    try:
        await get_customer(customer_key)
    except TBankApiError as err:
        logger.info(
            "Клиент %s отсутствует в T-Bank, требуется регистрация: %s",
            customer_key,
            err,
        )
    else:
        await db.set_customer_registered(user_id, True)
        return

    email = _extract_row_text(user_row, "email")
    phone = _extract_row_text(user_row, "phone")
    try:
        await add_customer(customer_key, email=email, phone=phone)
    except (TBankHttpError, TBankApiError) as err:
        logger.error(
            "Не удалось зарегистрировать клиента %s в T-Bank: %s",
            customer_key,
            err,
        )
        raise
    await db.set_customer_registered(user_id, True)


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


async def init_sbp_payment(
    user_id: int,
    months: int,
    amount: int,
    *,
    db: Optional[DB] = None,
) -> dict[str, Any]:
    """Создать платёж для СБП с рекуррентной привязкой счёта."""

    if user_id <= 0:
        raise ValueError("Некорректный идентификатор пользователя")
    explicit_db = db is not None
    resolved_db = db or _get_db()
    resolved_months, _, amount_minor = _normalize_amount_inputs(
        months, amount, explicit_db=explicit_db
    )
    order_id = _build_order_id("sbp", user_id, resolved_months)
    description = f"Подписка через СБП на {resolved_months} мес. (user {user_id})"

    try:
        response = await init_payment(
            amount=amount_minor,
            order_id=order_id,
            description=description,
            pay_type="O",
            extra={"QR": "true"},
            notification_url=config.TINKOFF_NOTIFY_URL or None,
        )
    except (TBankHttpError, TBankApiError) as err:
        logger.error("Init для СБП завершился ошибкой: %s", err)
        raise RuntimeError(str(err)) from err

    payment_id = str(response.get("PaymentId") or "")
    if not payment_id:
        raise RuntimeError("Init не вернул идентификатор платежа")

    await resolved_db.add_payment(
        user_id=user_id,
        payment_id=payment_id,
        order_id=order_id,
        amount=amount_minor,
        months=resolved_months,
        method="sbp",
        is_sbp=True,
    )

    return {
        "payment_id": payment_id,
        "payment_url": response.get("PaymentURL"),
        "order_id": order_id,
        "amount": amount_minor,
    }


async def form_sbp_qr(
    user_id: int,
    payment_id: str,
    *,
    db: Optional[DB] = None,
    data_type: str = "PAYLOAD",
) -> Optional[dict[str, Any]]:
    """Запросить QR для оплаты через СБП и сохранить RequestKey."""

    if user_id <= 0 or not payment_id:
        raise ValueError("Некорректные данные для формирования QR")
    resolved_db = db or _get_db()
    try:
        response = await get_qr(payment_id, data_type=data_type)
    except (TBankApiError, TBankHttpError) as err:
        logger.error("GetQr завершился ошибкой: %s", err)
        raise

    params = response.get("Params") or {}
    payload = (
        params.get("Data")
        or params.get("Payload")
        or response.get("Data")
        or response.get("Payload")
    )
    request_key = params.get("RequestKey") or response.get("RequestKey")
    if request_key:
        await resolved_db.save_request_key(user_id, request_key, status="NEW")
        await resolved_db.set_payment_request_key(payment_id, request_key)

        return {
            "success": True,
            "payload": payload,
            "request_key": request_key,
            "payment_id": payment_id,
        }

    if payload:
        logger.info(
            "GetQr вернул готовую ссылку для СБП без RequestKey: %s", response
        )
        return {
            "success": True,
            "payment_id": payment_id,
            "qr_url": payload,
        }

    logger.error("GetQr не вернул ни RequestKey, ни Data: %s", response)
    raise RuntimeError("GetQr не вернул ни RequestKey, ни Data")


async def get_sbp_link_status(
    request_key: str,
    *,
    user_id: Optional[int] = None,
    db: Optional[DB] = None,
) -> dict[str, Any]:
    """Проверить статус привязки счёта через RequestKey."""

    if not request_key:
        raise ValueError("RequestKey не указан")
    resolved_db = db or _get_db()
    resolved_user = user_id
    if resolved_user is None:
        resolved_user = await resolved_db.get_user_by_request_key(request_key)
    try:
        response = await get_add_account_qr_state(request_key)
    except (TBankApiError, TBankHttpError) as err:
        logger.error("GetAddAccountQrState завершился ошибкой: %s", err)
        raise

    status = (response.get("Status") or response.get("state") or "").upper()
    params = response.get("Params") or {}
    account_token = params.get("AccountToken") or response.get("AccountToken")
    bank_member_id = params.get("BankMemberId") or response.get("BankMemberId")
    bank_member_name = params.get("BankMemberName") or response.get("BankMemberName")

    if resolved_user:
        if status:
            await resolved_db.update_sbp_status(resolved_user, status)
        if account_token:
            await resolved_db.save_account_token(
                resolved_user,
                str(account_token),
                bank_member_id=str(bank_member_id) if bank_member_id else None,
                bank_member_name=str(bank_member_name) if bank_member_name else None,
            )
        payment_row = await resolved_db.get_payment_by_request_key(request_key)
        if payment_row and account_token:
            await resolved_db.set_payment_account_token(
                payment_row["payment_id"], account_token
            )

    return {
        "status": status,
        "account_token": account_token,
        "bank_member_id": bank_member_id,
        "bank_member_name": bank_member_name,
        "user_id": resolved_user,
    }


async def charge_sbp_autopayment(
    user_id: int,
    months: int,
    amount: int,
    account_token: str,
    *,
    db: Optional[DB] = None,
    ip: str = "127.0.0.1",
    send_email: bool = False,
    info_email: Optional[str] = None,
) -> dict[str, Any]:
    """Выполнить автосписание через СБП по сохранённому счёту."""

    if user_id <= 0:
        raise ValueError("Некорректный пользователь для автосписания")
    explicit_db = db is not None
    resolved_db = db or _get_db()
    resolved_months, _, amount_minor = _normalize_amount_inputs(
        months, amount, explicit_db=explicit_db
    )
    order_id = _build_order_id("sbp_auto", user_id, resolved_months)
    description = f"Автопродление подписки (СБП) на {resolved_months} мес."

    init_response = await init_payment(
        amount=amount_minor,
        order_id=order_id,
        description=description,
        pay_type="O",
        extra={"QR": "true"},
        notification_url=config.TINKOFF_NOTIFY_URL or None,
    )
    payment_id = str(init_response.get("PaymentId") or "")
    if not payment_id:
        raise RuntimeError("ChargeQr: Init не вернул PaymentId")

    await resolved_db.add_payment(
        user_id=user_id,
        payment_id=payment_id,
        order_id=order_id,
        amount=amount_minor,
        months=resolved_months,
        method="sbp",
        is_sbp=True,
        account_token=account_token,
    )

    charge_response = await charge_qr(
        payment_id,
        account_token,
        ip,
        send_email=send_email,
        info_email=info_email,
    )
    status = charge_response.get("Status") or "PENDING"
    await resolved_db.set_payment_status(payment_id, str(status).upper())
    await resolved_db.set_payment_account_token(payment_id, account_token)

    return {
        "payment_id": payment_id,
        "status": status,
        "charge_response": charge_response,
    }


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

    explicit_db = db is not None
    db_instance = db or _get_db()

    # Поддержка старого порядка аргументов: create_payment(user_id, price, months)
    resolved_months, resolved_amount, amount_minor = _normalize_amount_inputs(
        months, amount, explicit_db=explicit_db
    )

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
    customer_key_value: Optional[str] = str(user_id) if normalized_method == "card" else None
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

    if normalized_method == "card" and customer_key_value:
        try:
            await db_instance.set_customer_key(user_id, customer_key_value)
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось сохранить CustomerKey для пользователя %s", user_id)
    if effective_recurrent and customer_key_value:
        await _ensure_customer_registered(
            db_instance,
            user_id,
            customer_key_value,
            user_row=user_row,
        )

    try:
        response = await init_payment(
            amount=amount_minor,
            order_id=order_id,
            description=description,
            customer_key=customer_key_value if normalized_method == "card" else None,
            recurrent="Y" if effective_recurrent else None,
            success_url=config.T_PAY_SUCCESS_URL or None,
            fail_url=config.T_PAY_FAIL_URL or None,
            notification_url=config.TINKOFF_NOTIFY_URL or None,
        )
    except (TBankHttpError, TBankApiError) as err:
        logger.error("Некорректный ответ Init: %s", err)
        raise RuntimeError(str(err)) from err
    except Exception as err:  # noqa: BLE001
        logger.error("Неожиданная ошибка при создании платежа: %s", err)
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

    logger.info("Payment created: order=%s, payment_id=%s", order_id, response.get("PaymentId"))

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
    try:
        method = str(payment["method"] or "")
    except Exception:  # noqa: BLE001
        method = ""
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
    is_sbp_payment = method.strip().lower() == "sbp"
    if not is_sbp_payment:
        try:
            is_sbp_payment = bool(payment["is_sbp"])  # type: ignore[index]
        except Exception:  # noqa: BLE001
            pass

    if not is_sbp_payment:
        try:
            await db.set_auto_renew(user_id, True)
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "Не удалось автоматически включить автопродление для пользователя %s: %s",
                user_id,
                err,
            )
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
            try:
                await db_instance.set_auto_renew(user_id, True)
            except Exception as err:  # noqa: BLE001
                logger.debug(
                    "Не удалось включить автопродление после подтверждения платежа %s: %s",
                    payment_id,
                    err,
                )
    return status == "CONFIRMED"


__all__ = [
    "SBP_NOTE",
    "apply_successful_payment",
    "check_payment_status",
    "charge_sbp_autopayment",
    "create_payment",
    "detect_payment_type",
    "disable_auto_renew_for_sbp",
    "form_sbp_qr",
    "get_sbp_link_status",
    "init_sbp_payment",
    "set_db",
]
