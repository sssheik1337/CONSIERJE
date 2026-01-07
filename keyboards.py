from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def build_payment_method_keyboard() -> InlineKeyboardMarkup:
    """Построить клавиатуру выбора способа оплаты."""

    builder = InlineKeyboardBuilder()
    builder.button(text="Оплатить через СБП", callback_data="buy:open:sbp")
    builder.button(text="Оплатить картой", callback_data="buy:open:card")
    builder.adjust(2)
    return builder.as_markup()
