from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
import json
from typing import NamedTuple

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from db import DB
from logger import logger
from payments import charge_sbp_autopayment
from t_pay import TBankApiError, TBankHttpError, finalize_rebill, init_rebill_payment

DEFAULT_RECURRENT_IP = "127.0.0.1"
RETRY_PAYMENT_CALLBACK = "payment:retry"
MAX_EXPIRED_BATCH = 100
REMOVAL_RETRIES = 3
REMOVAL_RETRY_DELAY = 2
REMOVAL_THROTTLE_DELAY = 0.3
REMOVAL_CHUNK_SIZE = 25
REMOVAL_PARALLELISM = 5


FAILURE_MESSAGE = "Не удалось списать, автопродление отключено."
EXPIRED_MESSAGE = "Срок подписки истёк. Продлите, чтобы восстановить доступ."


class AutoRenewResult(NamedTuple):
    """Результат попытки автопродления."""

    success: bool
    attempted: bool
    amount: int
    user_notified: bool = False


def _load_admin_ids() -> list[int]:
    """Загрузить идентификаторы администраторов из файла авторизации."""

    path = (config.ADMIN_AUTH_FILE or "").strip()
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось прочитать файл администраторов", exc_info=err)
        return []
    raw_ids = payload.get("admins", [])
    result: list[int] = []
    for raw in raw_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        result.append(value)
    return result

def _retry_markup() -> InlineKeyboardMarkup:
    """Построить клавиатуру для повторного списания."""

    button = InlineKeyboardButton(text="🔄 Повторить платёж", callback_data=RETRY_PAYMENT_CALLBACK)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def _format_date(ts: int) -> str:
    """Вернуть строку даты в формате ДД.ММ.ГГГГ."""

    return datetime.utcfromtimestamp(ts).strftime("%d.%m.%Y")


def _next_month_date(now_ts: int) -> int:
    """Вернуть таймстамп через условные 30 дней от указанного момента."""

    future = datetime.utcfromtimestamp(now_ts) + timedelta(days=30)
    return int(future.timestamp())


async def _was_last_payment_sbp(db: DB, user_id: int) -> bool:
    """Понять, пользовался ли пользователь оплатой через СБП."""

    if user_id <= 0:
        return False
    try:
        payment = await db.get_latest_payment(user_id)
    except Exception:  # noqa: BLE001
        return False
    if not payment:
        return False
    try:
        method = str(payment["method"] or "").strip().lower()
    except (KeyError, TypeError, ValueError):
        return False
    return method == "sbp"


def _has_active_subscription(row) -> bool:
    """Понять, есть ли у пользователя платная подписка в истории."""

    if row is None:
        return False
    try:
        subscription_end = int(row.get("subscription_end_at") or 0)
    except (TypeError, ValueError, KeyError):
        subscription_end = 0
    return subscription_end > 0


async def _try_card_autorenew(bot: Bot, db: DB, row) -> bool:
    """Попытаться продлить подписку по карте через RebillId."""

    row_data = dict(row)
    user_id = int(row_data.get("user_id") or 0)
    if user_id <= 0:
        return False
    rebill_id = str(row_data.get("rebill_id") or "").strip()
    if not rebill_id:
        return False
    if not _has_active_subscription(row_data):
        return False

    try:
        last_payment = await db.get_latest_payment(user_id)
    except Exception:  # noqa: BLE001
        last_payment = None
    if not last_payment:
        return False
    try:
        months = int(last_payment.get("months") or 0)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        months = 0
    try:
        amount = int(last_payment.get("amount") or 0)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        amount = 0
    if months <= 0 or amount <= 0:
        return False

    try:
        payment_id = await init_rebill_payment(row_data, amount, months)
        state = await finalize_rebill(payment_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось выполнить автосписание по карте", exc_info=err)
        return False

    status = str(state.get("Status") or "").upper()
    if status == "CONFIRMED":
        await db.extend_subscription(user_id, months)
        await db.set_paid_only(user_id, False)
        await db.add_payment(
            user_id=user_id,
            payment_id=payment_id,
            order_id=f"card_rebill_{user_id}_{months}_{int(datetime.utcnow().timestamp())}",
            amount=amount,
            months=months,
            status="CONFIRMED",
            method="card",
            customer_key=str(row_data.get("customer_key") or ""),
        )
        await db.set_auto_renew(user_id, True)
        await bot.send_message(
            user_id,
            "✅ Списание по карте прошло успешно, подписка продлена.",
        )
        return True
    if status == "REJECTED":
        await db.set_auto_renew(user_id, False)
        await bot.send_message(
            user_id,
            "⚠️ Не удалось списать оплату по карте. Автопродление отключено.",
        )
        return False
    return False


async def try_auto_renew(
    bot: Bot,
    db: DB,
    user_row,
    now_ts: int | None = None,
    *,
    ip: str | None = None,
    force: bool = False,
) -> AutoRenewResult:
    """Попытаться продлить подписку пользователя через автосписание."""

    # Параметр force позволяет запускать списание вручную, даже если флаг auto_renew снят.

    row_dict = dict(user_row)
    user_id = int(row_dict.get("user_id", 0))
    auto_renew_flag = bool(row_dict.get("auto_renew"))
    test_interval = config.TEST_RENEW_INTERVAL_MINUTES
    account_token = (row_dict.get("account_token") or "").strip()
    if not account_token:
        account_token = (await db.get_account_token(user_id)) or ""
    if user_id <= 0:
        return AutoRenewResult(False, False, 0)
    should_attempt = auto_renew_flag or force
    if not should_attempt:
        return AutoRenewResult(False, False, 0)
    if not account_token:
        if should_attempt:
            await db.log_payment_attempt(
                user_id,
                "SKIPPED",
                "Нет account_token для автопродления через СБП",
                payment_type="sbp",
            )
        return AutoRenewResult(False, False, 0)

    months_to_extend = 1
    parent_amount = 0
    try:
        last_payment = await db.get_latest_payment(user_id)
    except Exception:  # noqa: BLE001
        last_payment = None
    if last_payment is not None:
        try:
            months_to_extend = int(last_payment.get("months", months_to_extend))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            months_to_extend = 1
        try:
            parent_amount = int(last_payment.get("amount", 0))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            parent_amount = 0
    if months_to_extend <= 0:
        months_to_extend = 1
    if parent_amount <= 0:
        await db.log_payment_attempt(
            user_id,
            "SKIPPED",
            "Не удалось определить сумму для автопродления через СБП",
            payment_type="sbp",
        )
        return AutoRenewResult(False, False, 0)

    if now_ts is None:
        now_ts = int(datetime.utcnow().timestamp())

    async def _notify_failure() -> bool:
        try:
            await bot.send_message(
                user_id,
                FAILURE_MESSAGE,
                reply_markup=_retry_markup(),
            )
            return True
        except Exception:
            logger.debug(
                "Не удалось уведомить пользователя %s об ошибке автосписания",
                user_id,
            )
            return False

    try:
        response = await charge_sbp_autopayment(
            user_id,
            months_to_extend,
            parent_amount,
            account_token,
            db=db,
            ip=ip or DEFAULT_RECURRENT_IP,
        )
    except (TBankHttpError, TBankApiError) as err:
        logger.warning("Автосписание через СБП отклонено: user=%s | %s", user_id, err)
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "FAILED", str(err), payment_type="sbp")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "Неожиданная ошибка автосписания через СБП для пользователя %s",
            user_id,
            exc_info=err,
        )
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "ERROR", str(err), payment_type="sbp")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)

    charge_response = response.get("charge_response") or {}
    status = (response.get("status") or charge_response.get("Status") or "").upper()
    success_flag = bool(charge_response.get("Success")) or status in {"CONFIRMED", "COMPLETED"}
    if not success_flag:
        info = json.dumps(charge_response or response, ensure_ascii=False)[:500]
        logger.warning("Автосписание через СБП неуспешно: user=%s | %s", user_id, info)
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "FAILED", info, payment_type="sbp")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)

    payment_id_value = response.get("payment_id") or charge_response.get("PaymentId")
    payment_id_str = str(payment_id_value).strip() if payment_id_value else ""

    if test_interval:
        await db.extend_subscription_minutes(user_id, test_interval)
    else:
        await db.extend_subscription(user_id, months_to_extend)
    try:
        await db.set_paid_only(user_id, False)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Не удалось сбросить флаг paid_only после автопродления для пользователя %s",
            user_id,
        )
    extended_until = await db.get_subscription_end(user_id)
    if not extended_until:
        base_dt = datetime.utcfromtimestamp(now_ts or int(datetime.utcnow().timestamp()))
        delta_dt = timedelta(minutes=test_interval) if test_interval else timedelta(days=30)
        extended_until = int((base_dt + delta_dt).timestamp())

    if payment_id_str:
        await db.set_payment_status(payment_id_str, "CONFIRMED")
        await db.set_payment_account_token(payment_id_str, account_token)

    await db.log_payment_attempt(
        user_id,
        "SUCCESS",
        json.dumps(charge_response or response, ensure_ascii=False)[:500],
        payment_type="sbp",
    )

    success_notified = False
    amount_text = (
        f"{parent_amount / 100:.2f}" if parent_amount and parent_amount % 100 == 0 else str(parent_amount)
    )
    try:
        await bot.send_message(
            user_id,
            f"✅ Списано {amount_text}₽, подписка продлена до {_format_date(extended_until)}",
        )
        success_notified = True
    except Exception:
        logger.debug("Не удалось отправить сообщение об успешном продлении пользователю %s", user_id)

    logger.info("Автопродление через СБП успешно: user=%s до %s", user_id, extended_until)
    return AutoRenewResult(True, True, max(0, parent_amount), success_notified)


async def _kick_user_with_retry(
    bot: Bot,
    chat_id: int,
    user_id: int,
) -> tuple[bool, str | None]:
    """Исключить пользователя из чата с повторными попытками при сетевых сбоях."""

    for attempt in range(REMOVAL_RETRIES):
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            return True, None
        except (TelegramForbiddenError, TelegramBadRequest) as err:
            logger.warning(
                "Ошибка Telegram API при удалении пользователя %s: %s",
                user_id,
                err,
            )
            return False, str(err)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            if attempt + 1 >= REMOVAL_RETRIES:
                logger.warning(
                    "Сетевая ошибка при удалении пользователя %s: %s",
                    user_id,
                    err,
                )
                return False, None
            await asyncio.sleep(REMOVAL_RETRY_DELAY)
        except Exception as err:  # noqa: BLE001
            logger.debug("Не удалось удалить пользователя %s из канала", user_id, exc_info=err)
            return False, None
    return False, None


async def daily_check(bot: Bot, db: DB):
    try:
        started_at = time.monotonic()
        now_ts = int(datetime.utcnow().timestamp())
        target_chat_id = await db.get_target_chat_id()
        if target_chat_id is None:
            logger.info("Пропуск проверки подписок: чат ещё не привязан.")
            return

        expired = await db.list_expired(now_ts)
        if len(expired) > MAX_EXPIRED_BATCH:
            logger.info(
                "Ограничение батча истёкших подписок: %s из %s",
                MAX_EXPIRED_BATCH,
                len(expired),
            )
            expired = expired[:MAX_EXPIRED_BATCH]
        auto_success_count = 0
        auto_fail_count = 0
        auto_success_amount = 0
        removed_count = 0
        skipped_count = 0
        error_count = 0
        semaphore = asyncio.Semaphore(REMOVAL_PARALLELISM)

        async def _process_row(row) -> None:
            nonlocal auto_success_count, auto_fail_count, auto_success_amount
            nonlocal removed_count, skipped_count, error_count
            user_id = int(row["user_id"])
            async with semaphore:
                renew_result = await try_auto_renew(bot, db, row, now_ts)
                if renew_result.success:
                    auto_success_count += 1
                    auto_success_amount += max(0, renew_result.amount)
                    skipped_count += 1
                    return
                if renew_result.attempted:
                    auto_fail_count += 1

                row_dict = dict(row)
                auto_flag = bool(row_dict.get("auto_renew"))
                if auto_flag:
                    await db.set_auto_renew(user_id, False)

                card_renewed = False
                if auto_flag and row_dict.get("rebill_id"):
                    try:
                        card_renewed = await _try_card_autorenew(bot, db, row_dict)
                    except Exception:  # noqa: BLE001
                        card_renewed = False
                if card_renewed:
                    skipped_count += 1
                    return

                sbp_recent = False
                if not renew_result.attempted and not auto_flag:
                    sbp_recent = await _was_last_payment_sbp(db, user_id)

                try:
                    await db.log_payment_attempt(
                        user_id,
                        "EXPIRED",
                        "Подписка неактивна, пользователь будет удалён",
                        payment_type="sbp" if sbp_recent else "card",
                    )
                except Exception:
                    logger.debug("Не удалось записать лог об удалении пользователя %s", user_id)

                try:
                    removed, removal_error = await _kick_user_with_retry(
                        bot,
                        target_chat_id,
                        user_id,
                    )
                except Exception as err:  # noqa: BLE001
                    logger.debug("Не удалось удалить пользователя %s из канала", user_id, exc_info=err)
                    removed = False
                    removal_error = None

                if removed:
                    removed_count += 1
                else:
                    error_count += 1

                try:
                    await db.set_pending_removal(user_id, not removed)
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        "Не удалось обновить признак pending_removal для пользователя %s",
                        user_id,
                        exc_info=err,
                    )
                if removal_error:
                    summary_text = (
                        "⚠️ Бот не смог удалить пользователя из канала.\n"
                        f"chat_id: {target_chat_id}\n"
                        f"user_id: {user_id}\n"
                        "Проверьте право «Бан пользователей» у бота."
                    )
                    for admin_id in _load_admin_ids():
                        try:
                            await bot.send_message(admin_id, summary_text)
                        except Exception:
                            logger.debug(
                                "Не удалось отправить администратору %s уведомление о правах бота",
                                admin_id,
                            )
                notify_text = None
                notify_markup = None
                if renew_result.attempted:
                    notify_text = FAILURE_MESSAGE
                    notify_markup = _retry_markup()
                    if renew_result.user_notified:
                        notify_text = None
                else:
                    if auto_flag:
                        notify_text = FAILURE_MESSAGE
                        notify_markup = _retry_markup()
                    else:
                        notify_text = EXPIRED_MESSAGE
                if notify_text:
                    try:
                        await bot.send_message(
                            user_id,
                            notify_text,
                            reply_markup=notify_markup,
                        )
                    except Exception:
                        logger.debug(
                            "Не удалось уведомить пользователя %s об окончании подписки",
                            user_id,
                        )
                await asyncio.sleep(REMOVAL_THROTTLE_DELAY)

        for start in range(0, len(expired), REMOVAL_CHUNK_SIZE):
            chunk = expired[start : start + REMOVAL_CHUNK_SIZE]
            await asyncio.gather(*(_process_row(row) for row in chunk))

        if auto_success_count or auto_fail_count:
            summary_lines = [
                "💳 Автосписания за последний цикл:",
                f"✅ Успешно: {auto_success_count}",
                f"⚠️ Ошибки: {auto_fail_count}",
            ]
            if auto_success_amount > 0:
                summary_lines.append(f"💰 Сумма: {auto_success_amount / 100:.2f} ₽")
            summary_text = "\n".join(summary_lines)
            for admin_id in _load_admin_ids():
                try:
                    await bot.send_message(admin_id, summary_text)
                except Exception:
                    logger.debug(
                        "Не удалось отправить администратору %s сводку автосписаний",
                        admin_id,
                    )
        logger.info(
            "Итог проверки подписок: удалено=%s пропущено=%s ошибок=%s",
            removed_count,
            skipped_count,
            error_count,
        )
        duration = time.monotonic() - started_at
        logger.info("Проверка подписок завершена за %.2f сек.", duration)
    except asyncio.CancelledError:
        return


def setup_scheduler(bot: Bot, db: DB, tz_name: str = "Europe/Moscow") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=pytz.timezone(tz_name))
    interval_minutes = config.TEST_RENEW_INTERVAL_MINUTES
    if interval_minutes:
        scheduler.add_job(
            daily_check,
            IntervalTrigger(minutes=interval_minutes),
            kwargs={"bot": bot, "db": db},
        )
    else:
        scheduler.add_job(
            daily_check,
            CronTrigger(hour=3, minute=10),
            kwargs={"bot": bot, "db": db},
        )
    scheduler.start()
    return scheduler


__all__ = [
    "daily_check",
    "setup_scheduler",
    "try_auto_renew",
    "RETRY_PAYMENT_CALLBACK",
]
