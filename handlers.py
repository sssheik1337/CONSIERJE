from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from db import DB
from payments import process_payment

router = Router()

DEFAULT_TRIAL_DAYS = 3
DEFAULT_AUTO_RENEW = True
TRIAL_CODE_KIND = "trial"

CANCEL_REPLY = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
)


class BindChat(StatesGroup):
    """Состояния для привязки чата по username."""

    wait_username = State()


class Admin(StatesGroup):
    """Состояния администратора для ввода параметров."""

    WaitPrices = State()
    WaitTrialDays = State()


class User(StatesGroup):
    """Состояния пользователя."""

    WaitPromoCode = State()


def is_super_admin(user_id: int) -> bool:
    """Проверить, является ли пользователь суперадмином."""

    return user_id in config.SUPER_ADMIN_IDS


def inline_emoji(flag: bool) -> str:
    """Вернуть эмодзи по булеву флагу."""

    return "✅" if flag else "❌"


def is_cancel(text: str | None) -> bool:
    """Понять, хочет ли пользователь отменить ввод."""

    if text is None:
        return False
    return text.strip().lower() == "отмена"


async def has_trial_coupon(db: DB, user_id: int) -> bool:
    """Проверить, применял ли пользователь trial-промокод."""

    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM coupons WHERE kind=? AND used_by=? LIMIT 1",
            (TRIAL_CODE_KIND, user_id),
        )
        return await cur.fetchone() is not None


async def build_user_keyboard(db: DB, user_id: int) -> InlineKeyboardMarkup:
    """Собрать пользовательскую inline-клавиатуру."""

    user = await db.get_user(user_id)
    auto_flag = bool(user and user["auto_renew"])
    builder = InlineKeyboardBuilder()
    for months in (1, 2, 3):
        builder.button(
            text=f"Купить {months} мес",
            callback_data=f"buy:months:{months}",
        )
    builder.button(
        text=f"Автопродление: {inline_emoji(auto_flag)}",
        callback_data="ar:toggle",
    )
    builder.button(text="Получить ссылку", callback_data="invite:once")
    builder.button(text="Ввести промокод", callback_data="promo:enter")
    if is_super_admin(user_id):
        builder.button(text="Админ-панель", callback_data="admin:menu")
        builder.adjust(3, 3, 1)
    else:
        builder.adjust(3, 3)
    return builder.as_markup()


async def build_admin_keyboard(db: DB) -> InlineKeyboardMarkup:
    """Собрать клавиатуру админ-панели."""

    auto_default = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
    builder = InlineKeyboardBuilder()
    builder.button(text="Привязать чат", callback_data="admin:bind")
    builder.button(text="Показать настройки", callback_data="admin:show")
    builder.button(text="Редактировать цены", callback_data="admin:prices")
    builder.button(text="Установить пробный период", callback_data="admin:trialdays")
    builder.button(
        text=f"Автопродление по умолчанию: {inline_emoji(auto_default)}",
        callback_data="admin:ar_default",
    )
    builder.button(text="Сгенерировать trial-коды", callback_data="admin:gen_trial")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


async def build_admin_summary(db: DB) -> str:
    """Сформировать сводку настроек для администратора."""

    chat_username = await db.get_target_chat_username()
    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        chat_line = "• Чат: не привязан"
    else:
        if chat_username:
            chat_line = f"• Чат: {chat_username} (id {chat_id})"
        else:
            chat_line = f"• Чат: id {chat_id}"
    trial_days = await db.get_trial_days_global(DEFAULT_TRIAL_DAYS)
    auto_default = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
    prices = await db.get_prices({})
    if prices:
        price_lines = [
            f"  - {months} мес: {price}₽" for months, price in sorted(prices.items())
        ]
        price_block = "Прайс-лист:\n" + "\n".join(price_lines)
    else:
        price_block = "Прайс-лист не настроен"
    lines = [
        "📊 Текущие настройки:",
        chat_line,
        f"• Пробный период: {trial_days} дн.",
        f"• Автопродление по умолчанию: {inline_emoji(auto_default)}",
        price_block,
    ]
    return "\n".join(lines)


async def update_user_menu(message: Message, db: DB, user_id: int) -> None:
    """Обновить inline-меню пользователя."""

    markup = await build_user_keyboard(db, user_id)
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        await message.answer("Меню обновлено.", reply_markup=markup)


async def apply_trial_to_user(db: DB, user_id: int, trial_days: int) -> tuple[str, bool]:
    """Применить trial-промокод к пользователю."""

    user = await db.get_user(user_id)
    if user is None:
        return (
            "Промокод сохранён. Выполните /start, чтобы активировать пробный доступ.",
            False,
        )
    now_ts = int(datetime.utcnow().timestamp())
    expires_at = user["expires_at"] or 0
    trial_seconds = max(trial_days, 0) * 24 * 3600
    if trial_seconds == 0:
        return (
            "Пробный период не настроен. Обратитесь к администратору.",
            True,
        )
    if expires_at <= now_ts:
        new_exp = now_ts + trial_seconds
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute(
                "UPDATE users SET expires_at=?, paid_only=0 WHERE user_id=?",
                (new_exp, user_id),
            )
            await conn.commit()
        readable = datetime.utcfromtimestamp(new_exp).strftime("%d.%m.%Y %H:%M UTC")
        return (f"Пробный доступ активирован до {readable}.", True)
    await db.set_paid_only(user_id, False)
    readable = datetime.utcfromtimestamp(expires_at).strftime("%d.%m.%Y %H:%M UTC")
    return (
        f"Промокод принят. Текущая подписка активна до {readable}.",
        True,
    )


async def redeem_promo_code(
    message: Message,
    db: DB,
    code: str,
    *,
    remove_keyboard: bool,
) -> None:
    """Общая логика применения промокода."""

    normalized = (code or "").strip()
    if not normalized:
        text = "Промокод не должен быть пустым."
        if remove_keyboard:
            await message.answer(text, reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer(text)
        return
    ok, info, kind = await db.use_coupon(normalized, message.from_user.id)
    if not ok:
        if remove_keyboard:
            await message.answer(info, reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer(info)
        return
    if kind != TRIAL_CODE_KIND:
        text = "Этот промокод пока не поддерживается."
        if remove_keyboard:
            await message.answer(text, reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer(text)
        return
    trial_days = await db.get_trial_days_global(DEFAULT_TRIAL_DAYS)
    result_text, has_user = await apply_trial_to_user(db, message.from_user.id, trial_days)
    if not has_user:
        result_text = (
            f"{result_text}\n\nПосле команды /start бот оформит пробный доступ."
        )
    if remove_keyboard:
        await message.answer(result_text, reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer(result_text)
    await message.answer(
        "Главное меню:",
        reply_markup=await build_user_keyboard(db, message.from_user.id),
    )


async def send_admin_panel(message: Message, db: DB) -> None:
    """Вывести админ-панель в чат."""

    summary = await build_admin_summary(db)
    markup = await build_admin_keyboard(db)
    await message.answer(summary, reply_markup=markup)


async def refresh_admin_panel(message: Message, db: DB) -> None:
    """Обновить уже отправленную админ-панель."""

    summary = await build_admin_summary(db)
    markup = await build_admin_keyboard(db)
    try:
        await message.edit_text(summary, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(summary, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: DB) -> None:
    """Обработать /start для пользователя."""

    await state.clear()
    user_id = message.from_user.id
    now_ts = int(datetime.utcnow().timestamp())
    auto_default = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
    trial_days = await db.get_trial_days_global(DEFAULT_TRIAL_DAYS)
    paid_only = True
    if await has_trial_coupon(db, user_id):
        paid_only = False
    await db.upsert_user(user_id, now_ts, trial_days, auto_default, paid_only)
    if not paid_only:
        await db.set_paid_only(user_id, False)
    warning = "Автопродление включено по умолчанию. Можно выключить: тумблер ниже."
    await message.answer(
        warning,
        reply_markup=await build_user_keyboard(db, user_id),
    )


@router.callback_query(F.data.startswith("buy:months:"))
async def handle_buy(callback: CallbackQuery, db: DB) -> None:
    """Обработка покупки подписки по нажатию кнопки."""

    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Не удалось определить срок подписки.", show_alert=True)
        return
    prices = await db.get_prices({})
    price = prices.get(months)
    if price is None:
        await callback.answer("Цена не настроена. Обратитесь к администратору.", show_alert=True)
        return
    success, payment_text = await process_payment(user_id, months, prices)
    if not success:
        await callback.answer(payment_text, show_alert=True)
        return
    await db.extend_subscription(user_id, months)
    await db.set_paid_only(user_id, False)
    if callback.message:
        await callback.message.answer(payment_text)
        await update_user_menu(callback.message, db, user_id)
    await callback.answer("Подписка продлена.")


@router.callback_query(F.data == "ar:toggle")
async def handle_toggle_autorenew(callback: CallbackQuery, db: DB) -> None:
    """Переключить автопродление пользователя."""

    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if user is None:
        await callback.answer("Сначала выполните /start.", show_alert=True)
        return
    current = bool(user["auto_renew"])
    new_flag = not current
    await db.set_auto_renew(user_id, new_flag)
    if callback.message:
        await update_user_menu(callback.message, db, user_id)
    status = "включено" if new_flag else "выключено"
    await callback.answer(f"Автопродление {status}.")


@router.callback_query(F.data == "invite:once")
async def handle_invite(callback: CallbackQuery, bot: Bot, db: DB) -> None:
    """Выдать одноразовую ссылку в целевой чат."""

    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None:
        await callback.answer("Чат не привязан.", show_alert=True)
        return
    expire_ts = int((datetime.utcnow() + timedelta(days=1)).timestamp())
    try:
        link = await bot.create_chat_invite_link(
            target_chat_id,
            member_limit=1,
            expire_date=expire_ts,
        )
    except Exception:
        await callback.answer("Не удалось создать ссылку. Сообщите администратору.", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(
            "Ваша ссылка (действует 24 часа):\n" f"{link.invite_link}",
        )
    await callback.answer()


@router.callback_query(F.data == "promo:enter")
async def handle_promo_enter(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к вводу промокода."""

    await state.set_state(User.WaitPromoCode)
    if callback.message:
        await callback.message.answer(
            "Введите промокод:",
            reply_markup=CANCEL_REPLY,
        )
    await callback.answer()


@router.message(User.WaitPromoCode)
async def handle_promo_input(message: Message, state: FSMContext, db: DB) -> None:
    """Обработать ввод промокода пользователем."""

    text = message.text or ""
    if is_cancel(text):
        await state.clear()
        await message.answer(
            "Ввод промокода отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "Главное меню:",
            reply_markup=await build_user_keyboard(db, message.from_user.id),
        )
        return
    await redeem_promo_code(message, db, text, remove_keyboard=True)
    await state.clear()


@router.message(Command("use"))
async def cmd_use(message: Message, state: FSMContext, db: DB) -> None:
    """Команда /use для применения промокода."""

    await state.clear()
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите промокод после команды, например: /use ABC123.")
        return
    await redeem_promo_code(message, db, parts[1], remove_keyboard=False)


@router.callback_query(F.data == "admin:menu")
async def open_admin_menu(callback: CallbackQuery, db: DB) -> None:
    """Открыть админ-панель по кнопке."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await send_admin_panel(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "admin:bind")
async def admin_bind(callback: CallbackQuery, state: FSMContext) -> None:
    """Запросить у админа @username целевого чата."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(BindChat.wait_username)
    if callback.message:
        await callback.message.answer(
            "Пришлите @username канала или группы для привязки.",
            reply_markup=CANCEL_REPLY,
        )
    await callback.answer()


@router.message(BindChat.wait_username)
async def process_bind_username(
    message: Message,
    bot: Bot,
    db: DB,
    state: FSMContext,
) -> None:
    """Привязать чат по присланному username."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await state.clear()
        await message.answer(
            "Привязка отменена.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if not text.startswith("@") or len(text) < 2:
        await message.answer("Нужен username в формате @example.")
        return
    try:
        chat = await bot.get_chat(text)
    except TelegramBadRequest:
        await message.answer("Не удалось получить чат. Проверьте username и права бота.")
        return
    except Exception:
        await message.answer("Произошла ошибка при получении чата. Попробуйте позже.")
        return
    stored_username = f"@{chat.username}" if getattr(chat, "username", None) else text
    await db.set_target_chat_username(stored_username)
    await db.set_target_chat_id(chat.id)
    await state.clear()
    await message.answer(
        f"Чат {stored_username} (id {chat.id}) привязан.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_admin_panel(message, db)


@router.callback_query(F.data == "admin:show")
async def admin_show(callback: CallbackQuery, db: DB) -> None:
    """Показать текущие настройки."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    text = await build_admin_summary(db)
    if callback.message:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "admin:prices")
async def admin_prices(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к редактированию цен."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(Admin.WaitPrices)
    if callback.message:
        await callback.message.answer(
            "Пришлите цены в формате '1:399,2:699'.",
            reply_markup=CANCEL_REPLY,
        )
    await callback.answer()


@router.message(Admin.WaitPrices)
async def admin_set_prices(message: Message, state: FSMContext, db: DB) -> None:
    """Обработать ввод цен."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await state.clear()
        await message.answer("Редактирование отменено.", reply_markup=ReplyKeyboardRemove())
        return
    cleaned = text.replace(" ", "")
    entries = [item for item in cleaned.split(",") if item]
    prices: dict[int, int] = {}
    for entry in entries:
        if ":" not in entry:
            await message.answer("Используйте формат 'месяцы:цена'.")
            return
        left, right = entry.split(":", 1)
        try:
            months = int(left)
            price = int(right)
        except ValueError:
            await message.answer("Нужно указать целые числа через двоеточие.")
            return
        if months <= 0 or price <= 0:
            await message.answer("Числа должны быть положительными.")
            return
        prices[months] = price
    if not prices:
        await message.answer("Не удалось распознать ни одной записи.")
        return
    await db.set_prices(prices)
    await state.clear()
    await message.answer("Цены обновлены.", reply_markup=ReplyKeyboardRemove())
    await send_admin_panel(message, db)


@router.callback_query(F.data == "admin:trialdays")
async def admin_trialdays(callback: CallbackQuery, state: FSMContext) -> None:
    """Запросить количество пробных дней."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(Admin.WaitTrialDays)
    if callback.message:
        await callback.message.answer(
            "Пришлите количество дней пробного периода (целое число).",
            reply_markup=CANCEL_REPLY,
        )
    await callback.answer()


@router.message(Admin.WaitTrialDays)
async def admin_set_trial_days(message: Message, state: FSMContext, db: DB) -> None:
    """Сохранить новый пробный период."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await state.clear()
        await message.answer("Изменение отменено.", reply_markup=ReplyKeyboardRemove())
        return
    if not text.isdigit():
        await message.answer("Нужно указать положительное целое число.")
        return
    days = int(text)
    if days <= 0:
        await message.answer("Количество дней должно быть больше нуля.")
        return
    await db.set_trial_days_global(days)
    await state.clear()
    await message.answer(
        f"Пробный период установлен: {days} дн.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_admin_panel(message, db)


@router.callback_query(F.data == "admin:ar_default")
async def admin_toggle_auto_default(callback: CallbackQuery, db: DB) -> None:
    """Переключить автопродление по умолчанию."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    current = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
    new_flag = not current
    await db.set_auto_renew_default(new_flag)
    if callback.message:
        await refresh_admin_panel(callback.message, db)
    await callback.answer(f"Теперь по умолчанию: {inline_emoji(new_flag)}")


@router.callback_query(F.data == "admin:gen_trial")
async def admin_generate_trial(callback: CallbackQuery, db: DB) -> None:
    """Сгенерировать набор trial-кодов."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    codes = await db.gen_coupons(TRIAL_CODE_KIND, 5)
    if callback.message:
        if codes:
            lines = ["Созданы trial-коды:"] + codes
            await callback.message.answer("\n".join(lines))
        else:
            await callback.message.answer("Не удалось создать промокоды.")
    await callback.answer()
