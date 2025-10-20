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
    if not config.BOT_TOKEN or not config.TARGET_CHAT_ID:
        raise SystemExit("Заполни BOT_TOKEN и TARGET_CHAT_ID в .env")

    db = DB(config.DB_PATH)
    await db.init()
    await db.ensure_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    await db.ensure_target_chat_id(config.TARGET_CHAT_ID)

    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # прокинем db в контекст
    dp["db"] = db

    # роутеры
    dp.include_router(router)

    # шедулер
    setup_scheduler(bot, db, tz_name=config.TIMEZONE)

    # старт
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
