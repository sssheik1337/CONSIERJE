from __future__ import annotations

from datetime import datetime, timedelta

from collections.abc import Mapping, Sequence
from typing import Any
import asyncio
import json
import re

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMember,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from db import DB
from keyboards import build_payment_method_keyboard
from logger import logger
from payments import check_payment_status, create_card_payment, form_sbp_qr, init_sbp_payment
from scheduler import RETRY_PAYMENT_CALLBACK, daily_check, try_auto_renew

router = Router()

DEFAULT_TRIAL_DAYS = 3
DEFAULT_AUTO_RENEW = True
COUPON_KIND_TRIAL = "trial"

MD_V2_SPECIAL = set("_*[]()~`>#+-=|{}.!\\")

CANCEL_REPLY = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⬅️ Назад")],
        [KeyboardButton(text="🏠 Главное меню")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
)

ADMIN_CANCEL_REPLY = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
)

START_TEXT = "🎟️ Доступ в канал\nВыберите действие ниже.\n\nℹ️ Пробный период доступен по промокоду."


def _safe_int(value: object) -> int:
    """Безопасно преобразовать значение в int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, object]:
    """Преобразовать строку БД в словарь."""

    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _normalize_payment_method(raw: str | None) -> str:
    """Нормализовать способ оплаты для внутренних колбэков."""

    if not raw:
        return "sbp"
    lowered = raw.strip().lower()
    if lowered == "sbp":
        return "sbp"
    if lowered == "card":
        return "card"
    return "sbp"


def _format_method_hint(method: str) -> str:
    """Вернуть описание способа оплаты для текстов пользователю."""

    if method == "card":
        return "картой"
    return "через СБП"


def _validate_contact_value(value: str) -> tuple[str | None, str | None]:
    """Проверить контакт пользователя и определить тип (телефон или email)."""

    if not value:
        return None, None
    cleaned = value.strip()
    phone_pattern = re.compile(r"^\+7\d{10}$")
    email_pattern = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")
    if phone_pattern.match(cleaned):
        return "phone", cleaned
    if email_pattern.match(cleaned):
        return "email", cleaned
    return None, None


def _build_consent_text(months: int, price: int, method: str) -> str:
    """Сформировать текст согласия перед оплатой в зависимости от метода."""

    base = [f"Условия подписки: сумма {price}₽, периодичность {months} мес."]
    if method == "sbp":
        details = [
            "",
            "Оплата проходит через СБП.",
            "Автопродление работает при привязанном счёте и включённом тумблере в личном меню бота.",
            "",
            "Нажимая кнопку «Я согласен», пользователь подтверждает согласие с условиями подписки.",
        ]
    else:
        details = [
            "",
            "При оплате картой автопродление будет доступно после подтверждения оплаты и получения RebillId.",
            "Вы сможете управлять автопродлением в личном меню бота (кнопка «Автопродление»).",
            "",
            "Списания будут происходить автоматически, если автопродление активно.",
            "Нажимая кнопку «Я согласен», пользователь подтверждает согласие с условиями подписки.",
        ]
    return "\n".join(base + details)


async def _ensure_subscription_state(
    bot: Bot | None,
    db: DB,
    user_row: aiosqlite.Row | None,
) -> tuple[aiosqlite.Row | None, bool]:
    """Проверить актуальность подписки и при необходимости инициировать автосписание."""

    if user_row is None:
        return None, True

    row_data = _row_to_dict(user_row)
    user_id = _safe_int(row_data.get("user_id"))
    now_ts = int(datetime.utcnow().timestamp())
    expires_at = _safe_int(row_data.get("expires_at"))
    auto_flag = bool(row_data.get("auto_renew"))

    if expires_at and expires_at < now_ts and auto_flag:
        if bot is None:
            logger.warning(
                "Не удалось инициировать автосписание при входе пользователя %s: бот отсутствует.",
                user_id,
            )
        else:
            try:
                await try_auto_renew(bot, db, user_row, now_ts)
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "Ошибка при запуске автопродления для пользователя %s", user_id, exc_info=err
                )
        user_row = await db.get_user(user_id)
        row_data = _row_to_dict(user_row)
        auto_flag = bool(row_data.get("auto_renew"))
        expires_at = _safe_int(row_data.get("expires_at"))
        now_ts = int(datetime.utcnow().timestamp())

    blocked = expires_at <= now_ts
    return user_row, blocked


class BindChat(StatesGroup):
    """Состояния для привязки чата по идентификатору."""

    wait_username = State()


class Admin(StatesGroup):
    """Состояния администратора для ввода параметров."""

    WaitTrialDays = State()
    WaitCustomCode = State()


class AdminDocs(StatesGroup):
    """Состояния администратора для настройки ссылок на документы."""

    WaitUrl = State()


class AdminBroadcast(StatesGroup):
    """Состояния администратора для рассылки сообщений."""

    WaitMessage = State()
    WaitButtonsMenu = State()
    WaitButtonText = State()
    WaitButtonUrl = State()
    WaitConfirm = State()


class AdminAuth(StatesGroup):
    """Состояния авторизации администратора."""

    WaitLogin = State()
    WaitPassword = State()


class AdminPrice(StatesGroup):
    """Состояния администратора для управления тарифами."""

    AddMonths = State()
    AddPrice = State()
    EditMonths = State()
    EditPrice = State()


class User(StatesGroup):
    """Состояния пользователя."""

    WaitPromoCode = State()


class BuyContactState(StatesGroup):
    """Состояние запроса контактных данных для чека."""

    waiting_for_contact = State()


def escape_md(text: str) -> str:
    """Экранировать текст для MarkdownV2."""

    return "".join(f"\\{char}" if char in MD_V2_SPECIAL else char for char in text)


def format_expiry(ts: int) -> str:
    """Отформатировать таймстамп в строку UTC."""

    return datetime.utcfromtimestamp(ts).strftime("%d.%m.%Y %H:%M UTC")


def format_short_date(ts: int) -> str:
    """Отформатировать дату в коротком виде ДД.ММ.ГГГГ."""

    return datetime.utcfromtimestamp(ts).strftime("%d.%m.%Y")


def _load_admin_ids() -> set[int]:
    """Загрузить список администраторов из файла."""

    path = (config.ADMIN_AUTH_FILE or "").strip()
    if not path:
        return set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return set()
    except Exception as err:  # noqa: BLE001
        logger.debug("Не удалось прочитать список администраторов: %s", err)
        return set()
    if isinstance(payload, dict):
        raw_ids = payload.get("admins", [])
    else:
        raw_ids = payload
    if not isinstance(raw_ids, list):
        return set()
    return {int(item) for item in raw_ids if str(item).isdigit()}


def _save_admin_id(user_id: int) -> None:
    """Сохранить пользователя в список администраторов."""

    path = (config.ADMIN_AUTH_FILE or "").strip()
    if not path:
        return
    ids = _load_admin_ids()
    ids.add(int(user_id))
    payload = {"admins": sorted(ids)}
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось сохранить список администраторов", exc_info=err)


def is_super_admin(user_id: int) -> bool:
    """Проверить, является ли пользователь суперадмином."""

    return user_id in _load_admin_ids()


def inline_emoji(flag: bool) -> str:
    """Вернуть эмодзи статуса."""

    return "✅" if flag else "❌"


def build_broadcast_buttons_menu(payment_enabled: bool) -> InlineKeyboardMarkup:
    """Собрать инлайн-клавиатуру управления кнопками для рассылки."""

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить кнопку", callback_data="admin:broadcast:buttons:add")
    builder.button(
        text=f"💳 Оплата: {inline_emoji(payment_enabled)}",
        callback_data="admin:broadcast:buttons:payment",
    )
    builder.button(text="👀 Предпросмотр", callback_data="admin:broadcast:buttons:preview")
    builder.button(text="❌ Отмена", callback_data="admin:broadcast:buttons:cancel")
    builder.adjust(1)
    return builder.as_markup()


def _broadcast_payment_enabled(buttons: list[dict[str, str]]) -> bool:
    """Проверить, включена ли кнопка оплаты в рассылке."""

    return any(entry.get("kind") == "payment" for entry in buttons)


def _toggle_broadcast_payment_button(buttons: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    """Переключить кнопку оплаты в списке кнопок."""

    enabled = _broadcast_payment_enabled(buttons)
    filtered = [entry for entry in buttons if entry.get("kind") != "payment"]
    if enabled:
        return filtered, False
    filtered.append({"kind": "payment"})
    return filtered, True


def _normalize_control_text(text: str | None) -> str:
    """Нормализовать текст кнопок управления для сравнения."""

    if text is None:
        return ""
    cleaned = (
        text.replace("🏠", "")
        .replace("⬅️", "")
        .replace("✅", "")
        .replace("❌", "")
        .strip()
        .lower()
    )
    return cleaned


def is_cancel(text: str | None) -> bool:
    """Понять, хочет ли пользователь отменить ввод."""

    cleaned = _normalize_control_text(text)
    return cleaned in {"отмена", "назад"}


def is_go_home(text: str | None) -> bool:
    """Понять, хочет ли пользователь вернуться в главное меню."""

    cleaned = _normalize_control_text(text)
    return cleaned in {"главное меню", "домой"}


async def has_trial_coupon(db: DB, user_id: int) -> bool:
    """Проверить, применял ли пользователь пробный промокод."""

    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM coupon_usages WHERE kind=? AND user_id=? LIMIT 1",
            (COUPON_KIND_TRIAL, user_id),
        )
        if await cur.fetchone() is not None:
            return True
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
        logger.exception("Ошибка при получении сведений о боте", exc_info=err)
        return (
            False,
            "Не удалось проверить права.",
            err_text,
        )
    except Exception as err:
        logger.exception("Не удалось получить сведения о боте", exc_info=err)
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
        logger.info(
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
            return (
                False,
                "Недостаточно прав для создания одноразовой ссылки.",
                "Дайте боту право «Пригласительные ссылки».",
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
        logger.exception("Неожиданная ошибка при создании ссылки", exc_info=err)
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


async def send_main_menu_screen(
    message: Message,
    db: DB,
    notice: str | None = None,
    *,
    bot: Bot | None = None,
) -> None:
    """Показать главное меню пользователю с удалением реплай-клавиатуры."""

    notice_text = notice or "Возвращаю в главное меню."
    await message.answer(
        escape_md(notice_text),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    effective_bot = bot or getattr(message, "bot", None)
    user = await db.get_user(message.from_user.id)
    user, blocked = await _ensure_subscription_state(effective_bot, db, user)
    menu = await get_user_menu(
        db,
        message.from_user.id,
        cached_user=user,
        blocked=blocked,
    )
    main_text = await compose_main_menu_text(
        db,
        message.from_user.id,
        cached_user=user,
        blocked=blocked,
    )
    await message.answer(
        escape_md(main_text),
        reply_markup=menu,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


async def go_home_from_state(
    message: Message,
    state: FSMContext,
    db: DB,
    notice: str | None = None,
    *,
    bot: Bot | None = None,
) -> None:
    """Очистить состояние и вернуть пользователя в главное меню."""

    await state.clear()
    await send_main_menu_screen(message, db, notice, bot=bot)


def invite_button_markup(link: str, permanent: bool = False) -> InlineKeyboardMarkup:
    """Создать инлайн-кнопку для перехода по ссылке с возвратом в меню."""

    builder = InlineKeyboardBuilder()
    text = "➡️ Войти в канал" if not permanent else "⚠️ Постоянная ссылка"
    builder.button(text=text, url=link)
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


async def _save_channel(event: ChatMemberUpdated, db: DB) -> None:
    """Сохранить канал при изменении статуса бота."""

    if event.chat.type != "channel":
        return
    status_raw = event.new_chat_member.status
    status_value = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
    if status_value in {"member", "administrator"}:
        username = getattr(event.chat, "username", None)
        username_value = f"@{username}" if username else ""
        await db.upsert_chat(event.chat.id, username_value, True)
        logger.info("Канал обнаружен и активирован: chat_id=%s", event.chat.id)
    elif status_value in {"left", "kicked"}:
        await db.set_chat_active(False)
        logger.info("Бот удалён из канала: chat_id=%s", event.chat.id)


DOCS_SETTINGS = {
    "newsletter": ("docs_newsletter_url", "Согласие на рассылку"),
    "pd_consent": ("docs_pd_consent_url", "Согласие на обработку ПД"),
    "pd_policy": ("docs_pd_policy_url", "Политика обработки ПД"),
    "offer": ("docs_offer_url", "Оферта"),
}


async def _get_docs_map(db: DB) -> dict[str, str]:
    """Вернуть словарь ссылок на документы с учётом настроек в БД."""

    result: dict[str, str] = {}
    for key, (setting_key, _) in DOCS_SETTINGS.items():
        stored = await db.get_setting(setting_key)
        value = (stored or "").strip()
        result[key] = value
    return result


async def build_docs_message(db: DB) -> tuple[str, str]:
    """Сформировать текст и режим форматирования для списка документов."""

    docs = await _get_docs_map(db)
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


async def build_welcome_with_legal(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    """Подготовить приветствие с обязательным согласием и клавиатурой."""

    docs_text, _ = await build_docs_message(db)
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
    builder.button(text="💳 Купить подписку", callback_data="buy:open")
    builder.button(
        text=f"🔁 Автопродление: {inline_emoji(auto_on)}",
        callback_data="ar:toggle",
    )
    builder.button(text="🔗 Получить ссылку", callback_data="invite:once")
    builder.button(text="🏷️ Ввести промокод", callback_data="promo:enter")
    builder.button(text="📄 Документы", callback_data="docs:open")
    if is_admin:
        builder.button(text="🛠️ Админ-панель", callback_data="admin:open")
    builder.adjust(1)
    return builder.as_markup()


def build_subscription_purchase_menu() -> InlineKeyboardMarkup:
    """Построить меню для пользователя без активной подписки."""

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Купить подписку", callback_data="buy:open")
    builder.adjust(1)
    return builder.as_markup()


async def get_user_menu(
    db: DB,
    user_id: int,
    *,
    cached_user: aiosqlite.Row | None = None,
    blocked: bool | None = None,
) -> InlineKeyboardMarkup:
    """Получить клавиатуру пользователя с актуальными данными."""

    user = cached_user or await db.get_user(user_id)
    auto_flag = bool(user and user["auto_renew"])
    price_months = [months for months, _ in await db.get_all_prices()]
    return build_user_menu_keyboard(auto_flag, is_super_admin(user_id), price_months)


async def compose_main_menu_text(
    db: DB,
    user_id: int,
    *,
    cached_user: aiosqlite.Row | None = None,
    blocked: bool | None = None,
) -> str:
    """Сформировать текст главного меню с указанием статуса доступа."""

    now_ts = int(datetime.utcnow().timestamp())
    user = cached_user or await db.get_user(user_id)
    if blocked is None:
        expires_at = _safe_int(user["expires_at"]) if user else 0
        blocked = expires_at <= now_ts
    trial_end = 0
    if user and hasattr(user, "keys") and "trial_end" in user.keys():
        try:
            trial_end = int(user["trial_end"] or 0)
        except (TypeError, ValueError):
            trial_end = 0
    subscription_end = await db.get_subscription_end(user_id) or 0
    if trial_end and now_ts < trial_end:
        status_line = f"🧪 Пробный период до: {format_short_date(trial_end)}"
    elif subscription_end and now_ts < subscription_end:
        status_line = f"✅ Подписка активна до: {format_short_date(subscription_end)}"
    else:
        status_line = "⛔ Нет активной подписки. Доступ к каналу закрыт."
    return f"{status_line}\n\n{START_TEXT}"


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

    text = escape_md("🛠️ Админ-панель. Выберите действие.")
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Настройки бота", callback_data="admin:settings")
    builder.button(text="📣 Опубликовать пост", callback_data="admin:broadcast")
    builder.adjust(1)

    return text, builder.as_markup()


async def build_admin_settings_panel(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    """Сформировать текст и клавиатуру настроек бота."""

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
        "⚙️ Настройки бота:",
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
    builder.button(text="📄 Ссылки на документы", callback_data="admin:docs")
    builder.button(text="🛡️ Проверить права бота", callback_data="admin:check_rights")
    builder.button(text="⬅️ Назад", callback_data="admin:open")
    builder.adjust(2, 2, 1, 1, 1, 1, 1)

    return text, builder.as_markup()


async def show_admin_panel(message: Message, db: DB) -> None:
    """Показать суперадмину актуальную админ-панель."""

    text, markup = await build_admin_panel(db)
    await message.answer(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


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


async def show_admin_settings_panel(message: Message, db: DB) -> None:
    """Показать суперадмину меню настроек бота."""

    text, markup = await build_admin_settings_panel(db)
    await message.answer(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


async def render_admin_settings_panel(message: Message, db: DB) -> None:
    """Отобразить или обновить меню настроек бота в заданном сообщении."""

    text, markup = await build_admin_settings_panel(db)
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


async def refresh_admin_settings_by_state(bot: Bot, state: FSMContext, db: DB) -> None:
    """Перерисовать меню настроек по сохранённым идентификаторам."""

    data = await state.get_data()
    chat_id = data.get("panel_chat_id")
    message_id = data.get("panel_message_id")
    if not chat_id or not message_id:
        return
    text, markup = await build_admin_settings_panel(db)
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
    lines = ["💰 Тарифы", "Выберите тариф для управления."]
    text = "\n".join(escape_md(line) for line in lines)

    builder = InlineKeyboardBuilder()
    for months, price in prices:
        builder.button(
            text=f"{months} мес — {price}₽",
            callback_data=f"price:edit:{months}",
        )
    builder.button(text="➕ Добавить тариф", callback_data="price:add")
    builder.button(text="⬅️ Назад", callback_data="admin:settings")
    builder.adjust(1)
    return text, builder.as_markup()


async def _send_price_list(
    bot: Bot,
    chat_id: int,
    db: DB,
    *,
    state: FSMContext | None = None,
    previous_message_id: int | None = None,
) -> None:
    """Отрисовать экран тарифов новой клавиатурой и при необходимости обновить стейт."""

    text, markup = await build_price_list_view(db)
    if previous_message_id:
        try:
            await bot.delete_message(chat_id, previous_message_id)
        except TelegramBadRequest:
            pass
    sent = await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    if state:
        await state.update_data(price_chat_id=chat_id, price_message_id=sent.message_id)


async def render_price_list(message: Message, db: DB, state: FSMContext | None = None) -> None:
    """Показать экран управления тарифами."""

    await _send_price_list(
        message.bot,
        message.chat.id,
        db,
        state=state,
        previous_message_id=message.message_id,
    )


async def render_price_list_by_state(bot: Bot, state: FSMContext, db: DB) -> None:
    """Обновить экран тарифов по сохранённым идентификаторам."""

    data = await state.get_data()
    chat_id = data.get("price_chat_id")
    message_id = data.get("price_message_id")
    if not chat_id:
        return
    await _send_price_list(
        bot,
        chat_id,
        db,
        state=state,
        previous_message_id=message_id,
    )


async def render_price_edit(message: Message, months: int) -> None:
    """Показать мини-меню редактирования тарифа."""

    lines = [f"Изменить тариф {months} мес", "Выберите действие."]
    text = "\n".join(escape_md(line) for line in lines)
    builder = InlineKeyboardBuilder()
    builder.button(text="⌛ Изменить месяцы", callback_data=f"price:editm:{months}")
    builder.button(text="💵 Изменить цену", callback_data=f"price:editp:{months}")
    builder.button(text="🗑️ Удалить", callback_data=f"price:del:{months}")
    builder.button(text="⬅️ Назад", callback_data="price:list")
    builder.adjust(2, 1, 1)
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
    subscription_end = await db.get_subscription_end(user_id) or 0
    trial_end_existing = 0
    if user and hasattr(user, "keys") and "trial_end" in user.keys():
        try:
            trial_end_existing = int(user["trial_end"] or 0)
        except (TypeError, ValueError):
            trial_end_existing = 0

    if user is None:
        auto_default = await db.get_auto_renew_default(DEFAULT_AUTO_RENEW)
        await db.upsert_user(user_id, now_ts, trial_days, auto_default, False)
        end_ts = now_ts + trial_seconds
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute(
                """
                UPDATE users
                SET trial_start=?, trial_end=?, expires_at=?, paid_only=0, invite_issued=0
                WHERE user_id=?
                """,
                (now_ts, end_ts, max(end_ts, subscription_end), user_id),
            )
            await conn.commit()
        return True, f"✅ Пробный доступ активирован до {format_expiry(end_ts)}."

    current_access = max(subscription_end, trial_end_existing)
    if current_access <= now_ts:
        new_end = now_ts + trial_seconds
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute(
                """
                UPDATE users
                SET trial_start=?, trial_end=?, expires_at=?, paid_only=0, invite_issued=0
                WHERE user_id=?
                """,
                (now_ts, new_end, max(new_end, subscription_end), user_id),
            )
            await conn.commit()
        return True, f"✅ Пробный доступ активирован до {format_expiry(new_end)}."

    await db.set_paid_only(user_id, False)
    return True, f"✅ Промокод принят. Подписка активна до {format_expiry(current_access)}."


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
    if is_super_admin(user_id):
        await show_admin_panel(message, db)
        return
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
        text, markup = await build_welcome_with_legal(db)
        await message.answer(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return
    user, blocked = await _ensure_subscription_state(message.bot, db, user)
    menu = await get_user_menu(db, user_id, cached_user=user, blocked=blocked)
    main_text = await compose_main_menu_text(
        db,
        user_id,
        cached_user=user,
        blocked=blocked,
    )
    await message.answer(
        escape_md(main_text),
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
            text, markup = await build_welcome_with_legal(db)
            await callback.message.answer(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        await callback.answer()
        return

    user, blocked = await _ensure_subscription_state(callback.bot, db, user)
    menu = await get_user_menu(db, user_id, cached_user=user, blocked=blocked)
    if callback.message:
        main_text = await compose_main_menu_text(
            db,
            user_id,
            cached_user=user,
            blocked=blocked,
        )
        await callback.message.answer(
            escape_md(main_text),
            reply_markup=menu,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(Command("test_expire_me"))
async def cmd_test_expire_me(message: Message, db: DB, bot: Bot) -> None:
    """Принудительно завершить подписку и триал для самотестирования суперадмина."""

    if not is_super_admin(message.from_user.id):
        await message.answer(
            escape_md("❌ Команда доступна только суперадминистратору."),
            reply_markup=main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return

    past_dt = datetime.utcnow() - timedelta(minutes=1)
    await db.set_subscription_end(message.from_user.id, past_dt)
    await db.set_trial_end(message.from_user.id, past_dt)
    try:
        await daily_check(bot, db)
    except Exception as err:  # noqa: BLE001
        logger.exception("Сбой тестовой проверки истечения подписки", exc_info=err)
    await send_main_menu_screen(
        message,
        db,
        notice="Тест: подписка и триал завершены, проверка истечения выполнена.",
        bot=bot,
    )


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
        text, parse_mode = await build_docs_message(db)
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
            text, markup = await build_welcome_with_legal(db)
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
    main_text = await compose_main_menu_text(db, user_id)
    if callback.message:
        try:
            await callback.message.answer(
                escape_md(main_text),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await bot.send_message(
                callback.message.chat.id,
                escape_md(main_text),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
    else:
        await bot.send_message(
            user_id,
            escape_md(main_text),
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
        text, parse_mode = await build_docs_message(db)
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
        main_text = await compose_main_menu_text(db, callback.from_user.id)
        try:
            await callback.message.edit_text(
                escape_md(main_text),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                escape_md(main_text),
                reply_markup=menu,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
    await callback.answer()

async def _send_payment_method_menu(callback: CallbackQuery) -> None:
    """Показать пользователю выбор способа оплаты."""

    if callback.message:
        await callback.message.answer(
            "Выберите способ оплаты:",
            reply_markup=build_payment_method_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("buy:open"))
async def handle_buy_open(callback: CallbackQuery, db: DB) -> None:
    """Показать пользователю список тарифов для оплаты."""

    parts = (callback.data or "").split(":")
    method_raw = parts[2] if len(parts) > 2 else None
    if method_raw is None:
        await _send_payment_method_menu(callback)
        return
    method = _normalize_payment_method(method_raw)
    prices = await db.get_all_prices()
    if not prices:
        await callback.answer("Тарифы пока не настроены.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for months, price in prices[:6]:
        builder.button(
            text=f"{months} мес — {price}₽",
            callback_data=f"buy:method:{method}:{months}",
        )
    builder.button(text="❌ Отмена", callback_data="buy:cancel")
    builder.adjust(1)
    if callback.message:
        method_hint = _format_method_hint(method)
        message_text = f"Выберите срок подписки для оплаты {method_hint}:"
        await callback.message.answer(
            message_text,
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


async def _send_payment_consent(
    callback: CallbackQuery,
    method: str,
    months: int,
    price: int,
    user_row: aiosqlite.Row | None,
) -> None:
    """Показать пользователю текст согласия перед созданием платежа."""

    consent_text = _build_consent_text(months, price, method)
    builder = InlineKeyboardBuilder()
    builder.button(text="✔ Я согласен", callback_data=f"buy:confirm:{method}:{months}")
    builder.button(text="❌ Отмена", callback_data="buy:cancel")
    builder.adjust(1)
    if callback.message:
        hint = _format_method_hint(method)
        await callback.message.answer(
            f"{consent_text}\n\nСпособ оплаты: {hint}.",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
    await callback.answer()


async def _request_contact_details(
    callback: CallbackQuery,
    state: FSMContext,
    method: str,
    months: int,
    price: int,
) -> None:
    """Запросить у пользователя контактные данные для формирования чека."""

    await state.set_state(BuyContactState.waiting_for_contact)
    await state.update_data(
        pending_method=method,
        pending_months=months,
        pending_price=price,
    )
    if callback.message:
        contact_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Поделиться телефоном", request_contact=True)],
                [KeyboardButton(text="Отмена")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await callback.message.answer(
            "Укажи телефон в формате +7XXXXXXXXXX или email, чтобы получить чек.",
            reply_markup=contact_keyboard,
        )
    await callback.answer("Ожидаю контакт для чека.")


async def _handle_buy_callback(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
    """Общая логика создания платежей по выбранному тарифу."""

    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    method = "sbp"
    months_value = None
    confirmed = False
    if len(parts) >= 4 and parts[1] == "confirm":
        confirmed = True
        method = _normalize_payment_method(parts[2])
        months_value = parts[3]
    elif len(parts) >= 4 and parts[1] == "method":
        method = _normalize_payment_method(parts[2])
        months_value = parts[3]
    elif len(parts) >= 3:
        months_value = parts[2]
    try:
        months = int(months_value) if months_value is not None else 0
    except (TypeError, ValueError):
        months = 0
    if months <= 0:
        await callback.answer("Не удалось определить срок подписки.", show_alert=True)
        return
    prices = await db.get_prices_dict()
    price = prices.get(months)
    if price is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    user_row = await db.get_user(user_id)
    if not confirmed:
        await _send_payment_consent(callback, method, months, price, user_row)
        return
    await _request_contact_details(callback, state, method, months, price)
    return


async def _send_sbp_payment_details(
    message: Message,
    user_id: int,
    months: int,
    price: int,
    payment_id: str,
    db: DB,
) -> None:
    """Сформировать QR/ссылку для оплаты через СБП и отправить пользователю."""

    try:
        qr_result = await form_sbp_qr(user_id, payment_id, db=db)
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось получить QR для СБП", exc_info=err)
        await message.answer(
            "Не удалось сформировать QR. Попробуйте позже.",
            reply_markup=main_menu_markup(),
        )
        return

    if qr_result is None:
        warning_text = "❗ Ошибка получения QR для СБП. Попробуйте позже или оплатите позже."
        await message.answer(warning_text, reply_markup=main_menu_markup())
        return

    builder = InlineKeyboardBuilder()
    qr_url = qr_result.get("qr_url")
    payload_url = qr_result.get("payload")
    payment_link = qr_url or payload_url
    if payment_link:
        builder.button(text="Оплатить", url=str(payment_link))
    builder.button(text="Я оплатил ✅", callback_data=f"payment:check:{payment_id}")
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(1)

    message_lines = [
        "📲 Оплата подписки через СБП.",
        f"Срок: {months} мес., сумма: {price}₽.",
        "Отсканируйте QR-код в приложении банка.",
    ]

    if not payment_link:
        payload_text = qr_result.get("payload") or "(данные QR недоступны)"
        message_lines.extend([
            "",
            "QR payload:",
            str(payload_text),
        ])
    await message.answer(
        "\n".join(message_lines),
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )


async def _send_card_payment_details(
    message: Message,
    months: int,
    price: int,
    payment_id: str,
    payment_url: str | None,
) -> None:
    """Отправить пользователю ссылку на оплату картой."""

    builder = InlineKeyboardBuilder()
    if payment_url:
        builder.button(text="Оплатить", url=str(payment_url))
    builder.button(text="Я оплатил ✅", callback_data=f"payment:check:{payment_id}")
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(1)

    message_lines = [
        "💳 Оплата подписки картой.",
        f"Срок: {months} мес., сумма: {price}₽.",
        "Оплатите на стороне T-Bank.",
    ]
    await message.answer(
        "\n".join(message_lines),
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )


async def _create_sbp_payment_with_contact(
    message: Message,
    db: DB,
    user_id: int,
    months: int,
    price: int,
    contact_type: str,
    contact_value: str,
) -> None:
    """Создать платёж СБП с учётом контактных данных и отправить ссылку оплаты."""

    try:
        init_result = await init_sbp_payment(
            user_id,
            months,
            price,
            contact_type,
            contact_value,
            db=db,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("Init СБП не удался", exc_info=err)
        await message.answer(
            "Не удалось создать платёж СБП. Попробуйте позже.",
            reply_markup=main_menu_markup(),
        )
        return

    payment_id = init_result.get("payment_id")
    if not payment_id:
        await message.answer(
            "T-Bank не вернул PaymentId. Попробуйте позже.",
            reply_markup=main_menu_markup(),
        )
        return

    await _send_sbp_payment_details(message, user_id, months, price, payment_id, db)

@router.callback_query(F.data.startswith("buy:months:"))
async def handle_buy(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
    """Совместимость со старыми кнопками покупки."""

    await _handle_buy_callback(callback, db, state)


@router.callback_query(F.data.startswith("buy:method:"))
async def handle_buy_with_method(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
    """Создание оплаты с указанием конкретного способа."""

    await _handle_buy_callback(callback, db, state)


@router.callback_query(F.data.startswith("buy:confirm:"))
async def handle_buy_confirm(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
    """Создание оплаты после подтверждения согласия."""

    await _handle_buy_callback(callback, db, state)


@router.message(BuyContactState.waiting_for_contact)
async def handle_buy_contact_input(message: Message, state: FSMContext, db: DB) -> None:
    """Получить телефон или email для формирования чека перед оплатой."""

    contact_type = None
    contact_value = None
    if message.contact and message.contact.phone_number:
        raw_phone = str(message.contact.phone_number).strip()
        if raw_phone and not raw_phone.startswith("+"):
            raw_phone = f"+{raw_phone}"
        contact_type, contact_value = "phone", raw_phone
    else:
        contact_type, contact_value = _validate_contact_value(message.text or "")
    if not contact_type:
        await message.answer("Отправь телефон в формате +7XXXXXXXXXX или email.")
        return

    data = await state.get_data()
    await state.clear()
    try:
        months = int(data.get("pending_months") or 0)
    except (TypeError, ValueError):
        months = 0
    try:
        price = int(data.get("pending_price") or 0)
    except (TypeError, ValueError):
        price = 0
    method = str(data.get("pending_method") or "sbp").strip().lower()
    if months <= 0 or price <= 0:
        await message.answer("Не удалось определить параметры оплаты. Попробуйте ещё раз.", reply_markup=main_menu_markup())
        return
    if method not in {"sbp", "card"}:
        method = "sbp"

    try:
        await db.set_user_contact(message.from_user.id, contact_value)
    except Exception as err:  # noqa: BLE001
        logger.debug("Не удалось сохранить контакт пользователя %s: %s", message.from_user.id, err)

    if method == "card":
        try:
            payment_url = await create_card_payment(
                message.from_user.id,
                months,
                price,
            )
        except Exception as err:  # noqa: BLE001
            logger.exception("Init оплаты картой не удался", exc_info=err)
            await message.answer(
                "Не удалось создать платёж картой. Попробуйте позже.",
                reply_markup=main_menu_markup(),
            )
            return

        latest_payment = await db.get_latest_payment(message.from_user.id)
        payment_id = ""
        if latest_payment is not None:
            try:
                payment_id = str(latest_payment["payment_id"] or "")
            except (KeyError, TypeError, ValueError):
                payment_id = ""
        if not payment_id:
            await message.answer(
                "T-Bank не вернул PaymentId. Попробуйте позже.",
                reply_markup=main_menu_markup(),
            )
            return

        await _send_card_payment_details(
            message,
            months,
            price,
            payment_id,
            str(payment_url),
        )
        return

    await _create_sbp_payment_with_contact(
        message,
        db,
        message.from_user.id,
        months,
        price,
        contact_type,
        contact_value,
    )


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
        payment_method = str(payment["method"] or "")
    except (KeyError, TypeError, ValueError):
        payment_method = ""
    is_sbp_payment = payment_method.strip().lower() == "sbp"

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
    if not is_sbp_payment:
        try:
            await db.set_auto_renew(user_id, True)
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "Не удалось включить автопродление после подтверждения платежа %s: %s",
                payment_id,
                err,
            )

    subscription_end = await db.get_subscription_end(user_id) or 0
    formatted_expiry = format_expiry(subscription_end) if subscription_end else None

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
            reply_markup=main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await refresh_user_menu(callback.message, db, user_id)
    await callback.answer("Оплата подтверждена.")


@router.callback_query(F.data == RETRY_PAYMENT_CALLBACK)
async def handle_retry_payment(callback: CallbackQuery, db: DB) -> None:
    """Повторить списание через сохранённую карту."""

    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if user is None:
        await callback.answer("Сначала выполните /start.", show_alert=True)
        return

    row = dict(user)
    rebill_id = (row.get("rebill_id") or "").strip()
    customer_key = (row.get("customer_key") or "").strip()
    parent_payment = (row.get("rebill_parent_payment") or "").strip()

    missing = []
    if not rebill_id:
        missing.append("RebillId")
    if not customer_key:
        missing.append("CustomerKey")
    if not parent_payment:
        missing.append("родительский платёж")

    if missing:
        message = (
            "⚠️ Не удалось выполнить повторное списание: отсутствуют сохранённые данные карты. "
            "Оформите оплату заново с галочкой «Сохранить карту» или оплатите вручную."
        )
        if callback.message:
            await callback.message.answer(message)
        await callback.answer("Нет сохранённых данных для списания.", show_alert=True)
        return

    now_ts = int(datetime.utcnow().timestamp())
    result = await try_auto_renew(
        callback.bot,
        db,
        user,
        now_ts,
        force=True,
    )

    if result.success:
        if callback.message:
            try:
                await callback.message.edit_reply_markup()
            except TelegramBadRequest:
                pass
        await callback.answer("Подписка продлена.")
        return

    await callback.answer("Не удалось выполнить списание. Попробуйте позже.", show_alert=True)


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
    message = "Автопродление включено." if new_flag else "Автопродление отключено."
    await callback.answer(message)


@router.callback_query(F.data == "invite:once")
async def handle_invite(callback: CallbackQuery, bot: Bot, db: DB) -> None:
    """Выдать одноразовую ссылку в целевой чат."""

    if not await db.has_accepted_legal(callback.from_user.id):
        await callback.answer("Сначала подтвердите согласие.", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)

    async def send_invite_failure(info_text: str, hint_text: str | None) -> None:
        """Отправить пользователю сообщение о невозможности выдать ссылку."""

        if not callback.message:
            return
        hint_value = hint_text or ""
        hint_lower = hint_value.lower()
        hint_is_link = hint_lower.startswith("http://") or hint_lower.startswith("https://")
        lines: list[str] = []
        if info_text:
            lines.append(escape_md(info_text))
        if hint_text and not hint_is_link:
            lines.append(escape_md(hint_value))
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

    now_ts = int(datetime.utcnow().timestamp())
    subscription_end = await db.get_subscription_end(callback.from_user.id) or 0
    trial_end = 0
    if user and hasattr(user, "keys") and "trial_end" in user.keys():
        try:
            trial_end = int(user["trial_end"] or 0)
        except (TypeError, ValueError):
            trial_end = 0
    has_active_subscription = subscription_end > now_ts
    has_active_trial = trial_end > now_ts

    if not has_active_subscription and not has_active_trial:
        if callback.message:
            builder = InlineKeyboardBuilder()
            builder.button(text="📲 Оплатить через СБП", callback_data="buy:open:sbp")
            builder.button(text="🎟 Ввести промокод", callback_data="promo:enter")
            builder.button(text="🏠 Главное меню", callback_data="menu:home")
            builder.adjust(1)
            await callback.message.answer(
                escape_md("У вас нет активной подписки. Оформите доступ или введите промокод."),
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        await callback.answer("Нет активной подписки", show_alert=True)
        return
    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        ok, info, hint = await make_one_time_invite(bot, db)
        await send_invite_failure(info, hint)
        await callback.answer("Чат не привязан", show_alert=True)
        return

    member: ChatMember | None = None
    try:
        member = await bot.get_chat_member(chat_id, callback.from_user.id)
    except TelegramForbiddenError as err:
        logger.warning("Не удалось проверить участие пользователя %s: %s", callback.from_user.id, err)
        ok, info, hint = await make_one_time_invite(bot, db)
        await send_invite_failure(info, hint)
        await callback.answer("Бот не имеет доступа к чату", show_alert=True)
        return
    except TelegramBadRequest as err:
        logger.warning(
            "Ошибка Telegram при проверке участия пользователя %s: %s",
            callback.from_user.id,
            err,
        )
        ok, info, hint = await make_one_time_invite(bot, db)
        await send_invite_failure(info, hint)
        await callback.answer("Не удалось проверить участие", show_alert=True)
        return
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "Сбой при проверке участия пользователя %s в канале", callback.from_user.id, exc_info=err
        )
        if callback.message:
            await callback.message.answer(
                escape_md(
                    "Не удалось проверить участие в канале. Попробуйте позже или обратитесь к администратору."
                ),
                reply_markup=main_menu_markup(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        await callback.answer("Ошибка проверки участия", show_alert=True)
        return

    status_raw = getattr(member, "status", "") if member else ""
    status_value = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
    if status_value.lower() in {"member", "administrator", "creator", "owner"}:
        if callback.message:
            await callback.message.answer(
                escape_md("Вы уже являетесь участником канала, пригласительная ссылка вам не нужна."),
                reply_markup=main_menu_markup(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        await callback.answer()
        return

    invite_flag = 0
    if user and hasattr(user, "keys") and "invite_issued" in user.keys():
        try:
            invite_flag = int(user["invite_issued"] or 0)
        except (TypeError, ValueError):
            invite_flag = 0
    if invite_flag:
        if callback.message:
            await callback.message.answer(
                escape_md(
                    "Вы уже использовали свою одноразовую ссылку. Если вы вышли из канала, свяжитесь с администратором"
                    " для восстановления доступа."
                ),
                reply_markup=main_menu_markup(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        await callback.answer("Ссылка уже выдавалась", show_alert=True)
        return

    ok, info, hint = await make_one_time_invite(bot, db)
    if ok:
        logger.info(
            "Выдана одноразовая ссылка пользователю %s для чата %s",
            callback.from_user.id,
            chat_id,
        )

    if callback.message:
        if ok:
            await callback.message.answer(
                escape_md("Ваша ссылка (действует 24ч, одноразовая)."),
                reply_markup=invite_button_markup(info),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        else:
            await send_invite_failure(info, hint)
    if ok:
        await callback.answer()
    else:
        await callback.answer("Не удалось создать ссылку.", show_alert=True)


@router.chat_member()
async def handle_chat_member_update(event: ChatMemberUpdated, db: DB) -> None:
    """Отметить использование одноразовой ссылки при вступлении пользователя."""

    if event.new_chat_member.user.id == event.bot.id:
        await _save_channel(event, db)
        return

    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None or event.chat.id != target_chat_id:
        return

    joined_statuses = {"member", "administrator", "creator"}
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    new_value = new_status.value if hasattr(new_status, "value") else str(new_status)
    old_value = old_status.value if hasattr(old_status, "value") else str(old_status)
    if new_value in joined_statuses and old_value not in joined_statuses:
        user_id = event.new_chat_member.user.id
        await db.set_invite_issued(user_id, True)
        logger.info(
            "Подтверждено вступление пользователя %s в чат %s, ссылка помечена как использованная",
            user_id,
            target_chat_id,
        )


@router.my_chat_member()
async def handle_my_chat_member_update(event: ChatMemberUpdated, db: DB) -> None:
    """Обработать изменения статуса бота в чате/канале."""

    await _save_channel(event, db)


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
    if is_go_home(text):
        await go_home_from_state(message, state, db, "Возвращаю вас в главное меню.")
        return
    if is_cancel(text):
        await go_home_from_state(message, state, db, "Ввод промокода отменён.")
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


@router.message(Command("admin_auth"))
async def admin_auth_start(message: Message, state: FSMContext) -> None:
    """Запустить скрытую авторизацию администратора."""

    await state.set_state(AdminAuth.WaitLogin)
    await message.answer("Введите логин администратора.")


@router.message(AdminAuth.WaitLogin)
async def admin_auth_login(message: Message, state: FSMContext) -> None:
    """Принять логин администратора."""

    login = (message.text or "").strip()
    if not login:
        await message.answer("Логин не должен быть пустым. Введите ещё раз.")
        return
    await state.update_data(admin_login=login)
    await state.set_state(AdminAuth.WaitPassword)
    await message.answer("Введите пароль администратора.")


@router.message(AdminAuth.WaitPassword)
async def admin_auth_password(message: Message, state: FSMContext) -> None:
    """Принять пароль администратора и авторизовать пользователя."""

    password = (message.text or "").strip()
    data = await state.get_data()
    login = str(data.get("admin_login") or "")
    if login == config.ADMIN_LOGIN and password == config.ADMIN_PASSWORD:
        _save_admin_id(message.from_user.id)
        await message.answer("✅ Администратор успешно авторизован.")
    else:
        await message.answer("❌ Неверный логин или пароль.")
    await state.clear()


@router.callback_query(F.data == "admin:open")
async def open_admin_panel(callback: CallbackQuery, db: DB) -> None:
    """Открыть админ-панель."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_admin_panel(callback.message, db)
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:settings")
async def open_admin_settings(callback: CallbackQuery, db: DB) -> None:
    """Открыть меню настроек бота."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_admin_settings_panel(callback.message, db)
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать рассылку поста администратором."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminBroadcast.WaitMessage)
    if callback.message:
        await callback.message.answer(
            "Отправьте текст поста в формате MarkdownV2.\n"
            "Для отмены вернитесь в админ-панель.",
        )
    await callback.answer()


def _build_broadcast_inline_markup(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    """Собрать инлайн-клавиатуру для рассылки из сохранённых кнопок."""

    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    added = False
    payment_added = False
    for entry in buttons:
        kind = entry.get("kind")
        if kind == "payment":
            if payment_added:
                continue
            builder.button(text="💳 Купить подписку", callback_data="buy:open")
            added = True
            payment_added = True
            continue
        text = entry.get("text", "")
        url = entry.get("url", "")
        if text and url:
            builder.button(text=text, url=url)
            added = True
    if not added:
        return None
    builder.adjust(1)
    return builder.as_markup()


async def _show_broadcast_preview(message: Message, state: FSMContext) -> None:
    """Показать предпросмотр рассылки и запросить подтверждение."""

    data = await state.get_data()
    preview_text = str(data.get("broadcast_text") or "")
    preview_entities = data.get("broadcast_entities") or []
    preview_buttons = data.get("broadcast_buttons") or []
    preview_markup = _build_broadcast_inline_markup(preview_buttons)
    if preview_entities:
        await message.answer(
            preview_text,
            entities=preview_entities,
            disable_web_page_preview=True,
            reply_markup=preview_markup,
        )
    else:
        await message.answer(
            preview_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=preview_markup,
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data="admin:broadcast:confirm")
    builder.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
    builder.adjust(1)
    await message.answer(
        "Предпросмотр сообщения ниже. Отправить рассылку?",
        reply_markup=builder.as_markup(),
    )


@router.message(AdminBroadcast.WaitMessage)
async def admin_broadcast_message(message: Message, state: FSMContext) -> None:
    """Принять текст рассылки от администратора."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Пост не должен быть пустым. Отправьте текст заново.")
        return
    entities = message.entities or []
    await state.update_data(
        broadcast_text=text,
        broadcast_entities=entities,
        broadcast_buttons=[],
    )
    await state.set_state(AdminBroadcast.WaitButtonsMenu)
    await message.answer(
        "Добавьте кнопку для поста или продолжите к предпросмотру.",
        reply_markup=build_broadcast_buttons_menu(payment_enabled=False),
    )


@router.message(AdminBroadcast.WaitButtonsMenu)
async def admin_broadcast_buttons_menu(message: Message, state: FSMContext) -> None:
    """Обработать выбор админа по кнопкам рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    choice = (message.text or "").strip()
    if is_cancel(choice):
        await state.clear()
        await message.answer("Рассылка отменена.")
        return
    data = await state.get_data()
    buttons = list(data.get("broadcast_buttons") or [])
    await message.answer(
        "Используйте кнопки под сообщением, чтобы управлять постом.",
        reply_markup=build_broadcast_buttons_menu(
            payment_enabled=_broadcast_payment_enabled(buttons),
        ),
    )


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:add")
async def admin_broadcast_buttons_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к вводу текста кнопки рассылки."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.set_state(AdminBroadcast.WaitButtonText)
    if callback.message:
        await callback.message.answer(
            "Отправьте текст для кнопки. Ссылка будет запрошена следующим сообщением.",
        )
    await callback.answer()


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:payment")
async def admin_broadcast_buttons_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Включить или выключить кнопку оплаты."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    data = await state.get_data()
    buttons = list(data.get("broadcast_buttons") or [])
    updated_buttons, enabled = _toggle_broadcast_payment_button(buttons)
    await state.update_data(broadcast_buttons=updated_buttons)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_broadcast_buttons_menu(payment_enabled=enabled),
            )
        except TelegramBadRequest:
            # Сообщение уже содержит актуальную клавиатуру.
            pass
    await callback.answer("Кнопка оплаты включена." if enabled else "Кнопка оплаты отключена.")


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:preview")
async def admin_broadcast_buttons_preview(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать предпросмотр рассылки."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.set_state(AdminBroadcast.WaitConfirm)
    if callback.message:
        await callback.message.answer("Готовлю предпросмотр.")
        await _show_broadcast_preview(callback.message, state)
    await callback.answer()


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:cancel")
async def admin_broadcast_buttons_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить рассылку до предпросмотра."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.message(AdminBroadcast.WaitButtonText)
async def admin_broadcast_button_text(message: Message, state: FSMContext) -> None:
    """Принять текст кнопки рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    button_text = (message.text or "").strip()
    if is_cancel(button_text):
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())
        return
    if not button_text:
        await message.answer("Текст кнопки не должен быть пустым. Попробуйте снова.")
        return
    await state.update_data(broadcast_button_text=button_text)
    await state.set_state(AdminBroadcast.WaitButtonUrl)
    await message.answer("Теперь отправьте ссылку для кнопки.")


@router.message(AdminBroadcast.WaitButtonUrl)
async def admin_broadcast_button_url(message: Message, state: FSMContext) -> None:
    """Принять ссылку для кнопки рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    button_url = (message.text or "").strip()
    if is_cancel(button_url):
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())
        return
    if not (button_url.startswith("https://") or button_url.startswith("http://")):
        await message.answer(
            "Ссылка для кнопки должна начинаться с http:// или https://. Попробуйте снова.",
        )
        return
    data = await state.get_data()
    button_text = str(data.get("broadcast_button_text") or "").strip()
    if not button_text:
        await state.set_state(AdminBroadcast.WaitButtonText)
        await message.answer("Текст кнопки не найден. Отправьте текст кнопки заново.")
        return
    buttons = list(data.get("broadcast_buttons") or [])
    buttons.append({"kind": "url", "text": button_text, "url": button_url})
    await state.update_data(broadcast_buttons=buttons)
    await state.set_state(AdminBroadcast.WaitButtonsMenu)
    await message.answer(
        "Кнопка добавлена. Добавим ещё?",
        reply_markup=build_broadcast_buttons_menu(
            payment_enabled=_broadcast_payment_enabled(buttons),
        ),
    )


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить рассылку поста."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:confirm")
async def admin_broadcast_confirm(
    callback: CallbackQuery, db: DB, state: FSMContext
) -> None:
    """Подтвердить и выполнить рассылку поста."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "")
    entities = data.get("broadcast_entities") or []
    buttons = data.get("broadcast_buttons") or []
    if not text.strip():
        await callback.answer("Текст рассылки не найден.", show_alert=True)
        await state.clear()
        return

    users = await db.list_users_for_broadcast()
    sent_count = 0
    blocked_count = 0
    error_count = 0
    delay_seconds = max(0.0, float(config.BROADCAST_DELAY_SECONDS or 0.0))
    markup = _build_broadcast_inline_markup(buttons)

    for user_id in users:
        if user_id == callback.from_user.id:
            continue
        try:
            if entities:
                await callback.bot.send_message(
                    user_id,
                    text,
                    entities=entities,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            else:
                await callback.bot.send_message(
                    user_id,
                    text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            sent_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramBadRequest as err:
            error_count += 1
            logger.debug("Ошибка рассылки для пользователя %s: %s", user_id, err)
        except Exception as err:  # noqa: BLE001
            error_count += 1
            logger.exception("Неожиданная ошибка рассылки для %s", user_id, exc_info=err)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

    summary = (
        "Рассылка завершена.\n"
        f"Отправлено: {sent_count}\n"
        f"Заблокировали бота: {blocked_count}\n"
        f"Ошибки: {error_count}"
    )
    if callback.message:
        await callback.message.answer(summary)
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "admin:settings")
async def open_admin_settings(callback: CallbackQuery, db: DB) -> None:
    """Открыть меню настроек бота."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_admin_settings_panel(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать рассылку поста администратором."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminBroadcast.WaitMessage)
    if callback.message:
        await callback.message.answer(
            "Отправьте текст поста в формате MarkdownV2.\n"
            "Для отмены вернитесь в админ-панель.",
        )
    await callback.answer()


def _build_broadcast_inline_markup(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    """Собрать инлайн-клавиатуру для рассылки из сохранённых кнопок."""

    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    added = False
    payment_added = False
    for entry in buttons:
        kind = entry.get("kind")
        if kind == "payment":
            if payment_added:
                continue
            builder.button(text="💳 Купить подписку", callback_data="buy:open")
            added = True
            payment_added = True
            continue
        text = entry.get("text", "")
        url = entry.get("url", "")
        if text and url:
            builder.button(text=text, url=url)
            added = True
    if not added:
        return None
    builder.adjust(1)
    return builder.as_markup()


async def _show_broadcast_preview(message: Message, state: FSMContext) -> None:
    """Показать предпросмотр рассылки и запросить подтверждение."""

    data = await state.get_data()
    preview_text = str(data.get("broadcast_text") or "")
    preview_entities = data.get("broadcast_entities") or []
    preview_buttons = data.get("broadcast_buttons") or []
    preview_markup = _build_broadcast_inline_markup(preview_buttons)
    if preview_entities:
        await message.answer(
            preview_text,
            entities=preview_entities,
            disable_web_page_preview=True,
            reply_markup=preview_markup,
        )
    else:
        await message.answer(
            preview_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=preview_markup,
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data="admin:broadcast:confirm")
    builder.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
    builder.adjust(1)
    await message.answer(
        "Предпросмотр сообщения ниже. Отправить рассылку?",
        reply_markup=builder.as_markup(),
    )


@router.message(AdminBroadcast.WaitMessage)
async def admin_broadcast_message(message: Message, state: FSMContext) -> None:
    """Принять текст рассылки от администратора."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Пост не должен быть пустым. Отправьте текст заново.")
        return
    entities = message.entities or []
    await state.update_data(
        broadcast_text=text,
        broadcast_entities=entities,
        broadcast_buttons=[],
    )
    await state.set_state(AdminBroadcast.WaitButtonsMenu)
    await message.answer(
        "Добавьте кнопку для поста или продолжите к предпросмотру.",
        reply_markup=build_broadcast_buttons_menu(payment_enabled=False),
    )


@router.message(AdminBroadcast.WaitButtonsMenu)
async def admin_broadcast_buttons_menu(message: Message, state: FSMContext) -> None:
    """Обработать выбор админа по кнопкам рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    choice = (message.text or "").strip()
    if is_cancel(choice):
        await state.clear()
        await message.answer("Рассылка отменена.")
        return
    data = await state.get_data()
    buttons = list(data.get("broadcast_buttons") or [])
    await message.answer(
        "Используйте кнопки под сообщением, чтобы управлять постом.",
        reply_markup=build_broadcast_buttons_menu(
            payment_enabled=_broadcast_payment_enabled(buttons),
        ),
    )


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:add")
async def admin_broadcast_buttons_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Перейти к вводу текста кнопки рассылки."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.set_state(AdminBroadcast.WaitButtonText)
    if callback.message:
        await callback.message.answer(
            "Отправьте текст для кнопки. Ссылка будет запрошена следующим сообщением.",
        )
    await callback.answer()


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:payment")
async def admin_broadcast_buttons_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Включить или выключить кнопку оплаты."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    data = await state.get_data()
    buttons = list(data.get("broadcast_buttons") or [])
    updated_buttons, enabled = _toggle_broadcast_payment_button(buttons)
    await state.update_data(broadcast_buttons=updated_buttons)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=build_broadcast_buttons_menu(payment_enabled=enabled),
        )
    await callback.answer("Кнопка оплаты включена." if enabled else "Кнопка оплаты отключена.")


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:preview")
async def admin_broadcast_buttons_preview(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать предпросмотр рассылки."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.set_state(AdminBroadcast.WaitConfirm)
    if callback.message:
        await callback.message.answer("Готовлю предпросмотр.")
        await _show_broadcast_preview(callback.message, state)
    await callback.answer()


@router.callback_query(AdminBroadcast.WaitButtonsMenu, F.data == "admin:broadcast:buttons:cancel")
async def admin_broadcast_buttons_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить рассылку до предпросмотра."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.message(AdminBroadcast.WaitButtonText)
async def admin_broadcast_button_text(message: Message, state: FSMContext) -> None:
    """Принять текст кнопки рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    button_text = (message.text or "").strip()
    if is_cancel(button_text):
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())
        return
    if not button_text:
        await message.answer("Текст кнопки не должен быть пустым. Попробуйте снова.")
        return
    await state.update_data(broadcast_button_text=button_text)
    await state.set_state(AdminBroadcast.WaitButtonUrl)
    await message.answer("Теперь отправьте ссылку для кнопки.")


@router.message(AdminBroadcast.WaitButtonUrl)
async def admin_broadcast_button_url(message: Message, state: FSMContext) -> None:
    """Принять ссылку для кнопки рассылки."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    button_url = (message.text or "").strip()
    if is_cancel(button_url):
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())
        return
    if not (button_url.startswith("https://") or button_url.startswith("http://")):
        await message.answer(
            "Ссылка для кнопки должна начинаться с http:// или https://. Попробуйте снова.",
        )
        return
    data = await state.get_data()
    button_text = str(data.get("broadcast_button_text") or "").strip()
    if not button_text:
        await state.set_state(AdminBroadcast.WaitButtonText)
        await message.answer("Текст кнопки не найден. Отправьте текст кнопки заново.")
        return
    buttons = list(data.get("broadcast_buttons") or [])
    buttons.append({"kind": "url", "text": button_text, "url": button_url})
    await state.update_data(broadcast_buttons=buttons)
    await state.set_state(AdminBroadcast.WaitButtonsMenu)
    await message.answer(
        "Кнопка добавлена. Добавим ещё?",
        reply_markup=build_broadcast_buttons_menu(
            payment_enabled=_broadcast_payment_enabled(buttons),
        ),
    )


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить рассылку поста."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:confirm")
async def admin_broadcast_confirm(
    callback: CallbackQuery, db: DB, state: FSMContext
) -> None:
    """Подтвердить и выполнить рассылку поста."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "")
    entities = data.get("broadcast_entities") or []
    buttons = data.get("broadcast_buttons") or []
    if not text.strip():
        await callback.answer("Текст рассылки не найден.", show_alert=True)
        await state.clear()
        return

    users = await db.list_users_for_broadcast()
    sent_count = 0
    blocked_count = 0
    error_count = 0
    delay_seconds = max(0.0, float(config.BROADCAST_DELAY_SECONDS or 0.0))
    markup = _build_broadcast_inline_markup(buttons)

    for user_id in users:
        if user_id == callback.from_user.id:
            continue
        try:
            if entities:
                await callback.bot.send_message(
                    user_id,
                    text,
                    entities=entities,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            else:
                await callback.bot.send_message(
                    user_id,
                    text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            sent_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramBadRequest as err:
            error_count += 1
            logger.debug("Ошибка рассылки для пользователя %s: %s", user_id, err)
        except Exception as err:  # noqa: BLE001
            error_count += 1
            logger.exception("Неожиданная ошибка рассылки для %s", user_id, exc_info=err)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

    summary = (
        "Рассылка завершена.\n"
        f"Отправлено: {sent_count}\n"
        f"Заблокировали бота: {blocked_count}\n"
        f"Ошибки: {error_count}"
    )
    if callback.message:
        await callback.message.answer(summary)
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "admin:bind_chat")
async def admin_bind_chat(callback: CallbackQuery, state: FSMContext, db: DB) -> None:
    """Запросить у администратора идентификатор целевого чата."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        chat_id = await db.get_target_chat_id()
        chat_username = await db.get_target_chat_username()
        if chat_id is None:
            await callback.message.answer(
                escape_md(
                    "Каналы не обнаружены. Добавьте бота в канал, затем вернитесь сюда."
                ),
                reply_markup=main_menu_markup(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            await callback.answer()
            return
        title = chat_username or f"id {chat_id}"
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"📌 {title}",
            callback_data=f"admin:bind_chat:select:{chat_id}",
        )
        builder.button(text="⬅️ Назад", callback_data="admin:settings")
        builder.adjust(1)
        await callback.message.answer(
            escape_md("Выберите канал для привязки:"),
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:bind_chat:select:"))
async def admin_bind_chat_select(callback: CallbackQuery, bot: Bot, db: DB) -> None:
    """Привязать канал по выбранной кнопке."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    raw_chat_id = parts[-1] if parts else ""
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        await callback.answer("Некорректный идентификатор чата.", show_alert=True)
        return
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramBadRequest as err:
        logger.exception("Ошибка при получении чата", exc_info=err)
        await callback.answer("Не удалось получить чат. Проверьте права бота.", show_alert=True)
        return
    except TelegramForbiddenError as err:
        logger.exception("Боту запрещён доступ к чату", exc_info=err)
        await callback.answer("Нет доступа к чату. Назначьте бота админом.", show_alert=True)
        return
    except Exception as err:  # noqa: BLE001
        logger.exception("Не удалось проверить чат", exc_info=err)
        await callback.answer("Не удалось проверить чат. См. логи.", show_alert=True)
        return

    status_raw = getattr(member, "status", "")
    status_value = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
    if status_value not in {"administrator", "creator"}:
        await callback.answer("Бот не администратор в чате.", show_alert=True)
        return
    invite_allowed = getattr(member, "can_invite_users", None)
    if invite_allowed is False:
        await callback.answer("Нет права «Пригласительные ссылки».", show_alert=True)
        return

    username = getattr(chat, "username", None)
    username_value = f"@{username}" if username else ""
    await db.set_target_chat_username(username_value)
    await db.set_target_chat_id(chat_id)
    await callback.answer("Чат привязан.", show_alert=True)
    if callback.message:
        await render_admin_settings_panel(callback.message, db)


@router.callback_query(F.data == "admin:docs")
async def admin_docs_menu(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
    """Показать меню настройки ссылок на документы."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    docs = await _get_docs_map(db)
    lines = ["📄 Ссылки на документы:"]
    for key, (_, title) in DOCS_SETTINGS.items():
        value = docs.get(key, "")
        if value:
            lines.append(f"• {title}: {value}")
        else:
            lines.append(f"• {title}: не указана")
    text = "\n".join(escape_md(line) for line in lines)
    builder = InlineKeyboardBuilder()
    for key, (_, title) in DOCS_SETTINGS.items():
        builder.button(text=f"✏️ {title}", callback_data=f"admin:docs:edit:{key}")
    builder.button(text="⬅️ Назад", callback_data="admin:settings")
    builder.adjust(1)
    if callback.message:
        await callback.message.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:docs:edit:"))
async def admin_docs_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Запросить новую ссылку на документ."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    key = parts[-1] if parts else ""
    if key not in DOCS_SETTINGS:
        await callback.answer("Неизвестный документ.", show_alert=True)
        return
    await state.set_state(AdminDocs.WaitUrl)
    await state.update_data(doc_key=key)
    title = DOCS_SETTINGS[key][1]
    if callback.message:
        await callback.message.answer(
            escape_md(
                f"Отправьте новую ссылку для «{title}».\n"
                "Чтобы очистить, отправьте «-»."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(AdminDocs.WaitUrl)
async def admin_docs_save(message: Message, state: FSMContext, db: DB) -> None:
    """Сохранить ссылку на документ."""

    if not is_super_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("doc_key")
    if key not in DOCS_SETTINGS:
        await message.answer("Не удалось определить документ.")
        await state.clear()
        return
    raw = (message.text or "").strip()
    setting_key, title = DOCS_SETTINGS[key]
    value = "" if raw == "-" else raw
    await db.set_setting(setting_key, value)
    await message.answer(
        escape_md(f"Ссылка для «{title}» обновлена."),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    await state.clear()


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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
                logger.exception("Ошибка при получении чата", exc_info=err)
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
            logger.warning(
                "Не удалось подобрать чат по числовому идентификатору: %s", compact
            )
            await message.answer(
                escape_md("Не удалось получить чат. Проверьте идентификатор и права бота."),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            if last_error is not None:
                logger.debug("Последняя ошибка Telegram: %s", last_error)
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
        logger.exception("Ошибка при проверке прав бота", exc_info=err)
        await message.answer(
            escape_md("Не удалось проверить права бота. Проверьте настройки чата."),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        return
    except Exception as err:
        logger.exception("Неожиданная ошибка при проверке прав бота", exc_info=err)
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
    await refresh_admin_settings_by_state(bot, state, db)
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
        logger.exception("Не удалось получить чат", exc_info=err)
        await callback.answer("Не удалось получить чат. Привяжите его заново.", show_alert=True)
        return
    except Exception as err:
        logger.exception("Неожиданная ошибка при получении чата", exc_info=err)
        await callback.answer("Не удалось получить чат. Попробуйте позже.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin:settings")
    builder.adjust(1)

    title = chat.title or "без названия"
    base_lines = [
        "🛡️ Права бота:",
        f"• Чат: {title} (id {chat_id}, {chat.type})",
        "• Требуемая роль: администратор",
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
        logger.exception("Ошибка при проверке прав бота", exc_info=err)
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
        can_ban_attr = getattr(member, "can_restrict_members", None)
        if can_ban_attr is None:
            can_ban_attr = getattr(member, "can_ban_users", None)
        if can_ban_attr is None:
            ban_flag = "—"
        else:
            ban_flag = "✅" if can_ban_attr else "❌"
        if status_display not in {"administrator", "creator"}:
            recommendation = "• Рекомендация: назначьте бота администратором."
        elif can_invite_attr is False:
            recommendation = "• Рекомендация: откройте права бота → включите «Пригласительные ссылки»."
        elif can_ban_attr is False:
            recommendation = "• Рекомендация: откройте права бота → включите «Бан пользователей»."
        else:
            recommendation = "• Рекомендация: всё в порядке."
        lines = base_lines + [
            f"• Статус: {status_display}",
            f"• Пригласительные ссылки: {invite_flag}",
            f"• Бан пользователей: {ban_flag}",
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
        await render_price_list(callback.message, db, state)
    await callback.answer()


@router.callback_query(F.data == "price:list")
async def price_list_back(callback: CallbackQuery, state: FSMContext, db: DB) -> None:
    """Вернуться к списку тарифов."""

    if not is_super_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.message:
        await render_price_list(callback.message, db, state)
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
        escape_md("Введите цену в ₽ (целое, ≥10)."),
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
    if price < 10:
        await message.answer(
            escape_md("Цена должна быть не меньше 10 ₽ из-за ограничений СБП."),
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
            escape_md("Введите новую цену в ₽ (целое, ≥10)."),
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
    if new_price < 10:
        await message.answer(
            escape_md("Цена должна быть не меньше 10 ₽ из-за ограничений СБП."),
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
    if current_price < 10:
        await message.answer(
            escape_md(
                "Сначала обновите цену тарифа до 10 ₽ и выше, затем меняйте длительность."
            ),
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
async def price_confirm_delete(callback: CallbackQuery, db: DB, state: FSMContext) -> None:
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
        await render_price_list(callback.message, db, state)
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
            reply_markup=ADMIN_CANCEL_REPLY,
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
    await refresh_admin_settings_by_state(bot, state, db)
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
        await render_admin_settings_panel(callback.message, db)
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
            reply_markup=ADMIN_CANCEL_REPLY,
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
    if is_go_home(text):
        await go_home_from_state(message, state, db)
        return
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
    await refresh_admin_settings_by_state(bot, state, db)
    await state.clear()


async def handle_sbp_notification_payload(
    payload: Mapping[str, Any], db: DB, bot: Bot | None = None
) -> bool:
    """Обработать уведомление T-Bank с AccountToken для СБП."""

    if not isinstance(payload, Mapping):
        return False
    request_key = str(
        payload.get("RequestKey")
        or payload.get("requestKey")
        or payload.get("REQUESTKEY")
        or ""
    ).strip()
    if not request_key:
        return False

    user_id = await db.get_user_by_request_key(request_key)
    if not user_id:
        logger.warning("СБП-уведомление: RequestKey %s не найден", request_key)
        return False

    params = payload.get("Params") if isinstance(payload.get("Params"), Mapping) else {}
    status = (payload.get("Status") or payload.get("status") or "").upper()
    account_token = (
        payload.get("AccountToken")
        or payload.get("accountToken")
        or (params.get("AccountToken") if isinstance(params, Mapping) else None)
    )
    bank_member_id = (
        payload.get("BankMemberId")
        or (params.get("BankMemberId") if isinstance(params, Mapping) else None)
    )
    bank_member_name = (
        payload.get("BankMemberName")
        or (params.get("BankMemberName") if isinstance(params, Mapping) else None)
    )

    if status:
        await db.update_sbp_status(user_id, status)
    if account_token:
        await db.save_account_token(
            user_id,
            str(account_token),
            bank_member_id=str(bank_member_id) if bank_member_id else None,
            bank_member_name=str(bank_member_name) if bank_member_name else None,
        )
        await db.set_auto_renew(user_id, True)
        payment_row = await db.get_payment_by_request_key(request_key)
        if payment_row and payment_row["payment_id"]:
            await db.set_payment_account_token(
                payment_row["payment_id"], str(account_token)
            )
        if bot:
            try:
                await bot.send_message(
                    user_id,
                    "Ваш счёт привязан, автопродление работает.",
                )
            except Exception:
                logger.debug(
                    "Не удалось отправить уведомление о привязке счёта пользователю %s",
                    user_id,
                )
    return True
