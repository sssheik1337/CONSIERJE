import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from db import DB
from handlers import router
from scheduler import setup_scheduler


logging.basicConfig(level=logging.INFO)


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("Заполни BOT_TOKEN в .env")

    db = DB(config.DB_PATH)
    await db.init()

    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Сохраняем экземпляр базы данных в контексте диспетчера
    dp["db"] = db

    # Подключаем маршрутизатор с обработчиками
    dp.include_router(router)

    # Настраиваем планировщик с учётом часового пояса
    setup_scheduler(bot, db, tz_name=config.TIMEZONE)

    # Запускаем обработку обновлений
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
