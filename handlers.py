from __future__ import annotations

from datetime import datetime, timedelta

import logging

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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

from config import config, get_docs_map
from db import DB
from payments import check_payment_status, create_payment

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
    """Состояния для привязки чата по идентификатору."""

    wait_username = State()


class Admin(StatesGroup):
    """Состояния администратора для ввода параметров."""

    WaitTrialDays = State()
    WaitCustomCode = State()


class AdminPrice(StatesGroup):
    """Состояния администратора для управления тарифами."""

    AddMonths = State()
    AddPrice = State()
    EditMonths = State()
    EditPrice = State()


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


async def make_one_time_invite(
    bot: Bot,
    db: DB,
    hours: int = 24,
    member_limit: int = 1,
) -> tuple[bool, str, str]:
    """Создать одноразовую ссылку или вернуть причину ошибки с подсказкой."""

    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        return (
            False,
            "Чат не привязан. Откройте Админ-панель → 🔗 Привязать чат.",
            "",
        )

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
        chat = await bot.get_chat(chat_id)
    except TelegramForbiddenError:
        return (
            False,
            "Доступ запрещён. Бот не админ или сняты права.",
            "Назначьте бота админом и дайте «Пригласительные ссылки».",
        )
    except TelegramBadRequest as err:
        err_text = str(err)
        lower = err_text.lower()
        if "chat not found" in lower or "chat_not_found" in lower:
            return (
                False,
                "Чат недоступен боту.",
                "Привяжите чат заново.",
            )
        logging.exception("Ошибка при получении сведений о боте", exc_info=err)
        return (
            False,
            "Не удалось проверить права.",
            err_text,
        )
    except Exception as err:
        logging.exception("Не удалось получить сведения о боте", exc_info=err)
        return (
            False,
            "Не удалось проверить права.",
            "См. логи.",
        )

    status_raw = getattr(member, "status", "")
    status_value = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
    if status_value not in {"administrator", "creator"}:
        return (
            False,
            "Бот не админ в целевом чате.",
            "Выдайте боту права администратора.",
        )

    if chat.type == "supergroup":
        can_invite_attr = getattr(member, "can_invite_users", None)
        if can_invite_attr is False:
            return (
                False,
                "Нет права «Пригласительные ссылки».",
                "Включите его в правах бота.",
            )

    expire_ts = int((datetime.utcnow() + timedelta(hours=hours)).timestamp())
    try:
        link = await bot.create_chat_invite_link(
            chat_id,
            member_limit=int(member_limit),
            expire_date=expire_ts,
            creates_join_request=False,
        )
        logging.info(
            "Создана одноразовая ссылка: chat_id=%s limit=%s expire=%s join_request=%s link=%s",
            chat_id,
            getattr(link, "member_limit", None),
            getattr(link, "expire_date", None),
            getattr(link, "creates_join_request", None),
            link.invite_link,
        )
        return True, link.invite_link, ""
    except TelegramForbiddenError:
        return (
            False,
            "Доступ запрещён. Бот не админ или сняты права.",
            "Назначьте бота админом и дайте «Пригласительные ссылки».",
        )
    except TelegramBadRequest as err:
        err_text = str(err)
        lower = err_text.lower()
        if "chat_admin_required" in lower or "not enough rights" in lower:
            try:
                fallback = await bot.export_chat_invite_link(chat_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                return (
                    False,
                    "Недостаточно прав для создания одноразовой ссылки.",
                    "Дайте право «Пригласительные ссылки».",
                )
            except Exception as export_err:
                logging.exception("Ошибка при получении постоянной ссылки", exc_info=export_err)
                return (
                    False,
                    "Недостаточно прав для создания одноразовой ссылки.",
                    "Дайте право «Пригласительные ссылки».",
                )
            return (
                False,
                "⚠️ Можно выдать постоянную ссылку (неодноразовая). Разрешите «Пригласительные ссылки», чтобы выдавать одноразовые.",
                fallback,
            )
        if "user_not_participant" in lower or "chat not found" in lower or "chat_not_found" in lower:
            return (
                False,
                "Чат недоступен боту.",
                "Привяжите чат заново.",
            )
        return (
            False,
            f"Не удалось создать ссылку: {err_text}",
            "Проверьте права и тип чата.",
        )
    except Exception as err:
        logging.exception("Неожиданная ошибка при создании ссылки", exc_info=err)
        return (
            False,
            "Не удалось создать ссылку.",
            "См. логи.",
        )


def main_menu_markup() -> InlineKeyboardMarkup:
    """Создать клавиатуру с переходом в главное меню."""

    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def invite_button_markup(link: str, permanent: bool = False) -> InlineKeyboardMarkup:
    """Создать инлайн-кнопку для перехода по ссылке с возвратом в меню."""

    builder = InlineKeyboardBuilder()
    text = "➡️ Войти в канал" if not permanent else "⚠️ Постоянная ссылка"
    builder.button(text=text, url=link)
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


def build_docs_message() -> tuple[str, str]:
    """Сформировать текст и режим форматирования для списка документов."""

    docs = get_docs_map()
    items = [
        ("Согласие на рассылку", docs.get("newsletter", "")),
        ("Согласие на обработку ПД", docs.get("pd_consent", "")),
        ("Политика обработки ПД", docs.get("pd_policy", "")),
        ("Оферта", docs.get("offer", "")),
    ]
    lines = ["📄 Документы:"]
    for idx, (title, url) in enumerate(items, start=1):
        if url:
            lines.append(f"{idx}) [{title}]({url})")
        else:
            lines.append(f"{idx}) {title} — не указан")
    text = "\n".join(lines)
    return text, "Markdown"


def build_welcome_with_legal() -> tuple[str, InlineKeyboardMarkup]:
    """Подготовить приветствие с обязательным согласием и клавиатурой."""

    docs_text, _ = build_docs_message()
    text = (
        "👋 Добро пожаловать!\n"
        "Прежде чем продолжить, ознакомьтесь с документами ниже.\n"
        "_Нажимая «✅ Продолжить», вы подтверждаете согласие._\n\n"
        f"{docs_text}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Продолжить", callback_data="legal:accept")
    builder.button(text="📄 Открыть документы", callback_data="legal:docs")
    builder.adjust(1)
    return text, builder.as_markup()


def build_user_menu_keyboard(
    auto_on: bool, is_admin: bool, price_months: list[int]
) -> InlineKeyboardMarkup:
    """Собрать пользовательскую inline-клавиатуру."""

    builder = InlineKeyboardBuilder()
    for months in price_months[:6]:
        builder.button(
            text=f"💳 Купить {months} мес",
            callback_data=f"buy:months:{months}",
        )
    builder.button(
        text="💳 Оплатить подписку",
        callback_data="buy:open",
    )
    builder.button(
        text=f"🔁 Автопродление: {inline_emoji(auto_on)}",
        callback_data="ar:toggle",
    )
    builder.button(text="🔗 Получить ссылку", callback_data="invite:once")
    builder.button(text="🏷️ Ввести промокод", callback_data="promo:enter")
    builder.button(text="📄 Документы", callback_data="docs:open")
    if is_admin:
        builder.button(text="🛠️ Админ-панель", callback_data="admin:open")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


async def get_user_menu(db: DB, user_id: int) -> InlineKeyboardMarkup:
    """Получить клавиатуру пользователя с актуальными данными."""

    user = await db.get_user(user_id)
    auto_flag = bool(user and user["auto_renew"])
    price_months = [months for months, _ in await db.get_all_prices()]
    return build_user_menu_keyboard(auto_flag, is_super_admin(user_id), price_months)


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
    prices = await db.get_all_prices()
    if prices:
        parts = [f"{months} мес — {price}₽" for months, price in prices]
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
    builder.button(text="💰 Тарифы и цены", callback_data="admin:prices")
    builder.button(text="🗓️ Пробный период", callback_data="admin:trial_days")
    builder.button(
        text=f"🔁 Автопродление по умолчанию: {inline_emoji(auto_default)}",
        callback_data="admin:auto_default",
    )
    builder.button(text="🏷️ Создать пробный промокод", callback_data="admin:create_coupon")
    builder.button(text="🛡️ Проверить права бота", callback_data="admin:check_rights")
    builder.adjust(2, 2, 1, 1)

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


async def build_price_list_view(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    """Сформировать текст и клавиатуру списка тарифов."""

    prices = await db.get_all_prices()
    lines = ["💰 Тарифы", "Выберите действие."]
    if prices:
        lines.append("")
        for months, price in prices:
            lines.append(f"{months} мес — {price}₽")
    else:
        lines.append("")
        lines.append("Тарифов пока нет.")
    text = "\n".join(escape_md(line) if line else "" for line in lines)

    builder = InlineKeyboardBuilder()
    for months, _ in prices:
        builder.button(text="✏️ Редактировать", callback_data=f"price:edit:{months}")
        builder.button(text="🗑️ Удалить", callback_data=f"price:del:{months}")
    builder.button(text="➕ Добавить тариф", callback_data="price:add")
    builder.button(text="⬅️ Назад", callback_data="admin:open")
    builder.adjust(2, 1, 1)
    return text, builder.as_markup()


async def render_price_list(message: Message, db: DB) -> None:
    """Показать экран управления тарифами."""

    text, markup = await build_price_list_view(db)
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


async def render_price_list_by_state(bot: Bot, state: FSMContext, db: DB) -> None:
    """Обновить экран тарифов по сохранённым идентификаторам."""

    data = await state.get_data()
    chat_id = data.get("price_chat_id")
    message_id = data.get("price_message_id")
    if not chat_id or not message_id:
        return
    text, markup = await build_price_list_view(db)
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


async def render_price_edit(message: Message, months: int) -> None:
    """Показать мини-меню редактирования тарифа."""

    lines = [f"Изменить тариф {months} мес", "Выберите действие."]
    text = "\n".join(escape_md(line) for line in lines)
    builder = InlineKeyboardBuilder()
    builder.button(text="⌛ Изменить месяцы", callback_data=f"price:editm:{months}")
    builder.button(text="💵 Изменить цену", callback_data=f"price:editp:{months}")
    builder.button(text="⬅️ Назад", callback_data="price:list")
    builder.adjust(2, 1)
    try:
        await message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await message.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )


async def render_price_delete_confirm(message: Message, months: int) -> None:
    """Показать подтверждение удаления тарифа."""

    text = escape_md(f"Удалить тариф {months} мес?")
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data=f"price:confirm_del:{months}")
    builder.button(text="❌ Нет", callback_data="price:list")
    builder.adjust(2)
    try:
        await message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        await message.answer(
            text,
            reply_markup=builder.as_markup(),
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
    existing_user = await db.get_user(user_id)
    paid_only = True
    if await has_trial_coupon(db, user_id):
        paid_only = False
    if existing_user is None:
        await db.upsert_user(user_id, now_ts, trial_days, auto_default, paid_only)
        user = await db.get_user(user_id)
    else:
        user = existing_user
        if not paid_only and user and user["paid_only"]:
            await db.set_paid_only(user_id, False)
            user = await db.get_user(user_id)
    if not user:
        return
    if not await db.has_accepted_legal(user_id):
        text, markup = build_welcome_with_legal()
        await message.answer(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return
    menu = await get_user_menu(db, user_id)
    await message.answer(
        escape_md(START_TEXT),
        reply_markup=menu,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "menu:home")
async def handle_menu_home(callback: CallbackQuery, state: FSMContext, db: DB) -> None:
    """Вернуть пользователя в главное меню по кнопке."""

    await state.clear()
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if user is None:
        if callback.message:
            await callback.message.answer(
                "Сначала выполните /start.",
                reply_markup=None,
            )
        await callback.answer("Требуется команда /start", show_alert=True)
        return

    if not await db.has_accepted_legal(user_id):
        if callback.message:
            text, markup = build_welcome_with_legal()
            await callback.message.answer(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        await callback.answer()
        return

    menu = await get_user_menu(db, user_id)
    if callback.message:
        await callback.message.answer(
            escape_md(START_TEXT),
            reply_markup=menu,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "legal:docs")
async def legal_show_docs(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Показать документы во время подтверждения согласия."""

    if callback.message:
        data = await state.get_data()
        prev_chat = data.get("legal_doc_chat_id")
        prev_message = data.get("legal_doc_message_id")
        if prev_chat and prev_message:
            try:
                await bot.delete_message(prev_chat, prev_message)
            except TelegramBadRequest:
                pass
        text, parse_mode = build_docs_message()
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="legal:back")
        builder.adjust(1)
        markup = builder.as_markup()
        sent = None
        try:
            sent = await callback.message.answer(
                text,
                reply_markup=markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            sent = await callback.message.answer(
                text,
                reply_markup=markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        if sent:
            await state.update_data(
                legal_doc_message_id=sent.message_id,
                legal_doc_chat_id=sent.chat.id,
            )
    await callback.answer()


@router.callback_query(F.data == "legal:back")
async def legal_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрыть список документов и вернуться к согласию."""

    await state.update_data(legal_doc_message_id=None, legal_doc_chat_id=None)
    if callback.message:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            text, markup = build_welcome_with_legal()
            try:
                await callback.message.edit_text(
                    text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                await callback.message.answer(
                    text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
    await callback.answer()


@router.callback_query(F.data == "legal:accept")
async def legal_accept(callback: CallbackQuery, bot: Bot, state: FSMContext, db: DB) -> None:
    """Зафиксировать согласие пользователя и открыть меню."""

    user_id = callback.from_user.id
    now_ts = int(datetime.utcnow().timestamp())
    data = await state.get_data()
    doc_chat_id = data.get("legal_doc_chat_id")
    doc_message_id = data.get("legal_doc_message_id")
    if doc_chat_id and doc_message_id:
        try:
            await bot.delete_message(doc_chat_id, doc_message_id)
        except TelegramBadRequest:
            pass
    await db.set_accepted_legal(user_id, True, now_ts)
    if callback.message:
        try:
            await callback.message.edit_text(
                "✅ Спасибо! Можете продолжить.",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                "✅ Спасибо! Можете продолжить.",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
    menu = await get_user_menu(db, user_id)
    if callback.message:
        try:
            await callback.message.answer(
                escape_md(START_TEXT),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await bot.send_message(
                callback.message.chat.id,
                escape_md(START_TEXT),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
    else:
        await bot.send_message(
            user_id,
            escape_md(START_TEXT),
            reply_markup=menu,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "docs:open")
async def docs_open(callback: CallbackQuery, db: DB) -> None:
    """Показать документы из пользовательского меню."""

    user_id = callback.from_user.id
    if not await db.has_accepted_legal(user_id):
        await callback.answer("Сначала подтвердите согласие.", show_alert=True)
        return
    if callback.message:
        text, parse_mode = build_docs_message()
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="docs:back")
        builder.adjust(1)
        markup = builder.as_markup()
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                text,
                reply_markup=markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data == "docs:back")
async def docs_back(callback: CallbackQuery, db: DB) -> None:
    """Вернуться к пользовательскому меню из раздела документов."""

    if callback.message:
        menu = await get_user_menu(db, callback.from_user.id)
        try:
            await callback.message.edit_text(
                escape_md(START_TEXT),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                escape_md(START_TEXT),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data == "buy:open")
async def handle_buy_open(callback: CallbackQuery, db: DB) -> None:
    """Показать пользователю список тарифов для оплаты."""

    prices = await db.get_all_prices()
    if not prices:
        await callback.answer("Тарифы пока не настроены.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for months, price in prices[:6]:
        builder.button(
            text=f"{months} мес — {price}₽",
            callback_data=f"buy:months:{months}",
        )
    builder.button(text="❌ Отмена", callback_data="buy:cancel")
    builder.adjust(1)
    if callback.message:
        await callback.message.answer(
            "Выберите срок подписки для оплаты:",
            reply_markup=builder.as_markup(),
        )
    await callback.answer()


@router.callback_query(F.data == "buy:cancel")
async def handle_buy_cancel(callback: CallbackQuery) -> None:
    """Закрыть сообщение с выбором срока подписки."""

    if callback.message:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            await callback.answer("Сообщение уже закрыто.", show_alert=True)
            return
    await callback.answer()


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
    prices = await db.get_prices_dict()
    price = prices.get(months)
    if price is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    try:
        payment_url = await create_payment(user_id, price, months)
    except Exception as err:  # noqa: BLE001
        logging.exception("Не удалось создать платёж", exc_info=err)
        await callback.answer("Не удалось создать платёж. Попробуйте позже.", show_alert=True)
        return

    payment = await db.get_latest_payment(user_id, status="PENDING")
    payment_id = payment["payment_id"] if payment else None
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти к оплате 💳", url=payment_url)
    if payment_id:
        builder.button(text="Я оплатил ✅", callback_data=f"payment:check:{payment_id}")
    builder.adjust(1)
    if callback.message:
        text_lines = [
            f"💳 Оплата подписки на {months} мес.",
            f"Сумма к оплате: {price}₽.",
            "Нажмите кнопку ниже, чтобы перейти к платёжной странице.",
        ]
        await callback.message.answer(
            "\n".join(text_lines),
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
    await callback.answer("Ссылка на оплату готова.")


@router.callback_query(F.data.startswith("payment:check:"))
async def handle_payment_check(callback: CallbackQuery, db: DB) -> None:
    """Проверить статус платежа и продлить подписку."""

    parts = (callback.data or "").split(":")
    try:
        payment_id = parts[2]
    except IndexError:
        await callback.answer("Не удалось определить платёж.", show_alert=True)
        return

    payment = await db.get_payment_by_id(payment_id)
    if payment is None:
        await callback.answer("Платёж не найден. Свяжитесь с поддержкой.", show_alert=True)
        return

    try:
        confirmed = await check_payment_status(payment_id)
    except RuntimeError as err:
        await callback.answer(str(err), show_alert=True)
        return

    if not confirmed:
        await callback.answer("Платёж ещё обрабатывается. Попробуйте чуть позже.", show_alert=True)
        return

    user_id = int(payment["user_id"])
    months = int(payment["months"])
    await db.extend_subscription(user_id, months)
    await db.set_paid_only(user_id, False)
    await db.set_payment_status(payment_id, "CONFIRMED")

    user_after = await db.get_user(user_id)
    expires_at = user_after["expires_at"] if user_after else 0
    formatted_expiry = format_expiry(expires_at) if expires_at else None

    if callback.message:
        if formatted_expiry:
            display_text = (
                f"✅ Оплата подтверждена. Подписка продлена на {months} мес.\n"
                f"Новая дата окончания: {formatted_expiry}."
            )
        else:
            display_text = "✅ Оплата подтверждена и подписка продлена."
        await callback.message.answer(
            escape_md(display_text),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await refresh_user_menu(callback.message, db, user_id)
    await callback.answer("Оплата подтверждена.")


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

    if not await db.has_accepted_legal(callback.from_user.id):
        await callback.answer("Сначала подтвердите согласие.", show_alert=True)
        return
    ok, info, hint = await make_one_time_invite(bot, db)
    if callback.message:
        if ok:
            await callback.message.answer(
                escape_md("Ваша ссылка (действует 24ч, одноразовая)."),
                reply_markup=invite_button_markup(info),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        else:
            hint_value = hint or ""
            hint_lower = hint_value.lower()
            hint_is_link = hint_lower.startswith("http://") or hint_lower.startswith("https://")
            lines: list[str] = []
            if info:
                lines.append(escape_md(info))
            if hint and not hint_is_link:
                lines.append(escape_md(hint))
            combined_lower = " ".join(lines).lower()
            expired_line = escape_md("Ссылка устарела, запросите новую")
            if "устарел" not in combined_lower:
                lines.append(expired_line)
            text = "\n".join(lines) if lines else expired_line
            reply_markup = (
                invite_button_markup(hint_value, permanent=True) if hint_is_link else main_menu_markup()
            )
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
    if ok:
        await callback.answer()
    else:
        await callback.answer("Не удалось создать ссылку.", show_alert=True)


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
    """Запросить у администратора идентификатор целевого чата."""

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
            escape_md(
                "Пришлите @username, username или chat_id канала/группы."
            ),
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
    """Привязать чат по присланному идентификатору."""

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
    compact = "".join(text.split())
    if not compact:
        await message.answer(
            escape_md("Введите идентификатор чата или отмените команду."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    is_numeric_candidate = False
    if compact.startswith("-"):
        is_numeric_candidate = compact[1:].isdigit()
    elif compact.isdigit():
        is_numeric_candidate = True

    normalized_chat_id: int | None = None
    chat = None

    if is_numeric_candidate:
        digits = compact
        numeric_candidates: list[int] = []

        if digits.startswith("-"):
            try:
                numeric_candidates.append(int(digits))
            except ValueError:
                numeric_candidates = []
        else:
            try:
                value = int(digits)
            except ValueError:
                numeric_candidates = []
            else:
                if len(digits) >= 11 and digits.startswith("100"):
                    numeric_candidates.append(-value)
                try:
                    numeric_candidates.append(int(f"-100{digits}"))
                except ValueError:
                    pass
                numeric_candidates.append(-value)
                numeric_candidates.append(value)

        seen_candidates: set[int] = set()
        ordered_candidates: list[int] = []
        for candidate in numeric_candidates:
            if candidate not in seen_candidates:
                seen_candidates.add(candidate)
                ordered_candidates.append(candidate)

        last_error: Exception | None = None
        for candidate in ordered_candidates:
            try:
                chat = await bot.get_chat(candidate)
            except TelegramForbiddenError:
                await message.answer(
                    escape_md(
                        "Бот не имеет доступа к чату. Назначьте его администратором."
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                )
                return
            except TelegramBadRequest as err:
                last_error = err
                continue
            except Exception as err:
                logging.exception("Ошибка при получении чата", exc_info=err)
                await message.answer(
                    escape_md("Произошла ошибка при получении чата. Попробуйте позже."),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                )
                return
            else:
                normalized_chat_id = chat.id
                break

        if chat is None:
            logging.warning(
                "Не удалось подобрать чат по числовому идентификатору: %s", compact
            )
            await message.answer(
                escape_md("Не удалось получить чат. Проверьте идентификатор и права бота."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            if last_error is not None:
                logging.debug("Последняя ошибка Telegram: %s", last_error)
            return
    else:
        if not compact.startswith("@"):
            compact = f"@{compact}"
        try:
            chat = await bot.get_chat(compact)
        except TelegramBadRequest:
            await message.answer(
                escape_md("Не удалось получить чат. Проверьте идентификатор и права бота."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return
        except TelegramForbiddenError:
            await message.answer(
                escape_md("Бот не имеет доступа к чату. Назначьте его администратором."),
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

        normalized_chat_id = chat.id

    if normalized_chat_id is None or chat is None:
        await message.answer(
            escape_md("Не удалось определить чат. Проверьте введённые данные."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramForbiddenError:
        await message.answer(
            escape_md(
                "Бот не является администратором в чате. Выдайте права администратора."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    except TelegramBadRequest as err:
        logging.exception("Ошибка при проверке прав бота", exc_info=err)
        await message.answer(
            escape_md("Не удалось проверить права бота. Проверьте настройки чата."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    except Exception as err:
        logging.exception("Неожиданная ошибка при проверке прав бота", exc_info=err)
        await message.answer(
            escape_md("Не удалось проверить права бота. См. логи."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    member_status = getattr(member, "status", "")
    status_value = member_status.value if hasattr(member_status, "value") else str(member_status)
    if status_value not in {"administrator", "creator"}:
        await message.answer(
            escape_md("Бот не администратор в чате. Назначьте права и повторите."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    invite_allowed = getattr(member, "can_invite_users", None)
    if invite_allowed is False:
        await message.answer(
            escape_md(
                "У бота нет права на создание пригласительных ссылок. Включите его."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    stored_username = getattr(chat, "username", None)
    if stored_username:
        username_to_store = f"@{stored_username}"
    else:
        username_to_store = ""

    await db.set_target_chat_username(username_to_store)
    await db.set_target_chat_id(normalized_chat_id)

    if username_to_store:
        chat_repr = f"{username_to_store} (id {normalized_chat_id})"
    else:
        chat_repr = f"(id {normalized_chat_id})"

    await message.answer(
        escape_md(f"✅ Чат {chat_repr} привязан."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await refresh_admin_panel_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data == "admin:check_rights")
async def admin_check_rights(callback: CallbackQuery, bot: Bot, db: DB) -> None:
    """Показать диагностику прав бота в целевом чате."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        await callback.answer(
            "Чат не привязан. Откройте Админ-панель → Привязать чат.",
            show_alert=True,
        )
        return
    try:
        chat = await bot.get_chat(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as err:
        logging.exception("Не удалось получить чат", exc_info=err)
        await callback.answer("Не удалось получить чат. Привяжите его заново.", show_alert=True)
        return
    except Exception as err:
        logging.exception("Неожиданная ошибка при получении чата", exc_info=err)
        await callback.answer("Не удалось получить чат. Попробуйте позже.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin:open")
    builder.adjust(1)

    title = chat.title or "без названия"
    base_lines = [
        "🛡️ Права бота:",
        f"• Чат: {title} (id {chat_id}, {chat.type})",
    ]

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramForbiddenError:
        lines = base_lines + [
            "• Статус: нет доступа",
            "• Пригласительные ссылки: ❌",
            "• Рекомендация: назначьте бота админом и включите «Пригласительные ссылки».",
        ]
    except TelegramBadRequest as err:
        err_text = str(err)
        lines = base_lines + [
            f"• Статус: ошибка ({err_text})",
            "• Пригласительные ссылки: ❌",
            "• Рекомендация: откройте права бота → включите «Пригласительные ссылки».",
        ]
    except Exception as err:
        logging.exception("Ошибка при проверке прав бота", exc_info=err)
        lines = base_lines + [
            "• Статус: не удалось проверить",
            "• Пригласительные ссылки: ❌",
            "• Рекомендация: проверьте права администратора и попробуйте снова.",
        ]
    else:
        status_raw = getattr(member, "status", "unknown")
        status_display = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
        can_invite_attr = getattr(member, "can_invite_users", None)
        if can_invite_attr is None:
            invite_flag = "—"
        else:
            invite_flag = "✅" if can_invite_attr else "❌"
        if status_display not in {"administrator", "creator"}:
            recommendation = "• Рекомендация: назначьте бота администратором."
        elif can_invite_attr is False:
            recommendation = "• Рекомендация: откройте права бота → включите «Пригласительные ссылки»."
        else:
            recommendation = "• Рекомендация: всё в порядке."
        lines = base_lines + [
            f"• Статус: {status_display}",
            f"• Пригласительные ссылки: {invite_flag}",
            recommendation,
        ]

    text = "\n".join(escape_md(line) for line in lines)
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "admin:prices")
async def admin_prices(callback: CallbackQuery, state: FSMContext, db: DB) -> None:
    """Перейти к редактированию тарифов."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await render_price_list(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "price:list")
async def price_list_back(callback: CallbackQuery, db: DB) -> None:
    """Вернуться к списку тарифов."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_price_list(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "price:add")
async def price_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать добавление тарифа."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminPrice.AddMonths)
    if callback.message:
        await state.update_data(
            price_chat_id=callback.message.chat.id,
            price_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            escape_md("Введите длительность в месяцах (целое, ≥1)."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(AdminPrice.AddMonths)
async def price_add_months(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Принять количество месяцев нового тарифа."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await message.answer(
            escape_md("Создание тарифа отменено."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await render_price_list_by_state(bot, state, db)
        await state.clear()
        return
    if not text.isdigit():
        await message.answer(
            escape_md("Нужно целое число."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    months = int(text)
    if months < 1:
        await message.answer(
            escape_md("Количество месяцев должно быть ≥1."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    await state.update_data(new_price_months=months)
    await state.set_state(AdminPrice.AddPrice)
    await message.answer(
        escape_md("Введите цену в ₽ (целое, ≥0)."),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
        reply_markup=CANCEL_REPLY,
    )


@router.message(AdminPrice.AddPrice)
async def price_add_price(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Принять стоимость нового тарифа."""

    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if is_cancel(text):
        await message.answer(
            escape_md("Создание тарифа отменено."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await render_price_list_by_state(bot, state, db)
        await state.clear()
        return
    if not text.isdigit():
        await message.answer(
            escape_md("Нужно целое число."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    price = int(text)
    if price < 0:
        await message.answer(
            escape_md("Цена должна быть ≥0."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    data = await state.get_data()
    months = data.get("new_price_months")
    chat_id = data.get("price_chat_id")
    message_id = data.get("price_message_id")
    if months is None or chat_id is None or message_id is None:
        await message.answer(
            escape_md("Не удалось обновить тарифы. Откройте меню заново."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    await db.upsert_price(int(months), price)
    await message.answer(
        escape_md("✅ Тариф сохранён."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await render_price_list_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data.startswith("price:edit:"))
async def price_edit(callback: CallbackQuery, db: DB) -> None:
    """Открыть мини-меню редактирования тарифа."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    if callback.message:
        await render_price_edit(callback.message, months)
    await callback.answer()


@router.callback_query(F.data.startswith("price:editp:"))
async def price_edit_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к редактированию цены тарифа."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await state.set_state(AdminPrice.EditPrice)
    await state.update_data(
        price_chat_id=callback.message.chat.id if callback.message else None,
        price_message_id=callback.message.message_id if callback.message else None,
        edit_months=months,
    )
    if callback.message:
        await callback.message.answer(
            escape_md("Введите новую цену в ₽ (целое, ≥0)."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(AdminPrice.EditPrice)
async def price_edit_price_input(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Принять новую цену тарифа."""

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
        await render_price_list_by_state(bot, state, db)
        await state.clear()
        return
    if not text.isdigit():
        await message.answer(
            escape_md("Нужно целое число."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    new_price = int(text)
    if new_price < 0:
        await message.answer(
            escape_md("Цена должна быть ≥0."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    data = await state.get_data()
    months = data.get("edit_months")
    chat_id = data.get("price_chat_id")
    message_id = data.get("price_message_id")
    if months is None or chat_id is None or message_id is None:
        await message.answer(
            escape_md("Не удалось обновить тарифы. Откройте меню заново."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    await db.upsert_price(int(months), new_price)
    await message.answer(
        escape_md("✅ Цена обновлена."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await render_price_list_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data.startswith("price:editm:"))
async def price_edit_months(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к редактированию длительности тарифа."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await state.set_state(AdminPrice.EditMonths)
    await state.update_data(
        price_chat_id=callback.message.chat.id if callback.message else None,
        price_message_id=callback.message.message_id if callback.message else None,
        old_months=months,
    )
    if callback.message:
        await callback.message.answer(
            escape_md("Введите новое количество месяцев (целое, ≥1)."),
            reply_markup=CANCEL_REPLY,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(AdminPrice.EditMonths)
async def price_edit_months_input(message: Message, state: FSMContext, db: DB, bot: Bot) -> None:
    """Принять новую длительность тарифа."""

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
        await render_price_list_by_state(bot, state, db)
        await state.clear()
        return
    if not text.isdigit():
        await message.answer(
            escape_md("Нужно целое число."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    new_months = int(text)
    if new_months < 1:
        await message.answer(
            escape_md("Количество месяцев должно быть ≥1."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    data = await state.get_data()
    old_months = data.get("old_months")
    chat_id = data.get("price_chat_id")
    message_id = data.get("price_message_id")
    if old_months is None or chat_id is None or message_id is None:
        await message.answer(
            escape_md("Не удалось обновить тарифы. Откройте меню заново."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    prices = await db.get_prices_dict()
    current_price = prices.get(int(old_months))
    if current_price is None:
        await message.answer(
            escape_md("Тариф не найден."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await state.clear()
        return
    if new_months == int(old_months):
        await message.answer(
            escape_md("Изменений нет."),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await render_price_list_by_state(bot, state, db)
        await state.clear()
        return
    await db.upsert_price(new_months, current_price)
    await db.delete_price(int(old_months))
    await message.answer(
        escape_md("✅ Длительность обновлена."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await render_price_list_by_state(bot, state, db)
    await state.clear()


@router.callback_query(F.data.startswith("price:del:"))
async def price_delete(callback: CallbackQuery) -> None:
    """Запросить подтверждение удаления тарифа."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    if callback.message:
        await render_price_delete_confirm(callback.message, months)
    await callback.answer()


@router.callback_query(F.data.startswith("price:confirm_del:"))
async def price_confirm_delete(callback: CallbackQuery, db: DB) -> None:
    """Удалить тариф после подтверждения."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        months = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    deleted = await db.delete_price(months)
    if callback.message:
        await render_price_list(callback.message, db)
    if deleted:
        await callback.answer("Тариф удалён.")
    else:
        await callback.answer("Тариф не найден.", show_alert=True)


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
