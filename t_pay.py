import os
import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv
import aiohttp


class TBankHttpError(RuntimeError):
    """Ошибка HTTP уровня при обращении к T-Bank."""


class TBankApiError(RuntimeError):
    """Ошибка бизнес-логики, возвращённая T-Bank API."""

    def __init__(self, code: str, message: str, details: Optional[str] = None):
        self.code = code
        self.details = details
        base_message = f"[{code}] {message}"
        if details:
            base_message = f"{base_message} | {details}"
        super().__init__(base_message)

load_dotenv()

"""
Этот модуль инкапсулирует работу с API интернет‑эквайринга T‑Bank (Tinkoff).
Для всех вызовов необходим TerminalKey и пароль, которые считываются из
переменных окружения. Дополнительно можно задавать success/fail URL, URL
уведомлений и API токен.

Примечание: сумма передаётся в копейках (например, 100₽ = 10000).
"""


def _read_env() -> Tuple[str, str, str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Прочитать и провалидировать настройки окружения для T-Bank."""

    base_url = (os.getenv("T_PAY_BASE_URL") or "https://securepay.tinkoff.ru/v2").rstrip("/")
    terminal_key = (os.getenv("T_PAY_TERMINAL_KEY", "") or "").strip()
    password = (os.getenv("T_PAY_PASSWORD", "") or "").strip()
    if not terminal_key or not password:
        raise RuntimeError("T_PAY_TERMINAL_KEY/T_PAY_PASSWORD не заданы")

    success_url = (os.getenv("T_PAY_SUCCESS_URL") or "").strip() or None
    fail_url = (os.getenv("T_PAY_FAIL_URL") or "").strip() or None
    notification_url = (os.getenv("T_PAY_NOTIFICATION_URL") or "").strip() or None
    api_token = (os.getenv("T_PAY_API_TOKEN") or "").strip() or None

    return (
        base_url,
        terminal_key,
        password,
        success_url,
        fail_url,
        notification_url,
        api_token,
    )


def _generate_token(payload: Dict[str, Any], password: str) -> str:
    """
    Сформировать токен подписи запроса согласно документации T‑Bank.

    Алгоритм:
      1. Взять только параметры корневого объекта (исключить вложенные словари и списки).
      2. Добавить пару {"Password": пароль терминала}.
      3. Отсортировать пары по ключу в алфавитном порядке.
      4. Сконкатенировать значения пар в одну строку.
      5. Посчитать SHA‑256 от строки и вернуть шестнадцатеричное представление.

    :param payload: словарь с параметрами запроса.
    :return: строка с хэш‑суммой.
    """
    items = []
    for key, value in payload.items():
        # Вложенные структуры (dict/list) не участвуют
        if isinstance(value, (dict, list)):
            continue
        # Пропускаем None
        if value is None:
            continue
        items.append((key, str(value)))
    # Добавляем секретный пароль
    items.append(("Password", password))
    # Сортировка по ключу
    items.sort(key=lambda x: x[0])
    # Конкатенация только значений
    token_string = "".join(v for _, v in items)
    # SHA‑256 хэш
    return hashlib.sha256(token_string.encode("utf-8")).hexdigest()


async def _post(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    base_url: str,
    terminal_key: str,
    password: str,
    api_token: Optional[str],
) -> Dict[str, Any]:
    """
    Общий метод для выполнения POST‑запроса к T‑Bank.

    Добавляет Token в тело запроса и, если задан API‑токен, заголовок Authorization.
    :param endpoint: относительный путь эндпоинта (например, "Init", "GetState").
    :param payload: параметры запроса.
    :return: JSON‑ответ сервера.
    :raises aiohttp.ClientError: при сетевой ошибке.
    """
    url = f"{base_url}/{endpoint.lstrip('/')}"
    body = payload.copy()
    body.setdefault("TerminalKey", terminal_key)
    terminal_key_value = str(body["TerminalKey"]).strip()
    if not (1 <= len(terminal_key_value) <= 64):
        raise RuntimeError("Некорректное значение TerminalKey")
    body["TerminalKey"] = terminal_key_value
    body["Token"] = _generate_token(body, password)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ConciergeBot/1.0",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise TBankHttpError(
                    f"HTTP {resp.status} {resp.content_type}: {text[:500]}"
                )
            if resp.content_type != "application/json":
                text = await resp.text()
                raise TBankHttpError(
                    f"Unexpected content-type {resp.content_type}: {text[:500]}"
                )
            data = await resp.json()
            if isinstance(data, dict) and data.get("Success") is False:
                raise TBankApiError(
                    str(data.get("ErrorCode", "")),
                    data.get("Message", ""),
                    data.get("Details"),
                )
            return data


async def init_payment(
    amount: int,
    order_id: str,
    description: str,
    *,
    customer_key: Optional[str] = None,
    pay_type: Optional[str] = None,
    language: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    recurrent: Optional[str] = None,
    receipt: Optional[Dict[str, Any]] = None,
    notification_url: Optional[str] = None,
    success_url: Optional[str] = None,
    fail_url: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Инициировать платеж.

    Создаёт платёж на стороне T‑Bank/Tinkoff и возвращает PaymentURL, по которому
    пользователь может оплатить заказ.

    :param amount: сумма в копейках (100₽ -> 10000).
    :param order_id: уникальный идентификатор заказа на стороне мерчанта.
    :param description: описание заказа, отображается на платёжной форме.
    :param customer_key: (опционально) идентификатор клиента для сохранения карт.
    :param pay_type: (опционально) 'O' или 'T' — одностадийная или двухстадийная оплата.
    :param language: (опционально) язык формы ('ru' или 'en').
    :param email: (опционально) email покупателя, используется для отправки чека.
    :param phone: (опционально) телефон покупателя, используется для отправки чека.
    :param recurrent: (опционально) 'Y' для сохранения реквизитов карты.
    :param receipt: (опционально) объект с данными для чека.
    :param notification_url: (опционально) override для URL уведомлений.
    :param success_url: (опционально) override для URL успеха.
    :param fail_url: (опционально) override для URL ошибки.
    :param extra: (опционально) дополнительные поля (будут вложены в DATA).
    :return: ответ метода Init (словарь). Полезные поля: PaymentURL, PaymentId, Status.
    """
    (
        base_url,
        terminal_key,
        password,
        success_url_env,
        fail_url_env,
        notification_url_env,
        api_token,
    ) = _read_env()

    logging.info(
        "Init to %s | term_key_len=%s",
        f"{base_url}/Init",
        len(terminal_key),
    )

    payload: Dict[str, Any] = {
        "Amount": amount,
        "OrderId": order_id,
        "Description": description,
    }
    if customer_key:
        payload["CustomerKey"] = customer_key
    if pay_type:
        payload["PayType"] = pay_type
    if language:
        payload["Language"] = language
    if recurrent:
        payload["Recurrent"] = recurrent
    if notification_url or notification_url_env:
        payload["NotificationURL"] = notification_url or notification_url_env
    if success_url or success_url_env:
        payload["SuccessURL"] = success_url or success_url_env
    if fail_url or fail_url_env:
        payload["FailURL"] = fail_url or fail_url_env
    # Кастомные параметры в DATA
    if extra:
        payload["DATA"] = extra
    # Реквизиты для чека (если подключена онлайн‑касса)
    if receipt:
        payload["Receipt"] = receipt
    # E‑mail и телефон можно также передавать в Receipt, но можно и на верхнем уровне
    if email:
        payload.setdefault("DATA", {})["Email"] = email
    if phone:
        payload.setdefault("DATA", {})["Phone"] = phone
    try:
        response = await _post(
            "Init",
            payload,
            base_url=base_url,
            terminal_key=terminal_key,
            password=password,
            api_token=api_token,
        )
    except TBankHttpError as err:
        logging.error("NETWORK/HTTP: %s (проверьте whitelist/host)", err)
        raise
    except TBankApiError as err:
        logging.error("API: %s", err)
        raise

    return response


async def confirm_payment(
    payment_id: str,
    amount: Optional[int] = None,
    receipt: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Подтвердить списание (двухстадийный платёж).

    :param payment_id: идентификатор платежа, полученный от Init/FinishAuthorize.
    :param amount: (опционально) сумма в копейках, если нужно списать меньше авторизации.
    :param receipt: (опционально) объект чека для второго этапа.
    :param ip: (опционально) IP‑адрес покупателя.
    :return: ответ метода Confirm.
    """
    (
        base_url,
        terminal_key,
        password,
        _,
        _,
        _,
        api_token,
    ) = _read_env()

    payload: Dict[str, Any] = {
        "PaymentId": payment_id,
    }
    if amount:
        payload["Amount"] = amount
    if ip:
        payload["IP"] = ip
    if receipt:
        payload["Receipt"] = receipt
    return await _post(
        "Confirm",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def get_payment_state(payment_id: str, ip: Optional[str] = None) -> Dict[str, Any]:
    """
    Получить статус платежа.

    :param payment_id: идентификатор платежа в системе T‑Bank.
    :param ip: (опционально) IP‑адрес покупателя.
    :return: словарь с полями Status, ErrorCode, OrderId и т.п.
    """
    (
        base_url,
        terminal_key,
        password,
        _,
        _,
        _,
        api_token,
    ) = _read_env()

    payload: Dict[str, Any] = {
        "PaymentId": payment_id,
    }
    if ip:
        payload["IP"] = ip
    try:
        return await _post(
            "GetState",
            payload,
            base_url=base_url,
            terminal_key=terminal_key,
            password=password,
            api_token=api_token,
        )
    except TBankHttpError as err:
        logging.error("NETWORK/HTTP: %s (проверьте whitelist/host)", err)
        raise
    except TBankApiError as err:
        logging.error("API: %s", err)
        raise


async def finish_authorize(
    payment_id: str,
    card_data: Dict[str, Any],
    ip: Optional[str] = None,
    send_email: Optional[bool] = None,
    source: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    three_ds_v2: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Подтвердить платеж с собственными платёжными данными (одностадийный или двухстадийный платёж).

    Используется, если мерчант собирает данные карты самостоятельно (PCI DSS).
    В Telegram‑боте обычно используется метод Init, выдающий PaymentURL, поэтому
    метод FinishAuthorize может не понадобиться.

    :param payment_id: идентификатор платежа, полученный от Init.
    :param card_data: словарь с данными карты (Pan, ExpDate, CVV и т. д.) — требует соответствия PCI DSS.
    :param ip: (опционально) IP‑адрес покупателя.
    :param send_email: (опционально) отправлять ли email‑уведомление покупателю.
    :param source: (опционально) источник платежа (cards, beeline, mts, tele2, megafon, einvoicing, webmoney).
    :param data: (опционально) дополнительные параметры (ключ:значение).
    :param three_ds_v2: (опционально) параметры 3DS v2, если требуется аутентификация.
    :return: ответ метода FinishAuthorize.
    """
    (
        base_url,
        terminal_key,
        password,
        _,
        _,
        _,
        api_token,
    ) = _read_env()

    payload: Dict[str, Any] = {
        "PaymentId": payment_id,
        "CardData": card_data,
    }
    if ip:
        payload["IP"] = ip
    if send_email is not None:
        payload["SendEmail"] = bool(send_email)
    if source:
        payload["Source"] = source
    if data:
        payload["DATA"] = data
    if three_ds_v2:
        # Параметры 3DS v2 должны быть плоскими полями, поэтому просто обновляем словарь
        payload.update(three_ds_v2)
    return await _post(
        "FinishAuthorize",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


__all__ = [
    "TBankApiError",
    "TBankHttpError",
    "init_payment",
    "confirm_payment",
    "get_payment_state",
    "finish_authorize",
]