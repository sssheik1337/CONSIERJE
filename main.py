# Подсказка по тестированию уведомлений T-Банка:
# 1) Запустите бота — он поднимет локальный HTTP-сервер на WEBHOOK_PORT.
# 2) Откройте туннель: ngrok http --domain=<ваш-поддомен>.ngrok-free.app <WEBHOOK_PORT>.
# 3) Пропишите TINKOFF_NOTIFY_URL=https://<ваш-поддомен>.ngrok-free.app/tbank_notify.
# 4) Создайте оплату и дождитесь нотификации от T-Банка.

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage

import payments
import t_pay
from config import config
from db import DB
from handlers import get_user_menu, handle_sbp_notification_payload, router
from logger import logger
from scheduler import setup_scheduler


def compute_token(payload: dict, password: str) -> str:
    """Вычислить подпись T-Банка по корневым полям."""

    items: list[tuple[str, str]] = []
    for key, value in payload.items():
        if key.lower() == "token":
            continue
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        items.append((str(key), str(value)))
    items.append(("Password", password))
    items.sort(key=lambda item: item[0])
    concatenated = "".join(value for _, value in items)
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


async def _notify_user_payment_confirmed(
    bot: Bot, db: DB, user_id: int, months: int, sbp_hint: bool = False
) -> None:
    """Отправить пользователю уведомление о продлении подписки."""

    try:
        user_row = await db.get_user(user_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить данные пользователя %s", user_id, exc_info=err)
        user_row = None

    expires_at = 0
    try:
        subscription_end = await db.get_subscription_end(user_id)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить конец подписки для уведомления", exc_info=err)
        subscription_end = None
    if subscription_end:
        expires_at = subscription_end
    elif user_row is not None:
        try:
            expires_at = int(user_row["expires_at"])
        except Exception:  # noqa: BLE001
            try:
                expires_at = int(user_row.get("expires_at", 0))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                expires_at = 0

    expiry_text = None
    if expires_at:
        expiry_text = datetime.utcfromtimestamp(expires_at).strftime("%d.%m.%Y %H:%M UTC")

    message_parts = [
        "✅ Оплата через T-Bank подтверждена.",
        f"Подписка продлена на {months} мес.",
    ]
    if expiry_text:
        message_parts.append(f"Новая дата окончания: {expiry_text}.")
    try:
        reply_markup = await get_user_menu(db, user_id)
    except Exception:  # noqa: BLE001
        reply_markup = None

    try:
        await bot.send_message(user_id, " ".join(message_parts), reply_markup=reply_markup)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось уведомить пользователя о подтверждении оплаты", exc_info=err)


async def tbank_notify(request: web.Request) -> web.Response:
    """Обработать уведомление от T-Bank о статусе платежа."""

    db: DB = request.app["db"]
    bot: Bot = request.app["bot"]
    now_ts = int(time.time())

    try:
        data = await request.json()
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось разобрать уведомление T-Bank", exc_info=err)
        return web.json_response({"ok": True})

    if not isinstance(data, dict):
        logger.warning("Webhook T-Bank получен в неверном формате: %s", data)
        return web.json_response({"ok": True})

    headers = dict(request.headers)

    logger.info("[TBank Webhook] Получено уведомление: %s", data)

    try:
        terminal_key = str(data.get("TerminalKey") or data.get("terminalKey") or "")
        if terminal_key != config.T_PAY_TERMINAL_KEY:
            logger.warning("Отклонён webhook T-Bank: некорректный TerminalKey")
            return web.Response(status=403)

        if config.TINKOFF_WEBHOOK_SECRET:
            secret_header = headers.get("X-Tbank-Secret") or headers.get("X-TBank-Secret")
            if secret_header != config.TINKOFF_WEBHOOK_SECRET:
                logger.warning("Отклонён webhook T-Bank: неверный X-Tbank-Secret")
                return web.Response(status=403)

        token = data.get("Token") or data.get("token")
        if token:
            expected = compute_token(data, config.T_PAY_PASSWORD)
            if expected != str(token):
                logger.warning("Отклонён webhook T-Bank: подпись не сошлась")
                return web.Response(status=403)
    except web.HTTPException:
        raise
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка при проверке подписи webhook T-Bank", exc_info=err)
        return web.json_response({"ok": True})

    payment_id = str(data.get("PaymentId") or data.get("paymentId") or "")
    order_id = str(data.get("OrderId") or data.get("orderId") or "")
    status_raw = str(data.get("Status") or data.get("status") or "")
    status_upper = status_raw.upper()

    logger.info(
        "Webhook от T-Bank: статус=%s payment_id=%s order_id=%s",
        status_upper or status_raw,
        payment_id or "-",
        order_id or "-",
    )

    try:
        event_id = await db.log_webhook_event(
            payment_id,
            order_id,
            status_upper,
            terminal_key,
            data,
            headers,
            now_ts,
            processed=0,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось записать webhook-событие", exc_info=err)
        event_id = 0

    processed = False
    sbp_link_processed = False
    account_token_saved = False

    try:
        target_payment_id = payment_id
        if not target_payment_id and order_id:
            payment_row = await db.get_payment_by_order_id(order_id)
            if payment_row:
                target_payment_id = payment_row["payment_id"]

        account_token_value = data.get("AccountToken") or data.get("accountToken")
        if account_token_value:
            logger.info("[TBank Webhook] AccountToken найден в уведомлении")

        if not sbp_link_processed and (
            data.get("AccountToken") or data.get("RequestKey")
        ):
            try:
                sbp_link_processed = await handle_sbp_notification_payload(
                    data, db, bot
                )
                if sbp_link_processed and account_token_value:
                    logger.info("[TBank Webhook] AccountToken обновлён")
                    account_token_saved = True
            except Exception as err:  # noqa: BLE001
                logger.exception("Ошибка обработки AccountToken", exc_info=err)

        if account_token_value and not account_token_saved:
            related_user_id = 0
            if target_payment_id:
                payment_row_for_token = await db.get_payment_by_payment_id(
                    target_payment_id
                )
                if payment_row_for_token:
                    related_user_id = int(payment_row_for_token.get("user_id") or 0)
            if not related_user_id and order_id:
                payment_row_for_token = await db.get_payment_by_order_id(order_id)
                if payment_row_for_token:
                    related_user_id = int(payment_row_for_token.get("user_id") or 0)
            if related_user_id:
                await db.save_account_token(
                    related_user_id, str(account_token_value)
                )
                await db.set_auto_renew(related_user_id, True)
                await db.update_sbp_status(related_user_id, status_upper or "ACTIVE")
                if target_payment_id:
                    await db.set_payment_account_token(
                        target_payment_id, str(account_token_value)
                    )
                logger.info("[TBank Webhook] AccountToken обновлён")

        if status_upper in {"CONFIRMED", "AUTHORIZED"} and target_payment_id:
            payment_before = await db.get_payment_by_payment_id(target_payment_id)
            was_confirmed = False
            if payment_before is not None:
                was_confirmed = (payment_before["status"] or "").upper() == "CONFIRMED"
            applied = await payments.apply_successful_payment(target_payment_id, db)
            processed = applied
            if applied:
                logger.info("[TBank Webhook] Оплата подтверждена")
                payment_row = await db.get_payment_by_payment_id(target_payment_id)
                stored_method = ""
                user_id = 0
                months = 0
                if payment_row:
                    user_id = int(payment_row["user_id"] or 0)
                    months = int(payment_row["months"] or 0)
                    try:
                        stored_method = str(payment_row["method"] or "").strip().lower()
                    except (KeyError, TypeError, ValueError):
                        stored_method = ""

                    payment_type = payments.detect_payment_type(data)
                    is_sbp = payment_type == "sbp" or stored_method == "sbp"

                    if user_id > 0 and months > 0 and not was_confirmed:
                        await _notify_user_payment_confirmed(
                            bot, db, user_id, months, sbp_hint=is_sbp
                        )
                    if user_id > 0:
                        try:
                            await db.set_payment_method(target_payment_id, payment_type)
                        except Exception as err:  # noqa: BLE001
                            logger.debug(
                                "Не удалось сохранить способ оплаты %s для платежа %s: %s",
                                payment_type,
                                target_payment_id,
                                err,
                            )
                        if account_token_value:
                            await db.save_account_token(user_id, str(account_token_value))
                            await db.set_auto_renew(user_id, True)
        elif status_upper:
            if target_payment_id:
                await db.set_payment_status(target_payment_id, status_upper)
                processed = True
            elif order_id:
                payment_row = await db.get_payment_by_order_id(order_id)
                if payment_row and payment_row["payment_id"]:
                    await db.set_payment_status(payment_row["payment_id"], status_upper)
                    processed = True
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка обработки webhook T-Bank", exc_info=err)
    finally:
        if processed and event_id:
            try:
                await db.mark_webhook_processed(event_id)
            except Exception as err:  # noqa: BLE001
                logger.exception("Не удалось пометить webhook как обработанный", exc_info=err)

    return web.json_response({"ok": True})


async def debug_net(request: web.Request) -> web.Response:
    """Вернуть результаты сетевой диагностики T-Bank."""

    try:
        data = await t_pay.net_diagnostics()
    except Exception as err:  # noqa: BLE001
        return web.Response(
            text=json.dumps({"error": str(err)}, ensure_ascii=False),
            content_type="application/json",
            status=500,
        )

    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


async def start_webhook_server(bot: Bot, db: DB) -> None:
    """Поднять aiohttp-сервер для приёма уведомлений T-Банка."""

    app = web.Application()
    app["db"] = db
    app["bot"] = bot
    app.router.add_get("/debug/net", debug_net)
    app.router.add_get("/health", lambda _: web.json_response({"status": "ok"}))
    app.router.add_post("/tbank_notify", tbank_notify)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.WEBHOOK_HOST, port=config.WEBHOOK_PORT)
    await site.start()
    logger.info(
        "Сервер уведомлений T-Bank запущен на %s:%s",
        config.WEBHOOK_HOST,
        config.WEBHOOK_PORT,
    )
    try:
        diagnostics = await t_pay.net_diagnostics()
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось выполнить сетевую диагностику T-Bank", exc_info=err)
    else:
        local_ip = diagnostics.get("local_ip") or "-"
        external_repr = (
            diagnostics.get("external_ip")
            or diagnostics.get("external_ip_error")
            or diagnostics.get("probe_error")
            or "-"
        )
        logger.info(
            "Диагностика сети при старте: local_ip=%s external=%s",
            local_ip,
            external_repr,
        )
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await site.stop()
        await runner.cleanup()
        raise


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("Заполни BOT_TOKEN в .env")

    effective_level = logging.getLevelName(logger.getEffectiveLevel())
    logger.info(
        "Запуск бота: база=%s, таймзона=%s, лог-уровень=%s",
        config.DB_PATH,
        config.TIMEZONE,
        effective_level,
    )

    db = DB(config.DB_PATH)
    await db.init()
    payments.set_db(db)

    bot = Bot(config.BOT_TOKEN)
    await bot.set_my_commands(
        [BotCommand(command="start", description="Главное меню")]
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp["db"] = db
    dp.include_router(router)
    setup_scheduler(bot, db, tz_name=config.TIMEZONE)

    webhook_task: Optional[asyncio.Task] = asyncio.create_task(start_webhook_server(bot, db))

    logger.info("Запускаем polling aiogram и фоновый сервер вебхуков")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if webhook_task:
            webhook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await webhook_task


if __name__ == "__main__":
    asyncio.run(main())
