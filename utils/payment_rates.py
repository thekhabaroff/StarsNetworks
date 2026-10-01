"""Синхронизация отображаемых курсов оплаты.

Все цены магазина и счета провайдеров остаются в рублях. USD/EUR здесь нужны
для понятного отображения и будущих валютных методов; Stars используется при
создании нового Telegram invoice как число Stars на рублёвую сумму.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from aiohttp import ClientSession, ClientTimeout

from database.db import async_session_maker
from utils.payments import save_payment_setting
from bot import settings

logger = logging.getLogger(__name__)

CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
HTTP_TIMEOUT = ClientTimeout(total=12)
# Установленный магазином курс: одна Telegram Star равна 0.015 USD.
TELEGRAM_STAR_USD_RATE = Decimal("0.015")


def _parse_cbr_rates(xml_text: str) -> dict[str, Decimal]:
    root = ET.fromstring(xml_text)
    rates: dict[str, Decimal] = {}
    for item in root.findall("Valute"):
        code = (item.findtext("CharCode") or "").upper()
        if code not in {"USD", "EUR"}:
            continue
        try:
            nominal = Decimal((item.findtext("Nominal") or "").replace(",", "."))
            value = Decimal((item.findtext("Value") or "").replace(",", "."))
        except InvalidOperation:
            continue
        if nominal > 0 and value > 0:
            rates[code] = value / nominal
    if {"USD", "EUR"} - rates.keys():
        raise ValueError("ЦБ не вернул курс USD и EUR")
    return rates


async def fetch_cbr_rates() -> dict[str, Decimal]:
    """Получить официальный ежедневный курс USD/EUR от Банка России."""
    async with ClientSession(timeout=HTTP_TIMEOUT) as client:
        async with client.get(CBR_DAILY_URL) as response:
            response.raise_for_status()
            return _parse_cbr_rates(await response.text())


async def synchronize_payment_rates(session) -> dict[str, Decimal]:
    """Сохранить USD/EUR ЦБ и рассчитанную цену Telegram Star в рублях."""
    rates = await fetch_cbr_rates()
    stars_rate = rates["USD"] * TELEGRAM_STAR_USD_RATE
    await save_payment_setting(session, "PAYMENT_USD_RATE", f"{rates['USD']:.6f}")
    await save_payment_setting(session, "PAYMENT_EUR_RATE", f"{rates['EUR']:.6f}")
    await save_payment_setting(session, "PAYMENT_STARS_RATE", f"{stars_rate:.6f}")
    await session.commit()
    return {**rates, "STARS": stars_rate}


async def sync_payment_rates_periodically() -> None:
    """Обновлять курсы ЦБ и Telegram Stars раз в шесть часов."""
    while True:
        try:
            if settings.PAYMENT_RATE_SYNC_ENABLED:
                async with async_session_maker() as session:
                    await synchronize_payment_rates(session)
                logger.info("Payment USD/EUR/Stars rates synchronized")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not synchronize payment rates with the Central Bank", exc_info=True)
        await asyncio.sleep(6 * 60 * 60)
