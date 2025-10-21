from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
COUPON_KIND_TRIAL = "trial"

MD_V2_SPECIAL = set("_*[]()~`>#+-=|{}.!\\")

CANCEL_REPLY = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
)

START_TEXT = "🎟️ Доступ в канал\nВыберите действие ниже.\n\nℹ️ Пробный период доступен по промокоду."


class BindChat(StatesGroup):
    """Состояния для привязки чата по username."""

    wait_username = State()


class Admin(StatesGroup):
    """Состояния администратора для ввода параметров."""

    WaitPrices = State()
    WaitTrialDays = State()
    WaitCustomCode = State()


class User(StatesGroup):
    """Состояния пользователя."""

    WaitPromoCode = State()


def escape_md(text: str) -> str:
    """Экранировать текст для MarkdownV2."""

    return "".join(f"\\{char}" if char in MD_V2_SPECIAL else char for char in text)


def format_expiry(ts: int) -> str:
    """Отформатировать таймстамп в строку UTC."""

    return datetime.utcfromtimestamp(ts).strftime("%d.%m.%Y %H:%M UTC")


def is_super_admin(user_id: int) -> bool:
    """Проверить, является ли пользователь суперадмином."""

    return user_id in config.SUPER_ADMIN_IDS


def inline_emoji(flag: bool) -> str:
    """Вернуть эмодзи статуса."""

    return "✅" if flag else "❌"


def is_cancel(text: str | None) -> bool:
    """Понять, хочет ли пользователь отменить ввод."""

    if text is None:
        return False
    return text.strip().lower() == "отмена"


async def has_trial_coupon(db: DB, user_id: int) -> bool:
    """Проверить, применял ли пользователь пробный промокод."""

    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM coupons WHERE kind=? AND used_by=? LIMIT 1",
            (COUPON_KIND_TRIAL, user_id),
        )
        return await cur.fetchone() is not None


def build_user_menu(auto_on: bool, is_admin: bool) -> InlineKeyboardMarkup:
    """Собрать пользовательскую inline-клавиатуру."""

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Купить 1 мес", callback_data="buy:months:1")
    builder.button(text="💳 Купить 2 мес", callback_data="buy:months:2")
    builder.button(text="💳 Купить 3 мес", callback_data="buy:months:3")
    builder.button(
        text=f"🔁 Автопродление: {inline_emoji(auto_on)}",
        callback_data="ar:toggle",
    )
    builder.button(text="🔗 Получить ссылку", callback_data="invite:once")
    builder.button(text="🏷️ Ввести промокод", callback_data="promo:enter")
    if is_admin:
        builder.button(text="🛠️ Админ-панель", callback_data="admin:open")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


async def get_user_menu(db: DB, user_id: int) -> InlineKeyboardMarkup:
    """Получить клавиатуру пользователя с актуальными данными."""

    user = await db.get_user(user_id)
    auto_flag = bool(user and user["auto_renew"])
    return build_user_menu(auto_flag, is_super_admin(user_id))


async def refresh_user_menu(message: Message, db: DB, user_id: int) -> None:
    """Перерисовать клавиатуру пользователя, не меняя текст."""

    markup = await get_user_menu(db, user_id)
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(
            escape_md("Меню обновлено."),
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )


async def build_admin_panel(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    """Сформировать текст и клавиатуру админ-панели."""

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
        parts = [f"{months} мес — {price}₽" for months, price in sorted(prices.items())]
        price_text = ", ".join(parts)
    else:
        price_text = "не настроен"
    lines = [
        "📊 Текущие настройки:",
        chat_line,
        f"• Пробный период: {trial_days} дн.",
        f"• Автопродление по умолчанию: {inline_emoji(auto_default)}",
        f"• Прайс-лист: {price_text}",
    ]
    text = "\n".join(escape_md(line) for line in lines)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Привязать чат", callback_data="admin:bind_chat")
    builder.button(text="💰 Редактировать цены", callback_data="admin:prices")
    builder.button(text="🗓️ Пробный период", callback_data="admin:trial_days")
    builder.button(
        text=f"🔁 Автопродление по умолчанию: {inline_emoji(auto_default)}",
        callback_data="admin:auto_default",
    )
    builder.button(text="🏷️ Создать пробный промокод", callback_data="admin:create_coupon")
    builder.adjust(2, 2, 1)

    return text, builder.as_markup()


async def render_admin_panel(message: Message, db: DB) -> None:
    """Отобразить или обновить админ-панель в заданном сообщении."""

    text, markup = await build_admin_panel(db)
    try:
        await message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await message.answer(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )


async def refresh_admin_panel_by_state(bot: Bot, state: FSMContext, db: DB) -> None:
    """Перерисовать админ-панель по сохранённым идентификаторам."""

    data = await state.get_data()
    chat_id = data.get("panel_chat_id")
    message_id = data.get("panel_message_id")
    if not chat_id or not message_id:
        return
    text, markup = await build_admin_panel(db)
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )


async def apply_trial_coupon(db: DB, user_id: int) -> tuple[bool, str]:
    """Применить пробный промокод к пользователю."""

    trial_days = await db.get_trial_days_global(DEFAULT_TRIAL_DAYS)
    if trial_days <= 0:
        return False, "❌ Пробный период не настроен. Сообщите администратору."
    trial_seconds = int(timedelta(days=trial_days).total_seconds())
    now_ts = int(datetime.utcnow().timestamp())
    user = await db.get_user(user_id)
    if user is None:
        auto_default = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
        await db.upsert_user(user_id, now_ts, trial_days, auto_default, False)
        await db.set_paid_only(user_id, False)
        expires_at = now_ts + trial_seconds
        return True, f"✅ Пробный доступ активирован до {format_expiry(expires_at)}."
    expires_at = user["expires_at"] or 0
    if expires_at <= now_ts:
        new_exp = now_ts + trial_seconds
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute(
                "UPDATE users SET expires_at=?, paid_only=0 WHERE user_id=?",
                (new_exp, user_id),
            )
            await conn.commit()
        return True, f"✅ Пробный доступ активирован до {format_expiry(new_exp)}."
    await db.set_paid_only(user_id, False)
    return True, f"✅ Промокод принят. Подписка активна до {format_expiry(expires_at)}."


async def redeem_promo_code(
    message: Message,
    db: DB,
    code: str,
    *,
    remove_keyboard: bool,
) -> None:
    """Попытаться применить промокод и сообщить результат."""

    normalized = (code or "").strip()
    if not normalized:
        text = escape_md("❌ Промокод не должен быть пустым.")
        reply_markup = ReplyKeyboardRemove() if remove_keyboard else None
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    ok, info, kind = await db.use_coupon(normalized, message.from_user.id)
    if not ok:
        reply_markup = ReplyKeyboardRemove() if remove_keyboard else None
        await message.answer(
            escape_md(f"❌ {info}"),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    if kind != COUPON_KIND_TRIAL:
        reply_markup = ReplyKeyboardRemove() if remove_keyboard else None
        await message.answer(
            escape_md("❌ Этот промокод пока не поддерживается."),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    success, result_text = await apply_trial_coupon(db, message.from_user.id)
    reply_markup = ReplyKeyboardRemove() if remove_keyboard else None
    await message.answer(
        escape_md(result_text),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    if not success:
        return
    menu = await get_user_menu(db, message.from_user.id)
    await message.answer(
        escape_md("Меню обновлено."),
        reply_markup=menu,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


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
    menu = await get_user_menu(db, user_id)
    await message.answer(
        escape_md(START_TEXT),
        reply_markup=menu,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("buy:months:"))
async def handle_buy(callback: CallbackQuery, db: DB) -> None:
    """Обработка покупки подписки."""

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
        await callback.message.answer(
            escape_md(payment_text),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await refresh_user_menu(callback.message, db, user_id)
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
        await refresh_user_menu(callback.message, db, user_id)
    await callback.answer("Статус обновлён.")


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
        text = "\n".join(
            [
                escape_md("Ваша ссылка (действует 24 часа):"),
                escape_md(link.invite_link),
            ]
        )
        await callback.message.answer(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "promo:enter")
async def handle_promo_enter(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к вводу промокода пользователем."""

    await state.set_state(User.WaitPromoCode)
    if callback.message:
        await callback.message.answer(
            escape_md("Введите промокод:"),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(User.WaitPromoCode)
async def handle_promo_input(message: Message, state: FSMContext, db: DB) -> None:
    """Обработать ввод промокода пользователем."""

    text = message.text or ""
    if is_cancel(text):
        await state.clear()
        await message.answer(
            escape_md("Ввод промокода отменён."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await message.answer(
            escape_md("Меню обновлено."),
            reply_markup=await get_user_menu(db, message.from_user.id),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await redeem_promo_code(message, db, text, remove_keyboard=True)
    await state.clear()


@router.message(Command("use"))
async def cmd_use(message: Message, state: FSMContext, db: DB) -> None:
    """Команда /use для применения промокода."""

    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            escape_md("❌ Укажите промокод после команды, например: /use CODE."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await redeem_promo_code(message, db, parts[1], remove_keyboard=False)


@router.callback_query(F.data == "admin:open")
async def open_admin_panel(callback: CallbackQuery, db: DB) -> None:
    """Открыть админ-панель."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_admin_panel(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "admin:bind_chat")
async def admin_bind_chat(callback: CallbackQuery, state: FSMContext) -> None:
    """Запросить у администратора username целевого чата."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(BindChat.wait_username)
    if callback.message:
        await state.update_data(
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            escape_md("Пришлите @username канала или группы."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
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
        await message.answer(
            escape_md("Привязка отменена."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    if not text.startswith("@") or len(text) < 2:
        await message.answer(
            escape_md("Нужен username в формате @example."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    try:
        chat = await bot.get_chat(text)
    except TelegramBadRequest:
        await message.answer(
            escape_md("Не удалось получить чат. Проверьте username и права бота."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    except Exception:
        await message.answer(
            escape_md("Произошла ошибка при получении чата. Попробуйте позже."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    stored_username = getattr(chat, "username", None)
    if stored_username:
        stored_value = f"@{stored_username}"
    else:
        stored_value = text
    await db.set_target_chat_username(stored_value)
    await db.set_target_chat_id(chat.id)
    await message.answer(
        escape_md(f"✅ Чат {stored_value} (id {chat.id}) привязан."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await refresh_admin_panel_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data == "admin:prices")
async def admin_prices(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к редактированию цен."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(Admin.WaitPrices)
    if callback.message:
        await state.update_data(
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            escape_md("Пришлите цены в формате 1:399,2:699."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(Admin.WaitPrices)
async def admin_set_prices(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Обработать ввод цен."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await message.answer(
            escape_md("Редактирование отменено."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    cleaned = text.replace(" ", "")
    entries = [item for item in cleaned.split(",") if item]
    prices: dict[int, int] = {}
    for entry in entries:
        if ":" not in entry:
            await message.answer(
                escape_md("Используйте формат месяцы:цена."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return
        left, right = entry.split(":", 1)
        try:
            months = int(left)
            price = int(right)
        except ValueError:
            await message.answer(
                escape_md("Нужно указать целые числа."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return
        if months <= 0 or price <= 0:
            await message.answer(
                escape_md("Числа должны быть положительными."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return
        prices[months] = price
    if not prices:
        await message.answer(
            escape_md("Не удалось распознать ни одной записи."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await db.set_prices(prices)
    await message.answer(
        escape_md("✅ Цены обновлены."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await refresh_admin_panel_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data == "admin:trial_days")
async def admin_trial_days(callback: CallbackQuery, state: FSMContext) -> None:
    """Запросить количество пробных дней."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(Admin.WaitTrialDays)
    if callback.message:
        await state.update_data(
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            escape_md("Пришлите количество дней пробного периода."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(Admin.WaitTrialDays)
async def admin_set_trial_days(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Сохранить новый пробный период."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await message.answer(
            escape_md("Изменение отменено."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    if not text.isdigit():
        await message.answer(
            escape_md("Нужно указать положительное целое число."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    days = int(text)
    if days <= 0:
        await message.answer(
            escape_md("Количество дней должно быть больше нуля."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await db.set_trial_days_global(days)
    await message.answer(
        escape_md(f"✅ Пробный период установлен: {days} дн."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await refresh_admin_panel_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data == "admin:auto_default")
async def admin_toggle_auto_default(callback: CallbackQuery, db: DB) -> None:
    """Переключить автопродление по умолчанию."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    current = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
    await db.set_auto_renew_default(not current)
    if callback.message:
        await render_admin_panel(callback.message, db)
    await callback.answer("Настройки обновлены.")


@router.callback_query(F.data == "admin:create_coupon")
async def admin_create_coupon(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к созданию пробного промокода."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(Admin.WaitCustomCode)
    if callback.message:
        await state.update_data(
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            escape_md("Пришлите промокод (латиница/цифры/дефис, 4–32 символа)."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(Admin.WaitCustomCode)
async def admin_save_custom_code(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Создать пробный промокод из присланного текста."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await message.answer(
            escape_md("Создание промокода отменено."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    ok, info = await db.create_coupon(text, COUPON_KIND_TRIAL)
    if not ok:
        await message.answer(
            escape_md(f"❌ {info}"),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await message.answer(
        escape_md(f"✅ Пробный промокод сохранён: {info}"),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await refresh_admin_panel_by_state(bot, state, db)
    await state.clear()

