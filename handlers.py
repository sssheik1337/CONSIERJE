from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from datetime import datetime, timedelta
import re
import secrets
from config import config
from db import DB

router = Router(name="core")


class AdminStates(StatesGroup):
    """FSM-состояния для диалогов администратора."""

    editing_prices = State()
    setting_trial_days = State()
    generating_promos = State()
    setting_auto_renew_default = State()


ADMIN_MENU_BUTTONS = {
    "edit_prices": "Редактировать цены",
    "set_trial": "Установить пробный период",
    "generate_promos": "Сгенерировать промокоды",
    "auto_renew": "Автопродление по умолчанию",
    "cancel": "Отмена",
}


def is_super_admin(uid: int) -> bool:
    return uid in config.SUPER_ADMIN_IDS


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """Формирует клавиатуру для админ-панели."""

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=ADMIN_MENU_BUTTONS["edit_prices"]),
                KeyboardButton(text=ADMIN_MENU_BUTTONS["set_trial"]),
            ],
            [
                KeyboardButton(text=ADMIN_MENU_BUTTONS["generate_promos"]),
                KeyboardButton(text=ADMIN_MENU_BUTTONS["auto_renew"]),
            ],
            [KeyboardButton(text=ADMIN_MENU_BUTTONS["cancel"])],
        ],
        resize_keyboard=True,
    )


def format_prices(prices: dict[int, int]) -> str:
    """Форматирует цены для отображения."""

    parts = [f"{months} мес: {price}₽" for months, price in sorted(prices.items())]
    return "\n".join(parts)


def parse_prices(text: str) -> dict[int, int] | None:
    """Пытается распарсить пары месяц:цена из текста."""

    cleaned = re.split(r"[\n,]+", text)
    result: dict[int, int] = {}
    for chunk in cleaned:
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            return None
        left, right = chunk.split(":", 1)
        left = left.strip()
        right = right.strip()
        if not left.isdigit() or not right.isdigit():
            return None
        months = int(left)
        price = int(right)
        if months <= 0 or price <= 0:
            return None
        result[months] = price
    return result if result else None


def generate_promo_code(length: int = 10) -> str:
    """Создаёт человекочитаемый промокод."""

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, db: DB):
    now_ts = int(datetime.utcnow().timestamp())
    trial_days = await db.get_trial_days_global(config.TRIAL_DAYS)
    auto_renew_default = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    paid_only = m.from_user.id in config.PAID_ONLY_IDS
    bypass = m.from_user.id in config.ADMIN_BYPASS_IDS
    await db.upsert_user(m.from_user.id, now_ts, trial_days, auto_renew_default, paid_only, bypass)

    # Добавить в канал, если не состоит
    try:
        # Индивидуальная одноразовая ссылка (24 часа)
        expire_ts = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        link = await bot.create_chat_invite_link(
            config.TARGET_CHAT_ID,
            member_limit=1,
            expire_date=expire_ts,
        )
        await m.answer(
            "Добро пожаловать! Вот ваша одноразовая ссылка в канал (действует 24 часа):\n"
            + link.invite_link
        )
    except Exception:
        await m.answer("Не удалось создать ссылку. Напишите админу.")

    if is_super_admin(m.from_user.id):
        prices = await db.get_prices(config.PRICES)
        auto_text = "включено" if auto_renew_default else "выключено"
        await m.answer(
            "Панель администратора:\n"
            f"Текущий пробный период: {trial_days} дн.\n"
            f"Автопродление по умолчанию: {auto_text}.\n"
            "Текущие цены:\n"
            + format_prices(prices),
            reply_markup=admin_menu_keyboard(),
        )


@router.message(Command("use"))
async def cmd_use(m: Message, bot: Bot, db: DB):
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


@router.message(F.text == ADMIN_MENU_BUTTONS["cancel"])
async def admin_cancel(m: Message, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("Действие отменено.", reply_markup=admin_menu_keyboard())


@router.message(F.text == ADMIN_MENU_BUTTONS["edit_prices"])
async def admin_edit_prices(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    prices = await db.get_prices(config.PRICES)
    await state.set_state(AdminStates.editing_prices)
    await m.answer(
        "Текущие цены:\n"
        + format_prices(prices)
        + "\n\nОтправьте новые значения в формате «месяцев:цена»."
          " Можно несколько пар через запятую или перенос строки.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.editing_prices)
async def admin_save_prices(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    parsed = parse_prices(m.text or "")
    if not parsed:
        await m.answer(
            "Не удалось разобрать данные. Используйте формат «1:399, 3:999»."
        )
        return
    await db.set_prices(parsed)
    await state.clear()
    await m.answer(
        "Цены обновлены:\n" + format_prices(parsed),
        reply_markup=admin_menu_keyboard(),
    )


@router.message(F.text == ADMIN_MENU_BUTTONS["set_trial"])
async def admin_set_trial_start(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    current = await db.get_trial_days_global(config.TRIAL_DAYS)
    await state.set_state(AdminStates.setting_trial_days)
    await m.answer(
        f"Сейчас пробный период: {current} дн. Введите новое значение (целое число).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.setting_trial_days)
async def admin_save_trial(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip()
    if not text.isdigit():
        await m.answer("Нужно целое положительное число.")
        return
    days = int(text)
    if days <= 0:
        await m.answer("Число должно быть больше нуля.")
        return
    await db.set_trial_days_global(days)
    await state.clear()
    await m.answer(
        f"Пробный период обновлён: {days} дн.",
        reply_markup=admin_menu_keyboard(),
    )


@router.message(F.text == ADMIN_MENU_BUTTONS["generate_promos"])
async def admin_generate_promos_start(m: Message, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.generating_promos)
    await m.answer(
        "Введите параметры генерации в формате «количество [месяцев]»."
        " Пример: 5 1",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminStates.generating_promos)
async def admin_generate_promos_finish(m: Message, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    parts = (m.text or "").strip().split()
    if not parts or not parts[0].isdigit():
        await m.answer("Нужно указать количество промокодов. Например: 3 1")
        return
    count = int(parts[0])
    if count <= 0 or count > 100:
        await m.answer("Количество должно быть от 1 до 100.")
        return
    months = 1
    if len(parts) > 1:
        if not parts[1].isdigit():
            await m.answer("Второй параметр — количество месяцев (целое число).")
            return
        months = int(parts[1])
        if months <= 0:
            await m.answer("Количество месяцев должно быть больше нуля.")
            return
    codes = [generate_promo_code() for _ in range(count)]
    await state.clear()
    formatted = "\n".join(f"{code} — {months} мес" for code in codes)
    await m.answer(
        "Сгенерированные промокоды:\n" + formatted,
        reply_markup=admin_menu_keyboard(),
    )


@router.message(F.text == ADMIN_MENU_BUTTONS["auto_renew"])
async def admin_auto_renew_start(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        return
    current = await db.get_auto_renew_default(config.AUTO_RENEW_DEFAULT)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Включить"), KeyboardButton(text="Выключить")],
            [KeyboardButton(text=ADMIN_MENU_BUTTONS["cancel"])],
        ],
        resize_keyboard=True,
    )
    status = "включено" if current else "выключено"
    await state.set_state(AdminStates.setting_auto_renew_default)
    await m.answer(
        f"Сейчас автопродление по умолчанию {status}. Выберите новое значение.",
        reply_markup=keyboard,
    )


@router.message(AdminStates.setting_auto_renew_default)
async def admin_auto_renew_finish(m: Message, db: DB, state: FSMContext):
    if not is_super_admin(m.from_user.id):
        await state.clear()
        return
    text = (m.text or "").strip().lower()
    if text in {"включить", "вкл", "on"}:
        flag = True
    elif text in {"выключить", "выкл", "off"}:
        flag = False
    else:
        await m.answer("Нужно выбрать «Включить» или «Выключить».")
        return
    await db.set_auto_renew_default(flag)
    await state.clear()
    status = "включено" if flag else "выключено"
    await m.answer(
        f"Автопродление по умолчанию теперь {status}.",
        reply_markup=admin_menu_keyboard(),
    )
