from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime, timedelta
from config import config
from db import DB
from payments import process_payment

router = Router(name="core")


def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS


class PromoCodeStates(StatesGroup):
    waiting_for_code = State()


def build_main_keyboard(prices: dict[int, int], auto_renew_enabled: bool) -> InlineKeyboardMarkup:
    """Построение основной инлайн-клавиатуры управления подпиской."""

    buttons: list[list[InlineKeyboardButton]] = []
    for months, price in sorted(prices.items()):
        text = f"Купить {months} мес ({price}₽)"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"buy:{months}")])

    auto_text = "Автопродление: вкл" if auto_renew_enabled else "Автопродление: выкл"
    buttons.append([InlineKeyboardButton(text=auto_text, callback_data="toggle_auto")])
    buttons.append([InlineKeyboardButton(text="Получить ссылку", callback_data="get_link")])
    buttons.append([InlineKeyboardButton(text="Ввести промокод", callback_data="promo")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def refresh_main_keyboard(message: Message, db: DB):
    """Обновление клавиатуры в закреплённом сообщении после действий пользователя."""

    prices = await db.get_price_map(config.PRICES)
    row = await db.get_user(message.chat.id)
    keyboard = build_main_keyboard(prices, bool(row["auto_renew"]) if row else False)
    try:
        await message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        # Игнорируем, если сообщение уже не доступно или не изменилось
        pass


async def handle_promocode(message: Message, code: str):
    """Заглушка обработки промокода."""

    code = (code or "").strip()
    if not code:
        await message.answer("Промокод не может быть пустым. Попробуйте снова.")
        return
    await message.answer(f"Промокод {code} сохранён. Ожидайте проверки администратора.")


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, db: DB):
    await state.clear()
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days_global(config.TRIAL_DAYS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    paid_only_default = await db.get_paid_only_default(False)
    paid_only = paid_only_default or (m.from_user.id in config.PAID_ONLY_IDS)
    bypass = m.from_user.id in config.ADMIN_BYPASS_IDS

    await db.upsert_user(m.from_user.id, now_ts, trial_days, auto_renew_default, paid_only, bypass)
    user_row = await db.get_user(m.from_user.id)
    prices = await db.get_price_map(config.PRICES)
    keyboard = build_main_keyboard(prices, bool(user_row["auto_renew"]) if user_row else auto_renew_default)

    auto_enabled = bool(user_row and user_row["auto_renew"]) or (not user_row and auto_renew_default)
    warning = (
        "⚠️ Автопродление подписки сейчас включено. Используйте кнопку ниже, чтобы отключить его."
        if auto_enabled
        else "ℹ️ Автопродление подписки сейчас выключено. Вы можете включить его кнопкой ниже."
    )

    await m.answer(
        "Добро пожаловать! Ниже доступно управление подпиской.\n" + warning,
        reply_markup=keyboard,
    )


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
        link = await bot.create_chat_invite_link(
            config.TARGET_CHAT_ID,
            member_limit=1,
            expire_date=expire_ts,
        )
        await m.answer("Ваша одноразовая ссылка (24ч):\n" + link.invite_link)
    except Exception:
        await m.answer("Не удалось создать ссылку. Напишите админу.")


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, db: DB):
    months_part = callback.data.split(":", 1)[1]
    if not months_part.isdigit():
        await callback.answer("Некорректный тариф", show_alert=True)
        return

    months = int(months_part)
    prices = await db.get_price_map(config.PRICES)
    ok, msg = await process_payment(callback.from_user.id, months, prices)
    if not ok:
        await callback.answer("Оплата не прошла", show_alert=True)
        if callback.message:
            await callback.message.answer("Оплата не прошла: " + msg)
        return

    await db.extend_subscription(callback.from_user.id, months)
    await callback.answer("Оплата успешна")
    if callback.message:
        await callback.message.answer(msg + "\nПодписка продлена.")
        await refresh_main_keyboard(callback.message, db)


@router.callback_query(F.data == "toggle_auto")
async def cb_toggle_auto(callback: CallbackQuery, db: DB):
    row = await db.get_user(callback.from_user.id)
    if not row:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return

    new_flag = not bool(row["auto_renew"])
    await db.set_auto_renew(callback.from_user.id, new_flag)
    await callback.answer("Автопродление обновлено")
    if callback.message:
        await refresh_main_keyboard(callback.message, db)


@router.callback_query(F.data == "get_link")
async def cb_get_link(callback: CallbackQuery, bot: Bot, db: DB):
    row = await db.get_user(callback.from_user.id)
    if not row:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return

    if row["expires_at"] < int(datetime.utcnow().timestamp()):
        await callback.answer("Подписка неактивна", show_alert=True)
        if callback.message:
            await callback.message.answer("Подписка не активна. Оплатите, чтобы вернуться.")
        return

    try:
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(
            config.TARGET_CHAT_ID,
            member_limit=1,
            expire_date=expire_ts,
        )
        await callback.answer("Ссылка отправлена")
        if callback.message:
            await callback.message.answer("Ваша одноразовая ссылка (24ч):\n" + link.invite_link)
    except Exception:
        await callback.answer("Не удалось выдать ссылку", show_alert=True)
        if callback.message:
            await callback.message.answer("Не удалось создать ссылку. Напишите админу.")


@router.callback_query(F.data == "promo")
async def cb_promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCodeStates.waiting_for_code)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введите промокод сообщением. Для отмены нажмите /start.")


@router.message(PromoCodeStates.waiting_for_code)
async def promo_code_input(m: Message, state: FSMContext):
    await handle_promocode(m, m.text or "")
    await state.clear()


@router.message(Command("use"))
async def cmd_use(m: Message, state: FSMContext):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: /use <промокод>")
        return

    await handle_promocode(m, parts[1])
    await state.clear()


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
        "/invite <user_id> [hours]\n"
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
    flag = args[2] == "on"
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
    flag = args[2] == "on"
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
    flag = args[2] == "on"
    await db.set_bypass(uid, flag)
    await m.answer(f"OK. user_id={uid} bypass={flag}")


@router.message(Command("price_list"))
async def cmd_price_list(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    price_map = await db.get_price_map(config.PRICES)
    lines = [f"{months} мес: {price}₽" for months, price in sorted(price_map.items())]
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
        link = await bot.create_chat_invite_link(
            config.TARGET_CHAT_ID,
            member_limit=1,
            expire_date=expire_ts,
        )
        await bot.send_message(uid, f"Ваша персональная ссылка (действует {hours}ч):\n{link.invite_link}")
        await m.answer("Отправил пользователю.")
    except Exception:
        await m.answer("Не удалось создать/отправить ссылку.")
