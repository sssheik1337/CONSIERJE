from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from datetime import datetime, timedelta
from config import config
from db import DB
from payments import process_payment

router = Router(name="core")


def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS


class AdminPromoTrialStates(StatesGroup):
    ожидание_количества = State()
    ожидание_длительности = State()


class UserPromoStates(StatesGroup):
    ожидание_кода = State()


def build_admin_menu_markup() -> InlineKeyboardMarkup:
    """Построить клавиатуру админ-панели."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сгенерировать промокоды (trial)", callback_data="admin_generate_trial")],
        ]
    )


def build_user_menu_markup() -> InlineKeyboardMarkup:
    """Построить клавиатуру для пользователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ввести промокод", callback_data="user_enter_promo")]]
    )


async def handle_promo_application(message: Message, db: DB, code: str, state: FSMContext):
    """Общая логика применения промокода."""
    normalized = (code or "").strip().upper()
    if not normalized:
        await message.answer("Пожалуйста, отправьте непустой промокод.")
        return
    now_ts = int(datetime.utcnow().timestamp())
    user = await db.get_user(message.from_user.id)
    if user and user["expires_at"] and user["expires_at"] > now_ts:
        await message.answer("Промокод недоступен: у вас уже активная подписка.")
        await state.clear()
        return
    promo = await db.validate_promo_code(normalized, now_ts)
    if not promo:
        await message.answer("Промокод не найден, исчерпан или истёк.")
        await state.clear()
        return
    if promo["code_type"] != "trial":
        await message.answer("Этот промокод не поддерживается.")
        await state.clear()
        return
    trial_days = promo["extension_days"] or await db.get_trial_days_global(config.TRIAL_DAYS)
    await db.set_trial_period(message.from_user.id, now_ts, trial_days, config.AUTO_RENEW_DEFAULT)
    await db.mark_promo_redeemed(normalized, message.from_user.id, now_ts)
    expire_dt = datetime.utcfromtimestamp(now_ts + int(timedelta(days=trial_days).total_seconds()))
    await message.answer(
        "Промокод принят! Пробный период активен до {:%Y-%m-%d %H:%M:%S} UTC.".format(expire_dt)
    )
    await state.clear()


@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, db: DB):
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days_global(config.TRIAL_DAYS)
    paid_only = (m.from_user.id in config.PAID_ONLY_IDS)
    bypass = (m.from_user.id in config.ADMIN_BYPASS_IDS)
    await db.upsert_user(m.from_user.id, now_ts, trial_days, config.AUTO_RENEW_DEFAULT, paid_only, bypass)

    # Добавить в канал, если не состоит
    try:
        # Индивидуальная одноразовая ссылка (24 часа)
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(config.TARGET_CHAT_ID, member_limit=1, expire_date=expire_ts)
        await m.answer(
            "Добро пожаловать! Вот ваша одноразовая ссылка в канал (действует 24 часа):\n" + link.invite_link,
            reply_markup=build_user_menu_markup(),
        )
    except Exception:
        await m.answer("Не удалось создать ссылку. Напишите админу.")
        await m.answer("Вы также можете ввести промокод через кнопку ниже.", reply_markup=build_user_menu_markup())


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
    await db.extend_subscription(m.from_user.id, months)
    await m.answer(msg + "\nПодписка продлена.")


# ==== Админские ====


@router.message(Command("admin"))
async def cmd_admin_help(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    await m.answer(
        "/set_trial_days <d>\n"
        "/set_paid_only <user_id> <on|off>\n"
        "/set_autorenew <user_id> <on|off>\n"
        "/bypass <user_id> <on|off>\n"
        "/price_list\n"
        "/invite <user_id> [hours]\n",
        reply_markup=build_admin_menu_markup(),
    )


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
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on", "off"):
        await m.answer("Формат: /set_paid_only <user_id> <on|off>")
        return
    uid = int(args[1])
    flag = (args[2] == "on")
    await db.set_paid_only(uid, flag)
    await m.answer(f"OK. user_id={uid} paid_only={flag}")


@router.message(Command("set_autorenew"))
async def cmd_set_autorenew(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on", "off"):
        await m.answer("Формат: /set_autorenew <user_id> <on|off>")
        return
    uid = int(args[1])
    flag = (args[2] == "on")
    await db.set_auto_renew(uid, flag)
    await m.answer(f"OK. user_id={uid} auto_renew={flag}")


@router.message(Command("bypass"))
async def cmd_bypass(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or args[2] not in ("on", "off"):
        await m.answer("Формат: /bypass <user_id> <on|off>")
        return
    uid = int(args[1])
    flag = (args[2] == "on")
    await db.set_bypass(uid, flag)
    await m.answer(f"OK. user_id={uid} bypass={flag}")


@router.message(Command("price_list"))
async def cmd_price_list(m: Message):
    if not is_super_admin(m.from_user.id):
        return
    lines = [f"{m} мес: {p}₽" for m, p in sorted(config.PRICES.items())]
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
        expire_ts = int((datetime.utcnow() + timedelta(hours=hours)).timestamp())
        link = await bot.create_chat_invite_link(config.TARGET_CHAT_ID, member_limit=1, expire_date=expire_ts)
        await bot.send_message(uid, f"Ваша персональная ссылка (действует {hours}ч):\n{link.invite_link}")
        await m.answer("Отправил пользователю.")
    except Exception:
        await m.answer("Не удалось создать/отправить ссылку.")


@router.callback_query(F.data == "admin_generate_trial")
async def cb_admin_generate_trial(c: CallbackQuery, state: FSMContext):
    if not is_super_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminPromoTrialStates.ожидание_количества)
    await c.message.answer("Введите количество промокодов для генерации:")
    await c.answer()


@router.message(AdminPromoTrialStates.ожидание_количества)
async def admin_promo_count(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        await m.answer("Нет доступа.")
        await state.clear()
        return
    text = (m.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await m.answer("Введите положительное число — количество промокодов.")
        return
    await state.update_data(count=int(text))
    default_trial = await db.get_trial_days_global(config.TRIAL_DAYS)
    await state.set_state(AdminPromoTrialStates.ожидание_длительности)
    await m.answer(
        "Введите длительность пробного периода в днях."
        f" Отправьте 0, чтобы использовать текущее значение trial_days ({default_trial})."
    )


@router.message(AdminPromoTrialStates.ожидание_длительности)
async def admin_promo_duration(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        await m.answer("Нет доступа.")
        await state.clear()
        return
    text = (m.text or "").strip()
    if not text.isdigit() or int(text) < 0:
        await m.answer("Введите неотрицательное число дней либо 0 для значения по умолчанию.")
        return
    data = await state.get_data()
    count = int(data.get("count", 0))
    default_trial = await db.get_trial_days_global(config.TRIAL_DAYS)
    days = default_trial if int(text) == 0 else int(text)
    codes = await db.generate_promo_codes("trial", count, days)
    await state.clear()
    formatted = "\n".join(codes)
    await m.answer(f"Сгенерировано {len(codes)} промокодов (trial, {days} дн.):\n{formatted}")


@router.callback_query(F.data == "user_enter_promo")
async def cb_user_enter_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserPromoStates.ожидание_кода)
    await c.message.answer("Введите промокод:")
    await c.answer()


@router.message(UserPromoStates.ожидание_кода)
async def user_enter_promo_code(m: Message, state: FSMContext, db: DB):
    await handle_promo_application(m, db, m.text or "", state)


@router.message(Command("use"))
async def cmd_use_promo(m: Message, state: FSMContext, db: DB):
    args = (m.text or "").split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Формат: /use <промокод>. Или воспользуйтесь кнопкой 'Ввести промокод'.")
        return
    await handle_promo_application(m, db, args[1], state)
