from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta
from config import config
from db import DB
from payments import process_payment

router = Router(name="core")

ADMIN_HELP_TEXT = (
    "/set_trial_days <d>\n"
    "/set_paid_only <user_id> <on|off>\n"
    "/set_autorenew <user_id> <on|off>\n"
    "/bypass <user_id> <on|off>\n"
    "/price_list\n"
    "/invite <user_id> [hours]\n"
)

def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS

def build_user_autorenew_keyboard(enabled: bool):
    builder = InlineKeyboardBuilder()
    emoji = "✅" if enabled else "❌"
    builder.button(text=f"Автопродление {emoji}", callback_data="user_toggle_autorenew")
    builder.adjust(1)
    return builder.as_markup()

def build_admin_keyboard(default_autorenew: bool):
    builder = InlineKeyboardBuilder()
    emoji = "✅" if default_autorenew else "❌"
    builder.button(
        text=f"Автопродление по умолчанию {emoji}",
        callback_data="admin_toggle_autorenew_default",
    )
    builder.adjust(1)
    return builder.as_markup()

def format_status_message(row) -> str:
    exp = datetime.utcfromtimestamp(row["expires_at"])
    ar = "вкл" if row["auto_renew"] else "выкл"
    po = "да" if row["paid_only"] else "нет"
    return f"Подписка до: {exp} UTC\nАвтопродление: {ar}\nБез пробника: {po}"

@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, db: DB):
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days_global(config.TRIAL_DAYS)
    paid_only = (m.from_user.id in config.PAID_ONLY_IDS)
    bypass = (m.from_user.id in config.ADMIN_BYPASS_IDS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    await db.upsert_user(m.from_user.id, now_ts, trial_days, auto_renew_default, paid_only, bypass)

    # Добавить в канал, если не состоит
    try:
        # Индивидуальная одноразовая ссылка (24 часа)
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(config.TARGET_CHAT_ID, member_limit=1, expire_date=expire_ts)
        await m.answer(
            "Добро пожаловать! Вот ваша одноразовая ссылка в канал (действует 24 часа):\n" + link.invite_link
        )
    except Exception as e:
        await m.answer("Не удалось создать ссылку. Напишите админу.")

@router.message(Command("status"))
async def cmd_status(m: Message, db: DB):
    row = await db.get_user(m.from_user.id)
    if not row:
        await m.answer("Вы ещё не зарегистрированы. Нажмите /start.")
        return
    text = format_status_message(row)
    keyboard = build_user_autorenew_keyboard(bool(row["auto_renew"]))
    await m.answer(text, reply_markup=keyboard)

@router.message(Command("rejoin"))
async def cmd_rejoin(m: Message, bot: Bot, db: DB):
    row = await db.get_user(m.from_user.id)
    if not row:
        await m.answer("Вы ещё не зарегистрированы. Нажмите /start.")
        return
    if row["expires_at"] < int(datetime.utcnow().timestamp()):
        await m.answer("Подписка не активна. Оплатите, чтобы вернуться.")
        return
    try:
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(config.TARGET_CHAT_ID, member_limit=1, expire_date=expire_ts)
        await m.answer("Ваша одноразовая ссылка (24ч):\n" + link.invite_link)
    except Exception:
        await m.answer("Не удалось создать ссылку. Напишите админу.")

@router.message(Command("buy"))
async def cmd_buy(m: Message, db: DB):
    args = (m.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await m.answer("Формат: /buy <месяцев>. Пример: /buy 1")
        return
    months = int(args[1])
    ok, msg = await process_payment(m.from_user.id, months, config.PRICES)
    if not ok:
        await m.answer("Оплата не прошла: " + msg)
        return
    new_exp = await db.extend_subscription(m.from_user.id, months)
    if new_exp:
        dt = datetime.utcfromtimestamp(new_exp)
        await m.answer(msg + f"\nПодписка продлена до: {dt} UTC.")
    else:
        await m.answer(msg + "\nПодписка продлена.")

# ==== Админские ====

@router.message(Command("admin"))
async def cmd_admin_help(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    default_auto = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    keyboard = build_admin_keyboard(default_auto)
    await m.answer(ADMIN_HELP_TEXT, reply_markup=keyboard)

@router.message(Command("set_trial_days"))
async def cmd_set_trial_days(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await m.answer("Формат: /set_trial_days <дней>")
        return
    days = int(args[1])
    await db.set_trial_days_global(days)
    await m.answer(f"OK. Пробный период установлен: {days} дн.")

@router.message(Command("set_paid_only"))
async def cmd_set_paid_only(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on","off"):
        await m.answer("Формат: /set_paid_only <user_id> <on|off>")
        return
    uid = int(args[1]); flag = (args[2]=="on")
    await db.set_paid_only(uid, flag)
    await m.answer(f"OK. user_id={uid} paid_only={flag}")

@router.message(Command("set_autorenew"))
async def cmd_set_autorenew(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on","off"):
        await m.answer("Формат: /set_autorenew <user_id> <on|off>")
        return
    uid = int(args[1]); flag = (args[2]=="on")
    await db.set_auto_renew(uid, flag)
    await m.answer(f"OK. user_id={uid} auto_renew={flag}")

@router.message(Command("bypass"))
async def cmd_bypass(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on","off"):
        await m.answer("Формат: /bypass <user_id> <on|off>")
        return
    uid = int(args[1]); flag = (args[2]=="on")
    await db.set_bypass(uid, flag)
    await m.answer(f"OK. user_id={uid} bypass={flag}")

@router.message(Command("price_list"))
async def cmd_price_list(m: Message):
    if not is_super_admin(m.from_user.id):
        return
    lines = [f"{m} мес: {p}₽" for m,p in sorted(config.PRICES.items())]
    await m.answer("Прайс:\n" + "\n".join(lines))

@router.message(Command("invite"))
async def cmd_invite(m: Message, bot: Bot):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await m.answer("Формат: /invite <user_id> [hours]")
        return
    uid = int(args[1])
    hours = int(args[2]) if len(args) > 2 and args[2].isdigit() else 24
    try:
        from datetime import datetime, timedelta
        expire_ts = int((datetime.utcnow() + timedelta(hours=hours)).timestamp())
        link = await bot.create_chat_invite_link(config.TARGET_CHAT_ID, member_limit=1, expire_date=expire_ts)
        await bot.send_message(uid, f"Ваша персональная ссылка (действует {hours}ч):\n{link.invite_link}")
        await m.answer("Отправил пользователю.")
    except Exception as e:
        await m.answer("Не удалось создать/отправить ссылку.")

@router.callback_query(F.data == "user_toggle_autorenew")
async def cb_toggle_autorenew(callback: CallbackQuery, db: DB):
    row = await db.get_user(callback.from_user.id)
    if not row:
        await callback.answer("Вы ещё не зарегистрированы. Нажмите /start.", show_alert=True)
        return
    current = bool(row["auto_renew"])
    new_flag = not current
    await db.set_auto_renew(callback.from_user.id, new_flag)
    updated_row = await db.get_user(callback.from_user.id)
    if updated_row and callback.message:
        text = format_status_message(updated_row)
        keyboard = build_user_autorenew_keyboard(bool(updated_row["auto_renew"]))
        await callback.message.edit_text(text, reply_markup=keyboard)
    notice = "Автопродление включено" if new_flag else "Автопродление выключено"
    await callback.answer(notice)

@router.callback_query(F.data == "admin_toggle_autorenew_default")
async def cb_admin_toggle_autorenew_default(callback: CallbackQuery, db: DB):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    current = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    new_flag = not current
    await db.set_auto_renew_default(new_flag)
    keyboard = build_admin_keyboard(new_flag)
    if callback.message:
        await callback.message.edit_text(ADMIN_HELP_TEXT, reply_markup=keyboard)
    notice = "Автопродление по умолчанию включено" if new_flag else "Автопродление по умолчанию выключено"
    await callback.answer(notice)
