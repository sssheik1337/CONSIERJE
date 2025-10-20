from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import pytz
from aiogram import Bot
from db import DB

async def daily_check(bot: Bot, db: DB):
    chat_id = await db.get_target_chat_id()
    if not chat_id:
        return
    now_ts = int(datetime.utcnow().timestamp())
    expired = await db.list_expired(now_ts)
    for row in expired:
        user_id = row["user_id"]
        # авто-продление?
        if row["auto_renew"]:
            # тут могла бы быть реальная попытка списания; пока — просто продлеваем на 1 мес.
            new_exp = await db.extend_subscription(user_id, months=1)
            exp_dt = datetime.utcfromtimestamp(new_exp) if new_exp else None
            try:
                if exp_dt:
                    await bot.send_message(
                        user_id,
                        "Подписка автопродлена на 1 месяц (заглушка оплаты).\n"
                        f"Новый срок действия: {exp_dt} UTC.\n"
                        "Автопродление: включено ✅",
                    )
                else:
                    await bot.send_message(user_id, "Подписка автопродлена. Автопродление: включено ✅")
            except Exception:
                pass
            continue

        # кик из канала/группы
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # чтобы мог войти позже по новой ссылке
        except Exception:
            pass
        try:
            await bot.send_message(user_id, "Срок подписки истёк. Оплатите, чтобы вернуться в канал.")
        except Exception:
            pass

def setup_scheduler(bot: Bot, db: DB, tz_name: str = "Europe/Moscow") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=pytz.timezone(tz_name))
    # Каждый день в 03:10 по локальному TZ
    scheduler.add_job(daily_check, CronTrigger(hour=3, minute=10), kwargs={"bot": bot, "db": db})
    scheduler.start()
    return scheduler
