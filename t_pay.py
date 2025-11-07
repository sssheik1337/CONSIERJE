import os
import hashlib
import json
from typing import Any, Dict, Optional

import aiohttp

"""
Этот модуль инкапсулирует работу с API интернет‑эквайринга T‑Bank (Tinkoff).
Для всех вызовов необходим TerminalKey и пароль, которые выдаются в
личном кабинете. Они считываются из переменных окружения:

  T_PAY_BASE_URL      – базовый URL для запросов (по умолчанию https://securepay.tinkoff.ru/v2)
  T_PAY_TERMINAL_KEY  – идентификатор терминала
  T_PAY_PASSWORD      – пароль терминала для генерации токена
  T_PAY_API_TOKEN     – API-токен (опционально). Если указан, передаётся в заголовке Authorization.
  T_PAY_NOTIFICATION_URL – URL для уведомлений о состоянии платежей (опционально)
  T_PAY_SUCCESS_URL       – URL для перенаправления клиента при успешной оплате (опционально)
  T_PAY_FAIL_URL          – URL для перенаправления клиента при неудачной оплате (опционально)

Примечание: сумма передается в копейках (например, 100₽ = 10000).
"""

# Читаем настройки из окружения
T_PAY_BASE_URL: str = os.getenv("T_PAY_BASE_URL", "https://securepay.tinkoff.ru/v2").rstrip("/")
T_PAY_TERMINAL_KEY: str = os.getenv("T_PAY_TERMINAL_KEY", "")
T_PAY_PASSWORD: str = os.getenv("T_PAY_PASSWORD", "")
T_PAY_API_TOKEN: Optional[str] = os.getenv("T_PAY_API_TOKEN") or None
T_PAY_NOTIFICATION_URL: Optional[str] = os.getenv("T_PAY_NOTIFICATION_URL") or None
T_PAY_SUCCESS_URL: Optional[str] = os.getenv("T_PAY_SUCCESS_URL") or None
T_PAY_FAIL_URL: Optional[str] = os.getenv("T_PAY_FAIL_URL") or None


def _generate_token(payload: Dict[str, Any]) -> str:
    """
    Сформировать токен подписи запроса согласно документации T‑Bank.

    Алгоритм:
      1. Взять только параметры корневого объекта (исключить вложенные словари и списки).
      2. Добавить пару {"Password": T_PAY_PASSWORD}.
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
    items.append(("Password", T_PAY_PASSWORD))
    # Сортировка по ключу
    items.sort(key=lambda x: x[0])
    # Конкатенация только значений
    token_string = "".join(v for _, v in items)
    # SHA‑256 хэш
    return hashlib.sha256(token_string.encode("utf-8")).hexdigest()


async def _post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Общий метод для выполнения POST‑запроса к T‑Bank.

    Добавляет Token в тело запроса и, если задан API‑токен, заголовок Authorization.
    :param endpoint: относительный путь эндпоинта (например, "Init", "GetState").
    :param payload: параметры запроса.
    :return: JSON‑ответ сервера.
    :raises aiohttp.ClientError: при сетевой ошибке.
    """
    url = f"{T_PAY_BASE_URL}/{endpoint.lstrip('/')}"
    # Устанавливаем обязательные поля
    payload = payload.copy()
    payload.setdefault("TerminalKey", T_PAY_TERMINAL_KEY)
    # Генерируем подпись
    payload["Token"] = _generate_token(payload)
    headers = {
        "Content-Type": "application/json",
    }
    if T_PAY_API_TOKEN:
        headers["Authorization"] = f"Bearer {T_PAY_API_TOKEN}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            # попытаться вернуть json, даже если статус код != 200
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                raise RuntimeError(f"Unexpected response from {url}: {text}")
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
    if notification_url or T_PAY_NOTIFICATION_URL:
        payload["NotificationURL"] = notification_url or T_PAY_NOTIFICATION_URL
    if success_url or T_PAY_SUCCESS_URL:
        payload["SuccessURL"] = success_url or T_PAY_SUCCESS_URL
    if fail_url or T_PAY_FAIL_URL:
        payload["FailURL"] = fail_url or T_PAY_FAIL_URL
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
    return await _post("Init", payload)


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
    payload: Dict[str, Any] = {
        "PaymentId": payment_id,
    }
    if amount:
        payload["Amount"] = amount
    if ip:
        payload["IP"] = ip
    if receipt:
        payload["Receipt"] = receipt
    return await _post("Confirm", payload)


async def get_payment_state(payment_id: str, ip: Optional[str] = None) -> Dict[str, Any]:
    """
    Получить статус платежа.

    :param payment_id: идентификатор платежа в системе T‑Bank.
    :param ip: (опционально) IP‑адрес покупателя.
    :return: словарь с полями Status, ErrorCode, OrderId и т.п.
    """
    payload: Dict[str, Any] = {
        "PaymentId": payment_id,
    }
    if ip:
        payload["IP"] = ip
    return await _post("GetState", payload)


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
    return await _post("FinishAuthorize", payload)


__all__ = [
    "init_payment",
    "confirm_payment",
    "get_payment_state",
    "finish_authorize",
]