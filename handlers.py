from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from datetime import datetime, timedelta
from config import config
from db import DB
from payments import process_payment

router = Router(name="core")

def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS


class AdminStates(StatesGroup):
    waiting_chat_username = State()


ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Привязать чат")]],
    resize_keyboard=True,
)


async def send_admin_menu(m: Message, db: DB):
    """Отправить основное админское меню со сводкой."""
    chat_username = await db.get_target_chat_username()
    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        chat_info = "Чат пока не привязан"
    else:
        if not chat_username:
            chat_info = f"Чат привязан: id {chat_id}"
        else:
            chat_info = f"Чат привязан: {chat_username} (id {chat_id})"
    text = (
        f"{chat_info}\n\n"
        "/set_trial_days <d>\n"
        "/set_paid_only <user_id> <on|off>\n"
        "/set_autorenew <user_id> <on|off>\n"
        "/bypass <user_id> <on|off>\n"
        "/price_list\n"
        "/invite <user_id> [hours]\n"
    )
    await m.answer(text, reply_markup=ADMIN_KEYBOARD)

@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, db: DB):
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days(config.TRIAL_DAYS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    paid_only = (m.from_user.id in config.PAID_ONLY_IDS)
    bypass = (m.from_user.id in config.ADMIN_BYPASS_IDS)
    await db.upsert_user(m.from_user.id, now_ts, trial_days, auto_renew_default, paid_only, bypass)

    # Добавить в канал, если не состоит
    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None:
        await m.answer("Вы зарегистрированы, но чат ещё не привязан. Дождитесь привязки администратором.")
        return
    try:
        # Индивидуальная одноразовая ссылка (24 часа)
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, expire_date=expire_ts)
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
    exp = row["expires_at"]
    dt = datetime.utcfromtimestamp(exp)
    ar = "вкл" if row["auto_renew"] else "выкл"
    po = "да" if row["paid_only"] else "нет"
    await m.answer(f"Подписка до: {dt} UTC\nАвтопродление: {ar}\nБез пробника: {po}")

@router.message(Command("rejoin"))
async def cmd_rejoin(m: Message, bot: Bot, db: DB):
    row = await db.get_user(m.from_user.id)
    if not row:
        await m.answer("Вы ещё не зарегистрированы. Нажмите /start.")
        return
    if row["expires_at"] < int(datetime.utcnow().timestamp()):
        await m.answer("Подписка не активна. Оплатите, чтобы вернуться.")
        return
    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None:
        await m.answer("Чат ещё не привязан. Свяжитесь с администратором.")
        return
    try:
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, expire_date=expire_ts)
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
    prices = await db.get_prices(config.PRICES)
    ok, msg = await process_payment(m.from_user.id, months, prices)
    if not ok:
        await m.answer("Оплата не прошла: " + msg)
        return
    await db.extend_subscription(m.from_user.id, months)
    await m.answer(msg + "\nПодписка продлена.")

# ==== Админские ====

@router.message(Command("admin"))
async def cmd_admin_help(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    await send_admin_menu(m, db)


@router.message(F.text == "Привязать чат")
async def admin_bind_chat_button(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.waiting_chat_username)
    await m.answer(
        "Пришлите @username канала или супергруппы, которую нужно привязать.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.waiting_chat_username)
async def admin_bind_chat_username(m: Message, bot: Bot, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip()
    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await m.answer("Привязка отменена.")
        await send_admin_menu(m, db)
        return
    if not text.startswith("@") or len(text) < 2:
        await m.answer("Нужно прислать username в формате @example. Попробуйте ещё раз или отправьте 'отмена'.")
        return
    username = text
    try:
        chat = await bot.get_chat(username)
    except Exception:
        await m.answer("Не удалось получить чат по этому username. Убедитесь, что бот имеет доступ, и повторите попытку.")
        return
    stored_username = f"@{chat.username}" if chat.username else username
    await db.set_target_chat_username(stored_username)
    await db.set_target_chat_id(chat.id)
    await state.clear()
    await m.answer(f"Чат {stored_username} (id {chat.id}) успешно привязан.")
    await send_admin_menu(m, db)

@router.message(Command("set_trial_days"))
async def cmd_set_trial_days(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await m.answer("Формат: /set_trial_days <дней>")
        return
    days = int(args[1])
    await db.set_trial_days(days)
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
async def cmd_price_list(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    prices = await db.get_prices(config.PRICES)
    lines = [f"{months} мес: {price}₽" for months, price in sorted(prices.items())]
    await m.answer("Прайс:\n" + "\n".join(lines))

@router.message(Command("invite"))
async def cmd_invite(m: Message, bot: Bot, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await m.answer("Формат: /invite <user_id> [hours]")
        return
    uid = int(args[1])
    hours = int(args[2]) if len(args) > 2 and args[2].isdigit() else 24
    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None:
        await m.answer("Чат ещё не привязан. Сначала используйте кнопку \"Привязать чат\".")
        return
    try:
        expire_ts = int((datetime.utcnow() + timedelta(hours=hours)).timestamp())
        link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, expire_date=expire_ts)
        await bot.send_message(uid, f"Ваша персональная ссылка (действует {hours}ч):\n{link.invite_link}")
        await m.answer("Отправил пользователю.")
    except Exception as e:
        await m.answer("Не удалось создать/отправить ссылку.")
