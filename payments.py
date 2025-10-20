from datetime import datetime

# Заглушка «оплата прошла»
async def process_payment(user_id: int, months: int, price_map: dict[int, int]) -> tuple[bool, str]:
    price = price_map.get(months)
    if not price:
        return False, "Неизвестный срок подписки"
    # Тут будет интеграция с платежкой. Пока просто «успех».
    return True, f"Оплата {price}₽ за {months} мес. прошла. (заглушка {datetime.utcnow().isoformat()})"
