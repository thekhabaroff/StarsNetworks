"""Read-only live cost quotes from Fragment's public purchase pages.

The connection-token API used for delivery exposes purchase endpoints but no
reliable quote endpoint.  Fragment's public Stars/Premium pages do expose the
current customer-facing amount, so we use those amounts as the base cost and
add the provider API fee configured for the account.  No wallet operation is
performed here.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from decimal import Decimal, InvalidOperation

import aiohttp

from utils.money import to_money

logger = logging.getLogger(__name__)

STARS_PRICING_URL = "https://fragment.com/stars/buy"
PREMIUM_PRICING_URL = "https://fragment.com/premium/gift"
DEFAULT_API_FEE_PERCENT = Decimal("0.5")
DEFAULT_CACHE_SECONDS = 300
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


class FragmentPricingError(RuntimeError):
    """A safe error raised when a live Fragment quote cannot be obtained."""


class FragmentQuote:
    """A normalized quote before applying the store's markup."""

    __slots__ = ("base_usd", "api_fee_percent", "total_usd", "source")

    def __init__(
        self,
        *,
        base_usd: Decimal,
        api_fee_percent: Decimal,
        source: str,
    ) -> None:
        self.base_usd = base_usd
        self.api_fee_percent = api_fee_percent
        self.total_usd = base_usd * (Decimal("1") + api_fee_percent / Decimal("100"))
        self.source = source


_page_cache: dict[str, tuple[float, str]] = {}
_cache_lock = asyncio.Lock()


def _setting_decimal(name: str, default: Decimal) -> Decimal:
    """Read a Decimal setting without allowing invalid env values to break pricing."""
    try:
        from bot import settings

        value = Decimal(str(getattr(settings, name, default)))
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not value.is_finite() or value < Decimal("0"):
        return default
    return value


def _cache_seconds() -> int:
    try:
        from bot import settings

        value = int(getattr(settings, "FRAGMENT_PRICING_CACHE_SECONDS", DEFAULT_CACHE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS
    return max(30, min(value, 3600))


async def _page(url: str) -> str:
    now = time.monotonic()
    async with _cache_lock:
        cached = _page_cache.get(url)
        if cached and now - cached[0] < _cache_seconds():
            return cached[1]

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as client:
            async with client.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "StarsNetworks/1.0 pricing-check",
                },
            ) as response:
                if response.status >= 400:
                    raise FragmentPricingError(f"Fragment pricing HTTP {response.status}")
                body = await response.text()
    except asyncio.TimeoutError as exc:
        raise FragmentPricingError("Fragment pricing request timed out") from exc
    except aiohttp.ClientError as exc:
        raise FragmentPricingError(f"Fragment pricing network error: {exc}") from exc

    async with _cache_lock:
        _page_cache[url] = (time.monotonic(), body)
    return body


def _amount(value: str) -> Decimal:
    try:
        result = Decimal(value.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise FragmentPricingError("Fragment returned an invalid price") from None
    if not result.is_finite() or result <= Decimal("0"):
        raise FragmentPricingError("Fragment returned an invalid price")
    return result


def _input_blocks(page: str, field: str) -> list[tuple[str, str]]:
    """Return (option value, nearby HTML) for a Fragment radio group."""
    matches = list(re.finditer(
        rf'<input(?=[^>]*\bname=["\']{re.escape(field)}["\'])'
        rf'(?=[^>]*\bvalue=["\'](\d+)["\'])[^>]*>',
        page,
        flags=re.IGNORECASE,
    ))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page), match.end() + 2500)
        value_match = re.search(r'\bvalue=["\'](\d+)["\']', match.group(0), flags=re.IGNORECASE)
        if value_match:
            blocks.append((value_match.group(1), page[match.end():end]))
    return blocks


def _stars_unit_usd(page: str) -> Decimal:
    options: list[tuple[int, Decimal]] = []
    for raw_stars, block in _input_blocks(page, "stars"):
        stars = int(raw_stars)
        match = re.search(
            r'class=["\'][^"\']*icon-usd[^"\']*["\'][^>]*>\s*([0-9]+(?:[.,][0-9]+)?)',
            block,
            flags=re.IGNORECASE,
        )
        if match and stars > 0:
            options.append((stars, _amount(match.group(1))))
    if not options:
        raise FragmentPricingError("Fragment Stars price was not found")
    # Fragment currently publishes a linear per-Star price. Prefer 100 Stars
    # when present, otherwise use the largest published package to avoid
    # rounding a small option disproportionately.
    stars, price = next(((s, p) for s, p in options if s == 100), max(options))
    return price / Decimal(stars)


def _premium_usd(page: str, months: int) -> Decimal:
    for raw_months, block in _input_blocks(page, "months"):
        if int(raw_months) != months:
            continue
        match = re.search(r'(?:&#036;|\$)\s*([0-9]+(?:[.,][0-9]+)?)', html_lib.unescape(block))
        if match:
            return _amount(match.group(1))
    raise FragmentPricingError(f"Fragment Premium price for {months} months was not found")


async def quote_for_product(product) -> FragmentQuote:
    """Fetch a current USD cost for one catalog unit."""
    delivery_type = getattr(product, "delivery_type", "")
    if delivery_type == "telegram_stars":
        base_usd = _stars_unit_usd(await _page(STARS_PRICING_URL))
        source = "fragment.com/stars/buy"
    elif delivery_type == "telegram_premium":
        months = int(getattr(product, "premium_months", 0) or 0)
        if months not in {3, 6, 12}:
            raise FragmentPricingError("Unsupported Premium duration")
        base_usd = _premium_usd(await _page(PREMIUM_PRICING_URL), months)
        source = f"fragment.com/premium/gift ({months}m)"
    else:
        raise FragmentPricingError("Automatic Fragment pricing is only available for Stars/Premium")

    api_fee = _setting_decimal("FRAGMENT_API_FEE_PERCENT", DEFAULT_API_FEE_PERCENT)
    return FragmentQuote(base_usd=base_usd, api_fee_percent=api_fee, source=source)


async def quote_cost_rubles(product) -> tuple[Decimal, FragmentQuote]:
    """Return ``(cost in RUB, quote metadata)`` for a product."""
    quote = await quote_for_product(product)
    usd_rate = _setting_decimal("PAYMENT_USD_RATE", Decimal("0"))
    if usd_rate <= Decimal("0"):
        # The payment-rate task may not have run yet after a fresh deployment.
        # Fetching CBR here is still read-only and avoids pricing a product at 0.
        try:
            from utils.payment_rates import fetch_cbr_rates

            usd_rate = (await fetch_cbr_rates())["USD"]
        except Exception as exc:
            raise FragmentPricingError("Курс USD/RUB пока недоступен") from exc
    return to_money(quote.total_usd * usd_rate, minimum=Decimal("0.01")), quote
