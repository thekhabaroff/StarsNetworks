"""Правила скидки за количество.

Этот модуль возвращает только целые проценты. Денежный расчёт выполняется в
``utils.global_discount`` через ``Decimal``; здесь не должно быть float-API,
который можно случайно использовать для цены или баланса.
"""


def calculate_discount(quantity: int) -> int:
    """
    Расчет скидки в зависимости от количества
    Пример: 500 почт = -5%, 1000 почт = -10%
    """
    # Можно настроить через БД или конфиг
    discount_rules = {
        500: 5,
        1000: 10,
        2000: 15,
        5000: 20
    }

    # Находим максимальную скидку для данного количества
    max_discount = 0
    for threshold, discount in sorted(discount_rules.items(), reverse=True):
        if quantity >= threshold:
            max_discount = discount
            break

    return max_discount
