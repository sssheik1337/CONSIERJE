import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from db import DB
from handlers import router
from scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO)

async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("Заполни BOT_TOKEN в .env")

    db = DB(config.DB_PATH)
    await db.init()

    # Инициализация настроек по умолчанию, если их ещё нет в базе
    await db.get_trial_days(config.TRIAL_DAYS)
    await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    await db.get_prices(config.PRICES)

    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None and config.TARGET_CHAT_ID:
        await db.set_target_chat_id(config.TARGET_CHAT_ID)
        target_chat_id = config.TARGET_CHAT_ID
    if target_chat_id is None:
        logging.warning("Чат пока не привязан. Используйте админскую команду, чтобы привязать его.")

    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # прокинем db в контекст
    dp["db"] = db

    # роутеры
    dp.include_router(router)

    # шедулер
    setup_scheduler(bot, db, target_chat_id, tz_name=config.TIMEZONE)

    # старт
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
