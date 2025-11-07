import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import payments
import t_pay  # noqa: F401
from config import config
from db import DB
from handlers import router
from scheduler import setup_scheduler


logging.basicConfig(level=logging.INFO)


async def handle_tbank_notify(request: web.Request) -> web.Response:
    """Обработать уведомление от T-Bank о статусе платежа."""

    db: DB = request.app["db"]
    bot: Bot = request.app["bot"]
    try:
        data = await request.json()
    except Exception as err:  # noqa: BLE001
        logging.exception("Не удалось разобрать уведомление T-Bank", exc_info=err)
        return web.Response(status=400)

    payment_id = data.get("PaymentId")
    status = (data.get("Status") or "").upper()
    if not payment_id:
        logging.warning("Получено уведомление T-Bank без PaymentId")
        return web.Response(status=400)

    payment = await db.get_payment_by_id(payment_id)
    if payment is None:
        logging.warning("Платёж %s не найден в базе", payment_id)
        if status:
            await db.set_payment_status(payment_id, status)
        return web.Response(status=200)

    current_status = (payment["status"] or "").upper()
    if status:
        await db.set_payment_status(payment_id, status)

    if status != "CONFIRMED" or current_status == "CONFIRMED":
        return web.Response(status=200)

    user_id = int(payment["user_id"])
    months = int(payment["months"])
    await db.extend_subscription(user_id, months)
    await db.set_paid_only(user_id, False)

    user_after = await db.get_user(user_id)
    expires_at = user_after["expires_at"] if user_after else 0
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
        await bot.send_message(user_id, " ".join(message_parts))
    except Exception as err:  # noqa: BLE001
        logging.exception("Не удалось уведомить пользователя о подтверждении оплаты", exc_info=err)

    return web.Response(status=200)


async def setup_tbank_server(bot: Bot, db: DB) -> Optional[Tuple[web.AppRunner, web.TCPSite]]:
    """Запустить сервер обработки уведомлений T-Bank, если указан URL."""

    if not config.TINKOFF_NOTIFY_URL:
        logging.info("TINKOFF_NOTIFY_URL не задан. Уведомления T-Bank отключены.")
        return None

    parsed = urlparse(config.TINKOFF_NOTIFY_URL)
    path = parsed.path or "/tbank_notify"
    app = web.Application()
    app["db"] = db
    app["bot"] = bot
    app.router.add_post(path, handle_tbank_notify)

    runner = web.AppRunner(app)
    await runner.setup()

    host = os.getenv("TINKOFF_NOTIFY_HOST", "0.0.0.0")
    port_raw = os.getenv("TINKOFF_NOTIFY_PORT")
    try:
        port = int(port_raw) if port_raw else 8080
    except ValueError:
        logging.warning("Некорректное значение TINKOFF_NOTIFY_PORT=%s, используется 8080", port_raw)
        port = 8080

    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.info("Эндпоинт T-Bank запущен на %s:%s%s", host, port, path)
    return runner, site


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("Заполни BOT_TOKEN в .env")

    db = DB(config.DB_PATH)
    await db.init()
    payments.set_db(db)

    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Сохраняем экземпляр базы данных в контексте диспетчера
    dp["db"] = db

    # Подключаем маршрутизатор с обработчиками
    dp.include_router(router)

    # Настраиваем планировщик с учётом часового пояса
    setup_scheduler(bot, db, tz_name=config.TIMEZONE)

    runner_site = await setup_tbank_server(bot, db)

    try:
        # Запускаем обработку обновлений
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if runner_site:
            runner, site = runner_site
            await site.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
