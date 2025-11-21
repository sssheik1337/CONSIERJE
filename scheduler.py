from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from typing import NamedTuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from db import DB
from logger import logger
from payments import SBP_NOTE
from t_pay import TBankHttpError, charge_saved_card

DEFAULT_RECURRENT_IP = "127.0.0.1"
RETRY_PAYMENT_CALLBACK = "payment:retry"


FAILURE_MESSAGE = "Не удалось продлить подписку. 🔄 Повторить платёж"
EXPIRED_MESSAGE = "Срок подписки истёк. Продлите, чтобы восстановить доступ."


class AutoRenewResult(NamedTuple):
    """Результат попытки автопродления."""

    success: bool
    attempted: bool
    amount: int
    user_notified: bool = False

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
    rebill_id = (row_dict.get("rebill_id") or "").strip()
    parent_payment = (row_dict.get("rebill_parent_payment") or "").strip()
    if user_id <= 0:
        return AutoRenewResult(False, False, 0)
    should_attempt = auto_renew_flag or force
    if not should_attempt:
        return AutoRenewResult(False, False, 0)
    customer_key = (row_dict.get("customer_key") or "").strip()
    if should_attempt and not customer_key:
        customer_key = str(user_id)
        try:
            await db.set_customer_key(user_id, customer_key)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Не удалось автоматически сохранить CustomerKey для пользователя %s", user_id
            )

    parent_amount = 0
    parent_months = 1
    parent_payment_row = None
    if parent_payment:
        try:
            parent_payment_row = await db.get_payment_by_payment_id(parent_payment)
        except Exception:  # noqa: BLE001
            parent_payment_row = None
        if parent_payment_row is not None:
            row_data = dict(parent_payment_row)
            try:
                parent_amount = int(row_data.get("amount", 0))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                parent_amount = 0
            try:
                parent_months = int(row_data.get("months", 1))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                parent_months = 1
            if parent_months <= 0:
                parent_months = 1

    if not rebill_id or not customer_key or not parent_payment:
        if should_attempt:
            missing = []
            if not rebill_id:
                missing.append("RebillId")
            if not customer_key:
                missing.append("CustomerKey")
            if not parent_payment:
                missing.append("родительский платёж")
            reason = "Недостаточно данных для автосписания"
            if missing:
                reason = f"{reason}: {', '.join(missing)}"
            await db.log_payment_attempt(user_id, "SKIPPED", reason, payment_type="card")
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

    user_email = (row_dict.get("email") or "").strip() or None

    try:
        response = await asyncio.to_thread(
            charge_saved_card,
            parent_payment,
            rebill_id,
            ip or DEFAULT_RECURRENT_IP,
            user_email,
            False,
        )
    except TBankHttpError as err:
        logger.warning("Автосписание отклонено: user=%s | %s", user_id, err)
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "FAILED", str(err), payment_type="card")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)
    except Exception as err:  # noqa: BLE001
        logger.exception("Неожиданная ошибка автосписания для пользователя %s", user_id, exc_info=err)
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "ERROR", str(err), payment_type="card")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)

    status = (response.get("Status") or "").upper()
    success_flag = bool(response.get("Success"))
    if status not in {"CONFIRMED", "COMPLETED"} and not success_flag:
        info = json.dumps(response, ensure_ascii=False)[:500]
        logger.warning("Автосписание неуспешно: user=%s | %s", user_id, info)
        await db.set_auto_renew(user_id, False)
        await db.log_payment_attempt(user_id, "FAILED", info, payment_type="card")
        notified = await _notify_failure()
        return AutoRenewResult(False, True, 0, notified)

    new_parent_payment = response.get("PaymentId") or parent_payment
    new_payment_id_str = str(new_parent_payment).strip() if new_parent_payment else ""
    if new_payment_id_str:
        await db.set_rebill_parent_payment(user_id, new_payment_id_str)

    months_to_extend = max(1, parent_months)
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
        extended_until = _next_month_date(now_ts)

    effective_payment_id = new_payment_id_str or parent_payment
    if new_payment_id_str and new_payment_id_str != parent_payment:
        order_id = f"auto_{user_id}_{now_ts}"
        try:
            await db.add_payment(
                user_id=user_id,
                payment_id=new_payment_id_str,
                order_id=order_id,
                amount=parent_amount,
                months=months_to_extend,
                status="CONFIRMED",
                method="card",
            )
        except Exception as err:  # noqa: BLE001
            logger.debug("Не удалось сохранить запись об автосписании: %s", err)
        effective_payment_id = new_payment_id_str

    if effective_payment_id:
        await db.set_payment_status(effective_payment_id, "CONFIRMED")

    await db.log_payment_attempt(
        user_id,
        "SUCCESS",
        json.dumps(response, ensure_ascii=False)[:500],
        payment_type="card",
    )

    success_notified = False
    try:
        await bot.send_message(
            user_id,
            f"✅ Подписка успешно продлена до {_format_date(extended_until)}",
        )
        success_notified = True
    except Exception:
        logger.debug("Не удалось отправить сообщение об успешном продлении пользователю %s", user_id)

    logger.info("Автопродление успешно: user=%s до %s", user_id, extended_until)
    return AutoRenewResult(True, True, max(0, parent_amount), success_notified)


async def daily_check(bot: Bot, db: DB):
    try:
        now_ts = int(datetime.utcnow().timestamp())
        target_chat_id = await db.get_target_chat_id()
        if target_chat_id is None:
            logger.info("Пропуск проверки подписок: чат ещё не привязан.")
            return

        expired = await db.list_expired(now_ts)
        auto_success_count = 0
        auto_fail_count = 0
        auto_success_amount = 0
        for row in expired:
            user_id = int(row["user_id"])

            renew_result = await try_auto_renew(bot, db, row, now_ts)
            if renew_result.success:
                auto_success_count += 1
                auto_success_amount += max(0, renew_result.amount)
                continue
            if renew_result.attempted:
                auto_fail_count += 1

            row_dict = dict(row)
            auto_flag = bool(row_dict.get("auto_renew"))
            if auto_flag:
                await db.set_auto_renew(user_id, False)

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
                await bot.ban_chat_member(target_chat_id, user_id)
                await bot.unban_chat_member(target_chat_id, user_id)
            except Exception:
                logger.debug("Не удалось удалить пользователя %s из канала", user_id)
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
                    if sbp_recent:
                        notify_text = f"{notify_text}\n\n{SBP_NOTE}"
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

        if auto_success_count or auto_fail_count:
            summary_lines = [
                "💳 Автосписания за последний цикл:",
                f"✅ Успешно: {auto_success_count}",
                f"⚠️ Ошибки: {auto_fail_count}",
            ]
            if auto_success_amount > 0:
                summary_lines.append(f"💰 Сумма: {auto_success_amount / 100:.2f} ₽")
            summary_text = "\n".join(summary_lines)
            for admin_id in config.SUPER_ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, summary_text)
                except Exception:
                    logger.debug(
                        "Не удалось отправить администратору %s сводку автосписаний",
                        admin_id,
                    )
    except asyncio.CancelledError:
        return


def setup_scheduler(bot: Bot, db: DB, tz_name: str = "Europe/Moscow") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=pytz.timezone(tz_name))
    scheduler.add_job(daily_check, CronTrigger(hour=3, minute=10), kwargs={"bot": bot, "db": db})
    scheduler.start()
    return scheduler


__all__ = [
    "daily_check",
    "setup_scheduler",
    "try_auto_renew",
    "RETRY_PAYMENT_CALLBACK",
]
