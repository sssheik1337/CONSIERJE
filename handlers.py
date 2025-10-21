from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from datetime import datetime, timedelta
import secrets
import re
from typing import Optional, Tuple
from config import config
from db import DB
from payments import process_payment

router = Router(name="core")
callback_router = Router(name="callbacks")

def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS


class AdminStates(StatesGroup):
    """Состояния администратора для диалогов и форм."""

    waiting_chat_username = State()
    waiting_trial_days = State()
    waiting_prices = State()
    waiting_trial_promo = State()


class UserStates(StatesGroup):
    """Состояния пользователя для диалогов по кнопкам."""

    waiting_promo_code = State()


def build_admin_keyboard(auto_renew_default: bool) -> ReplyKeyboardMarkup:
    """Построить reply-клавиатуру администратора с актуальным статусом."""

    autorenew_marker = "✅" if auto_renew_default else "❌"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Привязать чат"), KeyboardButton(text="Показать настройки")],
            [KeyboardButton(text="Редактировать цены")],
            [KeyboardButton(text="Установить пробный период")],
            [
                KeyboardButton(
                    text=f"Автопродление по умолчанию ({autorenew_marker})"
                )
            ],
            [KeyboardButton(text="Сгенерировать промокоды (trial)")],
        ],
        resize_keyboard=True,
    )


USER_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Получить ссылку")],
        [KeyboardButton(text="Статус подписки")],
        [KeyboardButton(text="Продлить подписку")],
        [KeyboardButton(text="Настроить автопродление")],
        [KeyboardButton(text="Ввести промокод")],
    ],
    resize_keyboard=True,
)


def build_user_inline_keyboard(auto_renew: bool) -> InlineKeyboardMarkup:
    """Построить пользовательское меню с учётом статуса автопродления."""

    autorenew_text = "Автопродление ✅" if auto_renew else "Автопродление ❌"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Получить ссылку", callback_data="user:get_link")],
            [InlineKeyboardButton(text="Статус подписки", callback_data="user:status")],
            [InlineKeyboardButton(text="Продлить подписку", callback_data="user:buy")],
            [
                InlineKeyboardButton(
                    text=autorenew_text, callback_data="user:autorenew:toggle"
                )
            ],
        ]
    )


def generate_promo_code(length: int = 10) -> str:
    """Сгенерировать промокод из удобочитаемых символов."""

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def apply_trial_promo(message: Message, code: str, db: DB) -> bool:
    """Проверить и активировать промокод на пробный период."""

    normalized = (code or "").strip()
    if not normalized:
        await message.answer("Промокод не должен быть пустым.")
        return False
    user_id = message.from_user.id
    now_ts = int(datetime.utcnow().timestamp())
    ok, error, details = await db.redeem_promo_code(normalized, user_id, now_ts)
    if not ok:
        await message.answer(error)
        return False
    if (details or {}).get("code_type") != "trial":
        await message.answer("Этот промокод не даёт пробный период.")
        return False
    trial_days_raw = details.get("trial_days") if details else None
    try:
        trial_days = int(trial_days_raw)
    except (TypeError, ValueError):
        await message.answer(
            "Для этого промокода не настроен срок пробного периода. Обратитесь к администратору."
        )
        return False
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    bypass = user_id in config.ADMIN_BYPASS_IDS
    await db.grant_trial_days(user_id, trial_days, now_ts, auto_renew_default, bypass)
    row = await db.get_user(user_id)
    if row and row["expires_at"]:
        dt = datetime.utcfromtimestamp(row["expires_at"])
        await message.answer(
            f"Промокод активирован! Подписка действует до: {dt} UTC."
        )
    else:
        await message.answer(
            "Промокод активирован. Выполните /start, если ещё не регистрировались."
        )
    return True


async def build_admin_summary(db: DB) -> str:
    """Сформировать текст сводки для администратора."""

    chat_username = await db.get_target_chat_username()
    chat_id = await db.get_target_chat_id()
    if chat_id is None:
        chat_info = "Чат пока не привязан"
    else:
        if not chat_username:
            chat_info = f"Чат привязан: id {chat_id}"
        else:
            chat_info = f"Чат привязан: {chat_username} (id {chat_id})"

    trial_days = await db.get_trial_days(config.TRIAL_DAYS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    auto_renew_text = "включено" if auto_renew_default else "выключено"
    prices = await db.get_prices(config.PRICES)
    if prices:
        price_lines = [
            f"{months} мес: {price}₽" for months, price in sorted(prices.items())
        ]
        price_text = "Прайс-лист:\n" + "\n".join(price_lines)
    else:
        price_text = "Прайс-лист пока пуст"

    lines = [
        chat_info,
        "",
        f"Пробный период: {trial_days} дн.",
        f"Автопродление по умолчанию: {auto_renew_text}",
        "",
        price_text,
        "",
        "Используйте кнопки ниже для управления настройками.",
    ]
    return "\n".join(lines)


async def send_admin_menu(m: Message, db: DB):
    """Отправить основное админское меню со сводкой."""

    summary = await build_admin_summary(db)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    kb = build_admin_keyboard(auto_renew_default)
    await m.answer(summary, reply_markup=kb)


def build_autorenew_keyboard(current_flag: bool) -> InlineKeyboardMarkup:
    """Построить inline-клавиатуру для переключения автопродления."""

    on_text = "✅ Автопродление включено" if current_flag else "Включить автопродление"
    off_text = "Выключить автопродление" if current_flag else "✅ Автопродление выключено"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=on_text, callback_data="user:autorenew:on")],
            [InlineKeyboardButton(text=off_text, callback_data="user:autorenew:off")],
        ]
    )


async def reply_to_target(target, text: str, **kwargs):
    """Ответить сообщением независимо от типа апдейта."""

    if isinstance(target, CallbackQuery):
        await target.message.answer(text, **kwargs)
        await target.answer()
    else:
        await target.answer(text, **kwargs)


async def send_subscription_status(target, db: DB):
    """Ответить пользователю текущим статусом подписки."""

    user_id = target.from_user.id
    row = await db.get_user(user_id)
    if not row:
        await reply_to_target(target, "Вы ещё не зарегистрированы. Нажмите /start.")
        return
    exp = row["expires_at"]
    dt = datetime.utcfromtimestamp(exp)
    ar = "вкл" if row["auto_renew"] else "выкл"
    po = "да" if row["paid_only"] else "нет"
    await reply_to_target(
        target,
        f"Подписка до: {dt} UTC\nАвтопродление: {ar}\nБез пробника: {po}",
    )


async def send_invite_link(target, bot: Bot, db: DB):
    """Создать и отправить одноразовую ссылку, если это возможно."""

    user_id = target.from_user.id
    row = await db.get_user(user_id)
    if not row:
        await reply_to_target(target, "Вы ещё не зарегистрированы. Нажмите /start.")
        return
    if row["expires_at"] < int(datetime.utcnow().timestamp()):
        await reply_to_target(target, "Подписка не активна. Оплатите, чтобы получить доступ.")
        return
    target_chat_id = await db.get_target_chat_id()
    if target_chat_id is None:
        await reply_to_target(target, "Чат ещё не привязан. Дождитесь действий администратора.")
        return
    try:
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(
            target_chat_id, member_limit=1, expire_date=expire_ts
        )
        await reply_to_target(
            target,
            "Ваша одноразовая ссылка в канал (действует 24 часа):\n" + link.invite_link,
        )
    except Exception:
        await reply_to_target(target, "Не удалось создать ссылку. Напишите админу.")


@router.message(CommandStart())
async def cmd_start(m: Message, db: DB):
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days(config.TRIAL_DAYS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    paid_only = (m.from_user.id in config.PAID_ONLY_IDS)
    bypass = (m.from_user.id in config.ADMIN_BYPASS_IDS)
    await db.upsert_user(
        m.from_user.id, now_ts, trial_days, auto_renew_default, paid_only, bypass
    )
    row = await db.get_user(m.from_user.id)
    auto_renew_flag = bool(row["auto_renew"]) if row else auto_renew_default

    warning_lines = [
        "⚠️ Подписка платная и продлевается автоматически по окончании оплаченного периода.",
        "Если вы не хотите автопродления, отключите его заранее через меню.",
        "Воспользуйтесь кнопками ниже, чтобы управлять доступом.",
    ]
    await m.answer("\n".join(warning_lines), reply_markup=USER_REPLY_KEYBOARD)
    user_menu = build_user_inline_keyboard(auto_renew_flag)
    await m.answer("Выберите действие:", reply_markup=user_menu)


@router.message(Command("use"))
async def cmd_use(m: Message, db: DB):
    args = (m.text or "").split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Формат: /use <промокод>.")
        return
    await apply_trial_promo(m, args[1], db)

@router.message(Command("status"))
async def cmd_status(m: Message, db: DB):
    await send_subscription_status(m, db)

@router.message(Command("rejoin"))
async def cmd_rejoin(m: Message, bot: Bot, db: DB):
    await send_invite_link(m, bot, db)

@router.message(F.text == "Получить ссылку")
async def user_get_link_button(m: Message, bot: Bot, db: DB):
    await send_invite_link(m, bot, db)


@router.message(F.text == "Статус подписки")
async def user_status_button(m: Message, db: DB):
    await send_subscription_status(m, db)


@router.message(F.text == "Продлить подписку")
async def user_buy_button(m: Message, db: DB):
    prices = await db.get_prices(config.PRICES)
    if not prices:
        await m.answer("Пока нет доступных вариантов продления. Обратитесь к администратору.")
        return
    kb = build_purchase_keyboard(prices)
    await m.answer("Выберите подходящий срок продления:", reply_markup=kb)


@router.message(F.text == "Настроить автопродление")
async def user_autorenew_menu_message(m: Message, db: DB):
    row = await db.get_user(m.from_user.id)
    if not row:
        await m.answer("Вы ещё не зарегистрированы. Нажмите /start.")
        return
    kb = build_autorenew_keyboard(bool(row["auto_renew"]))
    await m.answer("Выберите состояние автопродления:", reply_markup=kb)


@router.message(F.text == "Ввести промокод")
async def user_enter_promo_button(m: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_promo_code)
    await m.answer(
        "Пришлите промокод одним сообщением. Для отмены отправьте 'отмена'.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(UserStates.waiting_promo_code)
async def user_waiting_promo_code(m: Message, db: DB, state: FSMContext):
    text = (m.text or "").strip()
    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await m.answer(
            "Ввод промокода отменён.", reply_markup=USER_REPLY_KEYBOARD
        )
        return
    if not text:
        await m.answer(
            "Промокод не должен быть пустым. Попробуйте ещё раз или отправьте 'отмена'."
        )
        return
    success = await apply_trial_promo(m, text, db)
    if success:
        await state.clear()
        await m.answer(
            "Можете выбрать следующее действие из меню.",
            reply_markup=USER_REPLY_KEYBOARD,
        )
    else:
        await m.answer(
            "Попробуйте ещё раз или отправьте 'отмена'."
        )


def build_purchase_keyboard(prices: dict[int, int]) -> InlineKeyboardMarkup:
    """Построить клавиатуру с вариантами покупки по количеству месяцев."""

    buttons = []
    for months, _ in sorted(prices.items()):
        text = f"Купить {months} мес"
        callback_data = f"user:buy:{months}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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


@router.message(F.text == "Показать настройки")
async def admin_show_settings_button(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    summary = await build_admin_summary(db)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    kb = build_admin_keyboard(auto_renew_default)
    await m.answer(summary, reply_markup=kb)


@router.message(F.text == "Установить пробный период")
async def admin_set_trial_button(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.waiting_trial_days)
    await m.answer(
        "Пришлите количество дней пробного периода (целое число > 0). Для отмены отправьте 'отмена'.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.waiting_trial_days)
async def admin_set_trial_days_state(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip()
    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await m.answer("Установка пробного периода отменена.")
        await send_admin_menu(m, db)
        return
    if not text.isdigit():
        await m.answer("Нужно указать целое число дней. Попробуйте снова или отправьте 'отмена'.")
        return
    days = int(text)
    if days <= 0:
        await m.answer("Количество дней должно быть положительным. Попробуйте снова или отправьте 'отмена'.")
        return
    await db.set_trial_days(days)
    await state.clear()
    await m.answer(f"Пробный период обновлён: {days} дн.")
    await send_admin_menu(m, db)


def parse_prices_payload(text: str) -> Optional[dict[int, int]]:
    """Распарсить ввод прайса формата 1=990, 3=2700."""

    cleaned_text = text.replace("\n", " ")
    tokens = [token for token in re.split(r"[\s,;]+", cleaned_text) if token]
    if not tokens:
        return None
    prices: dict[int, int] = {}
    for token in tokens:
        if "=" not in token:
            return None
        left, right = token.split("=", 1)
        try:
            months = int(left)
            price = int(right)
        except ValueError:
            return None
        if months <= 0 or price <= 0:
            return None
        prices[months] = price
    return prices


@router.message(F.text == "Редактировать цены")
async def admin_edit_prices_button(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.waiting_prices)
    current_prices = await db.get_prices(config.PRICES)
    if current_prices:
        current_lines = [
            f"{months}={price}" for months, price in sorted(current_prices.items())
        ]
        current_text = ", ".join(current_lines)
    else:
        current_text = "не задан"
    await m.answer(
        "Пришлите пары вида '<месяцев>=<цена>' через пробел или запятую. Например: '1=990 3=2700'."
        " Для отмены отправьте 'отмена'."
        f"\nТекущий прайс: {current_text}",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.waiting_prices)
async def admin_edit_prices_state(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip()
    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await m.answer("Редактирование прайса отменено.")
        await send_admin_menu(m, db)
        return
    prices = parse_prices_payload(text)
    if not prices:
        await m.answer(
            "Не удалось разобрать ввод. Используйте формат '1=990 3=2700'."
            " Попробуйте снова или отправьте 'отмена'."
        )
        return
    await db.set_prices(prices)
    await state.clear()
    await m.answer("Прайс обновлён.")
    await send_admin_menu(m, db)


async def create_trial_codes_message(
    db: DB, count: int, trial_days: int, ttl_days: Optional[int]
) -> Tuple[bool, str]:
    """Создать промокоды и вернуть результат для ответа."""

    expires_at: Optional[int] = None
    if ttl_days is not None:
        expires_at = int((datetime.utcnow() + timedelta(days=ttl_days)).timestamp())

    codes: list[str] = []
    attempts = 0
    max_attempts = max(count * 5, 10)
    while len(codes) < count and attempts < max_attempts:
        attempts += 1
        code = generate_promo_code()
        try:
            await db.create_promo_code(
                code=code,
                code_type="trial",
                expires_at=expires_at,
                max_uses=1,
                per_user_limit=1,
                trial_days=trial_days,
            )
            codes.append(code)
        except ValueError:
            continue
    if not codes:
        return False, "Не удалось создать промокоды. Попробуйте ещё раз."
    lines = [f"Создано {len(codes)} промокодов на {trial_days} дн."]
    if len(codes) < count:
        lines.append("Не удалось получить полное количество. Попробуйте повторить операцию.")
    if expires_at:
        expire_dt = datetime.utcfromtimestamp(expires_at)
        lines.append(f"Коды действуют до {expire_dt} UTC.")
    lines.append("Коды:\n" + "\n".join(codes))
    return True, "\n\n".join(lines)


@router.message(F.text == "Сгенерировать промокоды (trial)")
async def admin_generate_trial_button(m: Message, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.waiting_trial_promo)
    await m.answer(
        "Пришлите параметры в формате '<кол-во> <дней_пробного> [срок_кода_в_днях]'."
        " Например: '5 7 30'. Для отмены отправьте 'отмена'.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.waiting_trial_promo)
async def admin_generate_trial_state(m: Message, state: FSMContext, db: DB):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip()
    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await m.answer("Создание промокодов отменено.")
        await send_admin_menu(m, db)
        return
    parts = text.split()
    if len(parts) < 2 or len(parts) > 3 or not all(part.isdigit() for part in parts):
        await m.answer(
            "Нужно указать два или три числа: '<кол-во> <дней_пробного> [срок_кода_в_днях]'."
            " Попробуйте снова или отправьте 'отмена'."
        )
        return
    count = int(parts[0])
    trial_days = int(parts[1])
    ttl_days = int(parts[2]) if len(parts) == 3 else None
    if count <= 0 or trial_days <= 0 or (ttl_days is not None and ttl_days <= 0):
        await m.answer(
            "Все параметры должны быть положительными числами. Попробуйте снова или отправьте 'отмена'."
        )
        return
    if count > 100:
        await m.answer("За один раз можно создать не более 100 кодов. Попробуйте снова.")
        return
    ok, message = await create_trial_codes_message(db, count, trial_days, ttl_days)
    await state.clear()
    await m.answer(message)
    await send_admin_menu(m, db)


@router.message(F.text.regexp(r"^Автопродление по умолчанию"))
async def admin_toggle_autorenew_default(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    current_flag = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    new_flag = not current_flag
    await db.set_auto_renew_default(new_flag)
    status_text = "включено" if new_flag else "выключено"
    await m.answer(f"Автопродление по умолчанию теперь {status_text}.")
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


@router.message(Command("generate_trial_codes"))
async def cmd_generate_trial_codes(m: Message, db: DB):
    if not is_super_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await m.answer(
            "Формат: /generate_trial_codes <кол-во> <дней_пробного> [срок_кода_в_днях]"
        )
        return
    count = int(args[1])
    trial_days = int(args[2])
    if count <= 0 or trial_days <= 0:
        await m.answer("Аргументы должны быть положительными.")
        return
    if count > 100:
        await m.answer("За один раз можно создать не более 100 кодов.")
        return
    ttl_days: Optional[int] = None
    if len(args) > 3:
        if not args[3].isdigit():
            await m.answer("Третий аргумент должен быть числом дней действия кода.")
            return
        ttl_days = int(args[3])
        if ttl_days <= 0:
            await m.answer("Срок действия кода должен быть положительным.")
            return
    ok, message = await create_trial_codes_message(db, count, trial_days, ttl_days)
    await m.answer(message)


# ==== Callback-кнопки ====

@callback_router.callback_query(F.data == "user:get_link")
async def user_get_link_callback(callback: CallbackQuery, bot: Bot, db: DB):
    await send_invite_link(callback, bot, db)


@callback_router.callback_query(F.data == "user:status")
async def user_status_callback(callback: CallbackQuery, db: DB):
    await send_subscription_status(callback, db)


@callback_router.callback_query(F.data == "user:buy")
async def user_buy_callback(callback: CallbackQuery, db: DB):
    prices = await db.get_prices(config.PRICES)
    if not prices:
        await callback.message.answer("Пока нет доступных вариантов продления. Обратитесь к администратору.")
        await callback.answer()
        return
    kb = build_purchase_keyboard(prices)
    await callback.message.answer("Выберите подходящий срок продления:", reply_markup=kb)
    await callback.answer()


@callback_router.callback_query(F.data.startswith("user:buy:"))
async def user_buy_months_callback(callback: CallbackQuery, db: DB):
    """Обработать выбор срока продления из инлайн-клавиатуры."""

    try:
        months = int(callback.data.split(":")[-1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    prices = await db.get_prices(config.PRICES)
    ok, msg = await process_payment(callback.from_user.id, months, prices)
    if not ok:
        await callback.message.answer("Оплата не прошла: " + msg)
        await callback.answer()
        return
    await db.extend_subscription(callback.from_user.id, months)
    await callback.message.answer(msg + "\nПодписка продлена.")
    await callback.answer()


@callback_router.callback_query(F.data == "user:autorenew_menu")
async def user_autorenew_menu_callback(callback: CallbackQuery, db: DB):
    row = await db.get_user(callback.from_user.id)
    if not row:
        await callback.answer("Сначала выполните /start.", show_alert=True)
        return
    kb = build_autorenew_keyboard(bool(row["auto_renew"]))
    await callback.message.answer("Выберите состояние автопродления:", reply_markup=kb)
    await callback.answer()


@callback_router.callback_query(F.data == "user:autorenew:toggle")
async def user_autorenew_toggle(callback: CallbackQuery, db: DB):
    """Переключить флаг автопродления из основного меню."""

    row = await db.get_user(callback.from_user.id)
    if not row:
        await callback.answer("Сначала выполните /start.", show_alert=True)
        return
    new_flag = not bool(row["auto_renew"])
    await db.set_auto_renew(callback.from_user.id, new_flag)
    await callback.message.edit_reply_markup(
        reply_markup=build_user_inline_keyboard(new_flag)
    )
    status_text = "Автопродление включено" if new_flag else "Автопродление выключено"
    await callback.answer(status_text)


@callback_router.callback_query(F.data == "user:autorenew:on")
async def user_autorenew_on(callback: CallbackQuery, db: DB):
    await db.set_auto_renew(callback.from_user.id, True)
    kb = build_autorenew_keyboard(True)
    await callback.message.edit_text("Автопродление включено.", reply_markup=kb)
    await callback.answer("Автопродление включено.")


@callback_router.callback_query(F.data == "user:autorenew:off")
async def user_autorenew_off(callback: CallbackQuery, db: DB):
    await db.set_auto_renew(callback.from_user.id, False)
    kb = build_autorenew_keyboard(False)
    await callback.message.edit_text("Автопродление выключено.", reply_markup=kb)
    await callback.answer("Автопродление выключено.")


@callback_router.callback_query()
async def handle_basic_callback(callback: CallbackQuery):
    """Базовый обработчик для callback-кнопок."""
    await callback.answer()


router.include_router(callback_router)
