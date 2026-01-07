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
    charge_qr,
    get_add_account_qr_state,
    get_payment_state,
    get_qr,
    init_payment,
)

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

    # Сумма из базы хранится в рублях, но API T-Bank принимает копейки,
    # поэтому всегда переводим в минорные единицы независимо от источника.
    amount_minor = resolved_amount * 100
    return resolved_months, resolved_amount, amount_minor


def _build_order_id(prefix: str, user_id: int, months: int) -> str:
    """Сформировать order_id с учётом пользователя и срока."""

    return f"{prefix}_{user_id}_{months}_{int(time.time())}"


async def create_card_payment(user_id: int, months: int, price: int) -> str:
    """Создать платёж по карте и вернуть ссылку PaymentURL."""

    if user_id <= 0:
        raise ValueError("Некорректный идентификатор пользователя")
    if months <= 0 or price <= 0:
        raise ValueError("Некорректные параметры оплаты")

    resolved_db = _get_db()
    user_row = await resolved_db.get_user(user_id)
    email_value = None
    if user_row is not None and hasattr(user_row, "keys") and "email" in user_row.keys():
        email_value = str(user_row["email"] or "").strip() or None
    if not email_value:
        raise ValueError("Не указан email пользователя для чека")

    order_id = _build_order_id("card", user_id, months)
    amount = price
    receipt = {
        "FfdVersion": "1.05",
        "Taxation": "usn_income",
        "Items": [
            {
                "Name": "Подписка",
                "Price": amount,
                "Quantity": 1,
                "Amount": amount,
                "PaymentMethod": "full_prepayment",
                "PaymentObject": "service",
                "Tax": "none",
            }
        ],
        "Payments": {"Electronic": amount},
        "Email": email_value,
    }
    response = await init_payment(
        amount=amount,
        order_id=order_id,
        description="Подписка",
        customer_key=str(user_id),
        pay_type="O",
        recurrent="Y",
        receipt=receipt,
        email=email_value,
        extra={"Email": email_value},
    )

    payment_id = str(response.get("PaymentId") or "")
    if not payment_id:
        raise RuntimeError("Init не вернул идентификатор платежа")

    await resolved_db.add_payment(
        user_id=user_id,
        payment_id=payment_id,
        order_id=order_id,
        amount=amount,
        months=months,
        status="PENDING",
        method="card",
        customer_key=str(user_id),
    )

    payment_url = response.get("PaymentURL")
    if not payment_url:
        raise RuntimeError("Init не вернул ссылку оплаты")
    return str(payment_url)


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
    price: int,
    contact_type: str,
    contact_value: str,
    *,
    db: Optional[DB] = None,
) -> dict[str, Any]:
    """Создать платёж для СБП с рекуррентной привязкой счёта."""

    if user_id <= 0:
        raise ValueError("Некорректный идентификатор пользователя")
    explicit_db = db is not None
    resolved_db = db or _get_db()
    resolved_months, _, amount_minor = _normalize_amount_inputs(
        months, price, explicit_db=explicit_db
    )
    order_id = _build_order_id("sbp", user_id, resolved_months)
    description = f"Подписка через СБП на {resolved_months} мес. (user {user_id})"

    email_value: Optional[str] = None
    phone_value: Optional[str] = None
    normalized_contact = (contact_type or "").strip().lower()
    if normalized_contact == "email":
        email_value = contact_value
    elif normalized_contact == "phone":
        phone_value = contact_value
    else:
        raise ValueError("Не указан тип контакта для чека")

    try:
        response = await init_payment(
            amount=amount_minor,
            order_id=order_id,
            description=description,
            recurrent="Y",
            pay_type="O",
            extra={"QR": "true"},
            notification_url=config.TINKOFF_NOTIFY_URL or None,
            email=email_value,
            phone=phone_value,
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
    user_row = await resolved_db.get_user(user_id)
    raw_contact = ""
    if user_row is not None and hasattr(user_row, "keys"):
        try:
            raw_contact = str(user_row["email"] or "").strip()
        except (KeyError, TypeError, ValueError):
            raw_contact = ""
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    if raw_contact:
        if raw_contact.startswith("+7") and len(raw_contact) == 12:
            contact_phone = raw_contact
        elif "@" in raw_contact:
            contact_email = raw_contact
    if not contact_email and not contact_phone:
        raise RuntimeError("Для отправки чека не указан контакт пользователя")
    resolved_months, _, amount_minor = _normalize_amount_inputs(
        months, amount, explicit_db=explicit_db
    )
    order_id = _build_order_id("sbp_auto", user_id, resolved_months)
    description = f"Автопродление подписки (СБП) на {resolved_months} мес."

    init_response = await init_payment(
        amount=amount_minor,
        order_id=order_id,
        description=description,
        recurrent="Y",
        pay_type="O",
        extra={"QR": "true"},
        notification_url=config.TINKOFF_NOTIFY_URL or None,
        email=contact_email,
        phone=contact_phone,
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
    try:
        account_token = await db.get_account_token(user_id)
    except Exception:  # noqa: BLE001
        account_token = None
    if account_token:
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
        if user_id > 0:
            try:
                account_token = await db_instance.get_account_token(user_id)
            except Exception:  # noqa: BLE001
                account_token = None
            if account_token:
                await db_instance.set_auto_renew(user_id, True)
    return status == "CONFIRMED"


__all__ = [
    "apply_successful_payment",
    "check_payment_status",
    "charge_sbp_autopayment",
    "detect_payment_type",
    "disable_auto_renew_for_sbp",
    "form_sbp_qr",
    "get_sbp_link_status",
    "init_sbp_payment",
    "set_db",
]
