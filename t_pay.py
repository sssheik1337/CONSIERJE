import asyncio
import hashlib
import json
import socket
from typing import Any, Dict, Optional, Tuple

import aiohttp
import requests

from config import config
from logger import logger


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

"""
Этот модуль инкапсулирует работу с API интернет‑эквайринга T‑Bank (Tinkoff).
Для всех вызовов необходим TerminalKey и пароль, которые считываются из
переменных окружения. Дополнительно можно задавать success/fail URL, URL
уведомлений и API токен.

Примечание: сумма передаётся в копейках (например, 100₽ = 10000).
"""


def _read_env() -> Tuple[str, str, str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Прочитать и провалидировать настройки окружения для T-Bank."""

    base_url = (config.T_PAY_BASE_URL or "https://securepay.tinkoff.ru/v2").rstrip("/")
    terminal_key = (config.T_PAY_TERMINAL_KEY or "").strip()
    password = (config.T_PAY_PASSWORD or "").strip()
    if not terminal_key or not password:
        raise RuntimeError("T_PAY_TERMINAL_KEY/T_PAY_PASSWORD не заданы")

    success_url = (config.T_PAY_SUCCESS_URL or "").strip() or None
    fail_url = (config.T_PAY_FAIL_URL or "").strip() or None
    notification_url = (config.TINKOFF_NOTIFY_URL or "").strip() or None
    api_token = (config.T_PAY_API_TOKEN or "").strip() or None

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


def _post_sync(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    base_url: str,
    terminal_key: str,
    password: str,
    api_token: Optional[str],
) -> Dict[str, Any]:
    """Синхронно выполнить POST‑запрос к T‑Bank через requests."""

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

    logger.info("T-Bank запрос: %s payload=%s", endpoint, body)
    try:
        response = requests.post(url, json=body, headers=headers, timeout=15)
    except requests.RequestException as err:  # noqa: PERF203
        logger.exception("T-Bank сеть: %s", err)
        raise TBankHttpError(f"NETWORK: {err}") from err

    content_type = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        preview = response.text[:500]
        logger.error(
            "T-Bank HTTP ошибка: %s status=%s body=%s", endpoint, response.status_code, preview
        )
        raise TBankHttpError(
            f"HTTP {response.status_code} {content_type or 'unknown'}: {preview}"
        )
    if "application/json" not in content_type.lower():
        preview = response.text[:500]
        logger.error(
            "T-Bank content-type ошибка: %s type=%s body=%s", endpoint, content_type, preview
        )
        raise TBankHttpError(
            f"Unexpected content-type {content_type or 'unknown'}: {preview}"
        )
    try:
        data = response.json()
    except ValueError as err:  # noqa: PERF203
        logger.exception("T-Bank JSON ошибка: %s", err)
        raise TBankHttpError("Не удалось разобрать JSON-ответ") from err

    if isinstance(data, dict) and data.get("Success") is False:
        logger.error(
            "T-Bank бизнес-ошибка: %s response=%s", endpoint, json.dumps(data, ensure_ascii=False)
        )
        raise TBankApiError(
            str(data.get("ErrorCode", "")),
            data.get("Message", ""),
            data.get("Details"),
        )
    logger.info("T-Bank ответ: %s response=%s", endpoint, json.dumps(data, ensure_ascii=False))
    return data


async def _post(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    base_url: str,
    terminal_key: str,
    password: str,
    api_token: Optional[str],
) -> Dict[str, Any]:
    """Асинхронно вызвать T‑Bank API через поток с requests."""

    return await asyncio.to_thread(
        _post_sync,
        endpoint,
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def net_diagnostics() -> Dict[str, Any]:
    """Выполнить сетевую диагностику доступности T-Bank."""

    result: Dict[str, Any] = {}
    try:
        result["local_ip"] = socket.gethostbyname(socket.gethostname())
    except Exception as err:  # noqa: BLE001
        result["local_ip"] = f"unresolved: {err}"

    try:
        loop = asyncio.get_running_loop()
        result["event_loop"] = str(loop)
    except RuntimeError:
        result["event_loop"] = "loop not running"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org", timeout=5) as resp:
                result["external_ip_status"] = resp.status
                result["external_ip"] = await resp.text()
    except Exception as err:  # noqa: BLE001
        result["external_ip_error"] = str(err)

    base_url = (config.T_PAY_BASE_URL or "https://securepay.tinkoff.ru/v2").rstrip("/")
    host = base_url.split("//", 1)[1].split("/", 1)[0]
    result["base_url"] = base_url
    result["base_host"] = host

    try:
        dns_entries = socket.getaddrinfo("rest-api-test.tinkoff.ru", 443)
        result["sandbox_dns_ok"] = True
        result["sandbox_dns"] = list({entry[4][0] for entry in dns_entries})
    except Exception as err:  # noqa: BLE001
        result["sandbox_dns_ok"] = False
        result["sandbox_dns_error"] = str(err)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/Init", timeout=5) as resp:
                result["probe_status"] = resp.status
                result["probe_ct"] = resp.headers.get("Content-Type")
                result["probe_body_peek"] = (await resp.text())[:200]
    except Exception as err:  # noqa: BLE001
        result["probe_error"] = str(err)

    logger.info("[NET] diag: %s", json.dumps(result, ensure_ascii=False))
    return result


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

    logger.info(
        "Init to %s | term_key_len=%s",
        f"{base_url}/Init",
        len(terminal_key),
    )

    payload: Dict[str, Any] = {
        "Amount": amount,
        "OrderId": order_id,
        "Description": description,
    }
    if not email and not phone:
        raise ValueError("Для формирования чека требуется email или телефон")
    # Формируем чек: либо используем переданный, либо собираем минимальный по ФФД 1.05
    if receipt is None:
        taxation = getattr(config, "T_PAY_TAXATION", None) or "usn_income"
        auto_receipt: Dict[str, Any] = {
            "FfdVersion": "1.05",
            "Taxation": taxation,
            "Items": [
                {
                    "Name": description,
                    "Price": amount,
                    "Quantity": 1,
                    "Amount": amount,
                    "PaymentMethod": "full_prepayment",
                    "PaymentObject": "service",
                    "Tax": "none",
                }
            ],
            "Payments": {"Electronic": amount},
        }
        if email:
            auto_receipt["Email"] = email
        if phone:
            auto_receipt["Phone"] = phone
        receipt_to_send = auto_receipt
    else:
        receipt_to_send = receipt
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
    payload["Receipt"] = receipt_to_send
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
        logger.error("NETWORK/HTTP: %s (проверьте whitelist/host)", err)
        raise
    except TBankApiError as err:
        logger.error("API: %s", err)
        raise

    return response


async def get_qr(payment_id: str, *, data_type: str = "PAYLOAD") -> Dict[str, Any]:
    """Получить QR-данные для оплаты через СБП."""

    if not payment_id:
        raise ValueError("PaymentId обязателен для GetQr")
    (
        base_url,
        terminal_key,
        password,
        *_
    ) = _read_env()
    payload: Dict[str, Any] = {
        "PaymentId": str(payment_id),
        "DataType": data_type or "PAYLOAD",
        "TerminalKey": terminal_key,
    }
    payload["Token"] = _generate_token(payload, password)
    url = f"{base_url}/GetQr"
    logger.info("GetQr запрос: %s payload=%s", url, json.dumps(payload, ensure_ascii=False))
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        ) as response:
            text = await response.text()
            if response.status != 200:
                logger.error("GetQr HTTP %s: %s", response.status, text[:200])
                raise TBankHttpError(f"GetQr HTTP {response.status}: {text[:100]}")
            data = json.loads(text)
    if not data.get("Success"):
        logger.error("GetQr ошибка: %s", json.dumps(data, ensure_ascii=False))
        raise TBankApiError(
            str(data.get("ErrorCode", "GetQr")),
            data.get("Message") or "GetQr вернул ошибку",
            data.get("Details"),
        )
    logger.info("GetQr ответ: %s", json.dumps(data, ensure_ascii=False))
    return data


async def get_add_account_qr_state(request_key: str) -> Dict[str, Any]:
    """Получить состояние привязки счёта (GetAddAccountQrState)."""

    if not request_key:
        raise ValueError("RequestKey обязателен для GetAddAccountQrState")
    (
        base_url,
        terminal_key,
        password,
        *_
    ) = _read_env()
    payload = {
        "TerminalKey": terminal_key,
        "RequestKey": request_key,
    }
    payload["Token"] = _generate_token(payload, password)
    url = f"{base_url}/GetAddAccountQrState"
    logger.info(
        "GetAddAccountQrState запрос: %s payload=%s",
        url,
        json.dumps(payload, ensure_ascii=False),
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        ) as response:
            text = await response.text()
            if response.status != 200:
                logger.error(
                    "GetAddAccountQrState HTTP %s: %s", response.status, text[:200]
                )
                raise TBankHttpError(
                    f"GetAddAccountQrState HTTP {response.status}: {text[:100]}"
                )
            data = json.loads(text)
    if not data.get("Success"):
        logger.error(
            "GetAddAccountQrState ошибка: %s", json.dumps(data, ensure_ascii=False)
        )
        raise TBankApiError(
            str(data.get("ErrorCode", "GetAddAccountQrState")),
            data.get("Message") or "Привязка счёта не подтверждена",
            data.get("Details"),
        )
    logger.info("GetAddAccountQrState ответ: %s", json.dumps(data, ensure_ascii=False))
    return data


async def send_closing_receipt(payment_id: str, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Отправить закрывающий чек по оплаченному платежу."""

    if not payment_id:
        raise ValueError("PaymentId обязателен для SendClosingReceipt")
    (
        _base_url,
        terminal_key,
        password,
        *_
    ) = _read_env()
    payload: Dict[str, Any] = {
        "TerminalKey": terminal_key,
        "PaymentId": str(payment_id),
        "Receipt": receipt,
    }
    payload["Token"] = _generate_token(payload, password)
    url = "https://securepay.tinkoff.ru/cashbox/SendClosingReceipt"
    logger.info(
        "SendClosingReceipt запрос: %s payload=%s",
        url,
        json.dumps(payload, ensure_ascii=False),
    )
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=15,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    logger.error("SendClosingReceipt HTTP %s: %s", response.status, text[:200])
                    raise TBankHttpError(
                        f"SendClosingReceipt HTTP {response.status}: {text[:100]}"
                    )
                data = json.loads(text)
        except aiohttp.ClientError as err:  # noqa: PERF203
            logger.exception("SendClosingReceipt: ошибка сети", exc_info=err)
            raise TBankHttpError(str(err)) from err

    if not data.get("Success"):
        logger.error("SendClosingReceipt ошибка: %s", json.dumps(data, ensure_ascii=False))
        raise TBankApiError(
            str(data.get("ErrorCode", "SendClosingReceipt")),
            data.get("Message") or "SendClosingReceipt вернул ошибку",
            data.get("Details"),
        )
    logger.info("SendClosingReceipt ответ: %s", json.dumps(data, ensure_ascii=False))
    return data


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
        logger.error("NETWORK/HTTP: %s (проверьте whitelist/host)", err)
        raise
    except TBankApiError as err:
        logger.error("API: %s", err)
        raise


async def charge_payment(
    *,
    payment_id: str,
    rebill_id: str,
    customer_key: Optional[str] = None,
    amount: Optional[int] = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """Выполнить рекуррентное списание через метод Charge."""

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
        "RebillId": rebill_id,
    }
    if customer_key:
        payload["CustomerKey"] = customer_key
    if amount:
        payload["Amount"] = amount
    if ip:
        payload["IP"] = ip

    return await _post(
        "Charge",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


def charge_saved_card(
    payment_id: str,
    rebill_id: str,
    ip: str,
    email: Optional[str] = None,
    send_email: bool = False,
) -> Dict[str, Any]:
    """Выполнить безакцептное списание по сохранённой карте."""

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
        "RebillId": rebill_id,
        "IP": ip,
    }

    info_email = (email or "").strip() or None
    if send_email:
        payload["SendEmail"] = True
        if info_email:
            payload["InfoEmail"] = info_email
        else:
            logger.warning(
                "Запрошена отправка чека по email для автосписания, но адрес отсутствует."
            )
    elif info_email:
        payload["InfoEmail"] = info_email

    url = f"{base_url}/Charge"
    body = payload.copy()
    body["TerminalKey"] = terminal_key
    body["Token"] = _generate_token(body, password)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ConciergeBot/Charge/1.0",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    logger.info(
        "Charge saved card: payment=%s rebill=%s", payment_id, rebill_id
    )

    try:
        response = requests.post(url, json=body, headers=headers, timeout=15)
    except requests.RequestException as err:  # noqa: PERF203
        logger.error("Charge saved card: ошибка сети %s", err)
        raise TBankHttpError(f"NETWORK: {err}") from err

    content_type = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        preview = response.text[:500]
        raise TBankHttpError(
            f"HTTP {response.status_code} {content_type or 'unknown'}: {preview}"
        )
    if "application/json" not in content_type.lower():
        preview = response.text[:500]
        raise TBankHttpError(
            f"Unexpected content-type {content_type or 'unknown'}: {preview}"
        )

    try:
        data = response.json()
    except ValueError as err:  # noqa: PERF203
        raise TBankHttpError("Не удалось разобрать JSON-ответ Charge") from err

    if isinstance(data, dict):
        if not data.get("Success"):
            logger.warning(
                "Charge saved card: отклонено %s",
                json.dumps(data, ensure_ascii=False)[:500],
            )
        else:
            logger.info(
                "Charge saved card: подтверждено PaymentId=%s статус=%s",
                data.get("PaymentId"),
                data.get("Status"),
            )

    return data


async def get_customer(customer_key: str) -> Dict[str, Any]:
    """Получить информацию о клиенте T-Bank по CustomerKey."""

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
        "CustomerKey": customer_key,
    }

    return await _post(
        "GetCustomer",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def add_customer(
    customer_key: str,
    *,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """Зарегистрировать клиента перед использованием рекуррентных платежей."""

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
        "CustomerKey": customer_key,
    }
    if email:
        payload["Email"] = email
    if phone:
        payload["Phone"] = phone
    if ip:
        payload["IP"] = ip

    return await _post(
        "AddCustomer",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def init_add_card(
    customer_key: str,
    ip: str,
    *,
    check_type: str = "3DSHOLD",
    resident_state: bool = True,
) -> Dict[str, Any]:
    """
    Инициировать привязку карты (метод AddCard) и получить ссылку формы.

    :param customer_key: идентификатор клиента T-Bank.
    :param ip: IP-адрес пользователя, передаваемый в AddCard.
    :param check_type: тип проверки карты (например, "3DSHOLD" или "NO").
    :param resident_state: статус резидентства для проверки.
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
        "CustomerKey": customer_key,
        "CheckType": check_type or "3DSHOLD",
        "ResidentState": bool(resident_state),
        "IP": ip,
    }

    return await _post(
        "AddCard",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def attach_card(
    request_key: str,
    card_data: str,
    *,
    device_channel: str = "02",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Подтвердить привязку карты через метод AttachCard."""

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
        "RequestKey": request_key,
        "CardData": card_data,
        "deviceChannel": device_channel,
    }
    if data:
        payload["DATA"] = data

    return await _post(
        "AttachCard",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def get_add_card_state(request_key: str) -> Dict[str, Any]:
    """Проверить состояние привязки карты и получить RebillId."""

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
        "RequestKey": request_key,
    }

    return await _post(
        "GetAddCardState",
        payload,
        base_url=base_url,
        terminal_key=terminal_key,
        password=password,
        api_token=api_token,
    )


async def add_account_qr(
    terminal_key: str,
    description: str,
    token: str,
    *,
    data_type: str = "PAYLOAD",
    bank_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    redirect_due_date: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], bool, str, str]:
    """Создать QR для СБП (AddAccountQr) и вернуть полезные данные ответа."""

    normalized_type = (data_type or "PAYLOAD").upper()
    url = "https://securepay.tinkoff.ru/v2/AddAccountQr"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if normalized_type == "IMAGE":
        headers["Accept"] = "image/svg"

    payload: Dict[str, Any] = {
        "TerminalKey": terminal_key,
        "Description": description,
        "DataType": normalized_type,
    }
    if bank_id:
        payload["BankId"] = bank_id
    if data:
        payload["Data"] = data
    if redirect_due_date:
        payload["RedirectDueDate"] = redirect_due_date

    # Подпись формируется из отсортированных параметров и секрета
    payload["Token"] = _generate_token(payload, token)

    logger.info(
        "AddAccountQr запрос: %s payload=%s",
        url,
        json.dumps(payload, ensure_ascii=False),
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, timeout=15) as response:
            response.raise_for_status()
            if normalized_type == "IMAGE":
                svg_data = await response.text()
                success = response.status == 200
                if not success:
                    logger.error("AddAccountQr IMAGE: HTTP %s", response.status)
                return svg_data, None, success, "0" if success else str(response.status), (
                    "" if success else "Ошибка HTTP при получении SVG"
                )
            data_json = await response.json()

    logger.info("AddAccountQr ответ: %s", json.dumps(data_json, ensure_ascii=False))

    success = bool(data_json.get("Success"))
    if not success:
        logger.error(
            "AddAccountQr: %s %s %s",
            data_json.get("ErrorCode"),
            data_json.get("Message"),
            data_json.get("Details"),
        )
    params = data_json.get("Params") or {}
    data_field = (
        params.get("Data")
        or params.get("Payload")
        or data_json.get("Data")
        or data_json.get("Payload")
    )
    request_key = data_json.get("RequestKey") or params.get("RequestKey")
    error_code = str(data_json.get("ErrorCode", ""))
    message = str(data_json.get("Message", ""))
    return data_field, request_key, success, error_code, message


async def charge_qr(
    payment_id: str,
    account_token: str,
    ip: str,
    *,
    send_email: bool = False,
    info_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Выполнить списание через ChargeQr по ранее привязанному счёту."""

    if not payment_id or not account_token:
        raise ValueError("Для ChargeQr необходимы PaymentId и AccountToken")

    base_url, terminal_key, password, *_ = _read_env()
    url = f"{base_url}/ChargeQr"
    normalized_email = (info_email or "").strip() or None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: Dict[str, Any] = {
        "TerminalKey": terminal_key,
        "PaymentId": str(payment_id),
        "AccountToken": str(account_token),
        "IP": ip or "",
    }
    if send_email:
        payload["SendEmail"] = True
        if not normalized_email:
            logger.warning(
                "ChargeQr: запрошена отправка email, но InfoEmail не указан"
            )
    if normalized_email:
        payload["InfoEmail"] = normalized_email

    payload["Token"] = _generate_token(payload, password)
    logger.info("ChargeQr: отправка запроса payment_id=%s", payment_id)
    logger.debug(
        "ChargeQr payload: %s",
        json.dumps({k: v for k, v in payload.items() if k != "Token"}, ensure_ascii=False),
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=20) as response:
                content_type = response.headers.get("Content-Type", "")
                text = await response.text()
                if response.status != 200:
                    logger.error(
                        "ChargeQr HTTP %s: %s", response.status, text[:500]
                    )
                    raise TBankHttpError(
                        f"ChargeQr HTTP {response.status}: {text[:200]}"
                    )
                if "application/json" not in content_type.lower():
                    logger.error(
                        "ChargeQr: неожиданный тип ответа %s", content_type or "unknown"
                    )
                    raise TBankHttpError(
                        f"ChargeQr: неожиданный тип ответа {content_type or 'unknown'}"
                    )
                data = json.loads(text)
    except TBankHttpError:
        raise
    except Exception as err:  # noqa: BLE001
        logger.exception("ChargeQr: ошибка сети", exc_info=err)
        raise TBankHttpError(str(err)) from err

    logger.debug("ChargeQr response: %s", json.dumps(data, ensure_ascii=False))
    success = bool(data.get("Success"))
    params = data.get("Params") or {}
    status = params.get("Status") or data.get("Status")
    if not success:
        error_code = str(data.get("ErrorCode", ""))
        message = data.get("Message") or data.get("Details") or "ChargeQr вернул ошибку"
        message_lower = message.lower()
        if "не поддерживает" in message_lower or "unsupported" in message_lower:
            logger.error(
                "ChargeQr не поддерживается терминалом %s", terminal_key
            )
        logger.error(
            "ChargeQr error: code=%s status=%s message=%s",
            error_code,
            status,
            message,
        )
        raise TBankApiError(error_code or "ChargeQr", message)

    logger.info(
        "ChargeQr: операция подтверждена payment_id=%s status=%s",
        data.get("PaymentId") or params.get("PaymentId") or payment_id,
        status or "неизвестно",
    )
    return {
        "Success": success,
        "Status": status,
        "OrderId": params.get("OrderId") or data.get("OrderId"),
        "PaymentId": params.get("PaymentId") or data.get("PaymentId") or payment_id,
        "Amount": params.get("Amount") or data.get("Amount"),
        "Currency": params.get("Currency") or data.get("Currency"),
        "Raw": data,
    }


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
    "charge_payment",
    "charge_saved_card",
    "get_customer",
    "add_customer",
    "init_add_card",
    "attach_card",
    "get_add_card_state",
    "get_qr",
    "get_add_account_qr_state",
    "send_closing_receipt",
    "add_account_qr",
    "charge_qr",
    "finish_authorize",
    "net_diagnostics",
]
