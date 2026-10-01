"""Клиенты платежных провайдеров.

Модуль не меняет заказы и баланс: он только создаёт счета и подтверждает
полученные от провайдеров данные. Финансовая логика находится в
``services.checkout`` и выполняется в одной транзакции БД.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlencode

from aiohttp import ClientSession, ClientTimeout
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import settings
from database.models import Setting
from utils.money import money

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = ClientTimeout(total=20)
LAVA_INVOICE_LIFETIME_MINUTES = 15
YOOMONEY_QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm"

# Один порядок используется и для оплаты заказа, и для пополнения баланса.
# Методы могут оставаться в списке, когда выключены: после включения они
# вернутся ровно на выбранную владельцем позицию.
PAYMENT_METHODS = ("stars", "yookassa", "yoomoney", "lava", "heleket", "cryptobot")
DEFAULT_PAYMENT_METHOD_ORDER = ",".join(PAYMENT_METHODS)
PAYMENT_METHOD_TITLES = {
    "stars": "⭐ Telegram Stars",
    "yookassa": "💳 ЮKassa",
    "yoomoney": "💳 ЮMoney",
    "lava": "💳 Lava",
    "heleket": "₿ Heleket",
    "cryptobot": "🤖 CryptoBot",
}

# Пользовательский уровень выбора оплаты.  Конкретные провайдеры остаются
# внутренней деталью до тех пор, пока в одной категории не окажется больше
# одного подключённого способа.
PAYMENT_CATEGORY_ORDER = ("stars", "cards", "crypto")
PAYMENT_CATEGORIES = {
    "stars": {
        "title": "⭐ Telegram Stars",
        "methods": ("stars",),
    },
    "cards": {
        "title": "💳 Банковские карты",
        "methods": ("yookassa", "yoomoney", "lava"),
    },
    "crypto": {
        "title": "₿ Криптовалюта",
        "methods": ("heleket", "cryptobot"),
    },
}
PAYMENT_ENABLED_FIELDS = frozenset({
    "PAYMENT_YOOMONEY_ENABLED",
    "PAYMENT_YOOKASSA_ENABLED",
    "PAYMENT_HELEKET_ENABLED",
    "PAYMENT_LAVA_ENABLED",
    "PAYMENT_CRYPTOBOT_ENABLED",
    "PAYMENT_STARS_ENABLED",
})
PAYMENT_BOOLEAN_FIELDS = {
    "PAYMENT_RATE_SYNC_ENABLED",
}
# Название оставлено только для совместимости импорта: это точные Decimal
# значения, а не бинарные float.
PAYMENT_DECIMAL_FIELDS = {
    "PAYMENT_USD_RATE",
    "PAYMENT_EUR_RATE",
    "PAYMENT_STARS_RATE",
}


def get_payment_method_order() -> tuple[str, ...]:
    """Вернуть полный корректный порядок способов оплаты.

    Повреждённое или устаревшее значение настройки не должно скрывать метод:
    неизвестные элементы игнорируются, отсутствующие дописываются в конце.
    """
    raw = str(getattr(settings, "PAYMENT_METHOD_ORDER", "") or "")
    order: list[str] = []
    for method in raw.split(","):
        method = method.strip().lower()
        if method in PAYMENT_METHODS and method not in order:
            order.append(method)
    order.extend(method for method in PAYMENT_METHODS if method not in order)
    return tuple(order)


def get_payment_category_title(category: str) -> str:
    """Вернуть безопасное отображаемое название категории оплаты."""
    return str(PAYMENT_CATEGORIES.get(category, {}).get("title", "Способ оплаты"))


def get_enabled_category_methods(category: str) -> tuple[str, ...]:
    """Вернуть включённые методы категории в настроенном порядке.

    Благодаря этому порядок, заданный в разделе «Позиционирование», сохраняется
    в списке провайдеров внутри категории.
    """
    category_methods = set(PAYMENT_CATEGORIES.get(category, {}).get("methods", ()))
    return tuple(
        method
        for method in get_payment_method_order()
        if method in category_methods and PaymentService.provider_enabled(method)
    )


def get_available_payment_categories() -> tuple[str, ...]:
    """Показать только категории, в которых есть хотя бы один способ оплаты."""
    return tuple(
        category
        for category in PAYMENT_CATEGORY_ORDER
        if get_enabled_category_methods(category)
    )


def stars_amount_for_rubles(rubles: Decimal | float | int | str) -> int:
    """Рассчитать количество Stars по сохранённому курсу рубль/Star."""
    try:
        rate = Decimal(str(settings.PAYMENT_STARS_RATE))
        amount = Decimal(str(rubles))
    except (InvalidOperation, ValueError):
        rate = Decimal("2.3")
        amount = Decimal("0")
    if not rate.is_finite() or rate <= 0:
        rate = Decimal("2.3")
    return max(1, int((amount / rate).to_integral_value(rounding=ROUND_UP)))


@dataclass(frozen=True)
class ProviderInvoice:
    payment_id: str
    payment_url: str


@dataclass(frozen=True)
class ProviderInvoiceStatus:
    """Проверенное состояние внешнего счёта.

    Сумма и валюта берутся только из API провайдера. Это позволяет фоновой
    сверке безопасно завершить уже оплаченный счёт, если callback был потерян,
    не доверяя данным старого локального URL или входящему webhook.
    """

    state: str
    payment_id: str | None = None
    payment_url: str | None = None
    amount: str | None = None
    currency: str | None = None


class ProviderCreationError(RuntimeError):
    """Ошибка создания счёта с признаком, можно ли безопасно его закрыть.

    ``definitive`` означает, что платёжный провайдер явно отклонил запрос до
    создания счёта. Для таймаутов, 5xx и повреждённых ответов это значение
    всегда ``False``: запрос мог быть принят, поэтому локальный платёж сначала
    нужно сверить по его стабильному ``external_order_id``.
    """

    def __init__(self, message: str, *, definitive: bool) -> None:
        super().__init__(message)
        self.definitive = definitive


def _definitive_client_error(status: int) -> bool:
    """Вернуть True только для однозначного отказа до создания счёта."""
    return 400 <= status < 500 and status not in {408, 409, 425, 429}


class PaymentService:
    """Серверная интеграция с внешними платежными системами."""

    # Создание одного счёта нельзя выполнять параллельно в одном воркере.
    # Блокировка не удерживает транзакцию БД и удаляется, когда последний
    # ожидающий вызов закончил работу. Идентификаторы YooKassa/Heleket/Lava
    # дополнительно защищены стабильным external_order_id на стороне API.
    _invoice_locks: dict[str, tuple[asyncio.Lock, int]] = {}
    _invoice_locks_guard = asyncio.Lock()

    @staticmethod
    @asynccontextmanager
    async def invoice_creation_lock(external_order_id: str) -> AsyncIterator[None]:
        """Сериализовать remote create/reconcile одного локального платежа."""
        key = str(external_order_id)
        async with PaymentService._invoice_locks_guard:
            lock, waiters = PaymentService._invoice_locks.get(
                key, (asyncio.Lock(), 0)
            )
            PaymentService._invoice_locks[key] = (lock, waiters + 1)
        try:
            async with lock:
                yield
        finally:
            async with PaymentService._invoice_locks_guard:
                current_lock, waiters = PaymentService._invoice_locks.get(
                    key, (lock, 1)
                )
                if current_lock is lock and waiters <= 1:
                    PaymentService._invoice_locks.pop(key, None)
                elif current_lock is lock:
                    PaymentService._invoice_locks[key] = (lock, waiters - 1)

    @staticmethod
    def cryptobot_api_token() -> str:
        """Return the configured Crypto Pay token, supporting both names."""
        return str(
            getattr(settings, "CRYPTOBOT_API", "")
            or getattr(settings, "CRYPTOBOT_API_TOKEN", "")
            or ""
        ).strip()

    @staticmethod
    def provider_configured(provider: str) -> bool:
        """Проверить реквизиты, необходимые для callback/status старого счёта.

        В отличие от :meth:`provider_enabled`, этот метод намеренно не смотрит
        на флаг включения метода и public URL. Это даёт оплате, выпущенной до
        отключения метода в панели, корректно завершиться по webhook.
        """
        if provider == "yookassa":
            return bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)
        if provider == "yoomoney":
            return bool(settings.YOOMONEY_WALLET and settings.YOOMONEY_NOTIFICATION_SECRET)
        if provider == "heleket":
            return bool(settings.HELEKET_API_KEY and settings.HELEKET_MERCHANT_ID)
        if provider == "lava":
            return bool(settings.LAVA_ID and settings.LAVA_SECRET_KEY)
        if provider == "cryptobot":
            return bool(PaymentService.cryptobot_api_token())
        if provider == "stars":
            return True
        return False

    @staticmethod
    def provider_invoice_lifetime_minutes(provider: str) -> int:
        """Return the actual lifetime requested from a provider's API."""
        configured = max(1, int(settings.ORDER_RESERVATION_MINUTES))
        if provider == "lava":
            return LAVA_INVOICE_LIFETIME_MINUTES
        if provider == "heleket":
            return max(5, configured)
        return configured

    @staticmethod
    def provider_enabled(provider: str) -> bool:
        if provider == "stars":
            return bool(settings.PAYMENT_STARS_ENABLED)
        if provider == "yookassa":
            return bool(
                settings.PAYMENT_YOOKASSA_ENABLED
                and PaymentService.provider_configured(provider)
                and settings.PAYMENT_PUBLIC_BASE_URL
            )
        if provider == "yoomoney":
            return bool(
                settings.PAYMENT_YOOMONEY_ENABLED
                and PaymentService.provider_configured(provider)
                and settings.PAYMENT_PUBLIC_BASE_URL
            )
        if provider == "heleket":
            return bool(
                settings.PAYMENT_HELEKET_ENABLED
                and PaymentService.provider_configured(provider)
                and settings.PAYMENT_PUBLIC_BASE_URL
            )
        if provider == "lava":
            return bool(
                settings.PAYMENT_LAVA_ENABLED
                and PaymentService.provider_configured(provider)
                and PaymentService._lava_webhook_secrets()
                and settings.PAYMENT_PUBLIC_BASE_URL
            )
        if provider == "cryptobot":
            return bool(
                settings.PAYMENT_CRYPTOBOT_ENABLED
                and PaymentService.provider_configured(provider)
            )
        return False

    @staticmethod
    def _lava_webhook_secrets() -> tuple[str, ...]:
        """Ключи для проверки webhook Lava.

        В актуальном API Lava для подписи webhook используется дополнительный
        ключ. ``LAVA_WEBHOOK_SECRET`` сохранён как совместимый резервный ключ
        для ранее настроенных/legacy webhook.
        """
        return tuple(dict.fromkeys(
            secret.strip()
            for secret in (settings.LAVA_ADDITIONAL_KEY, settings.LAVA_WEBHOOK_SECRET)
            if secret and secret.strip()
        ))

    @staticmethod
    async def _cryptobot_call(
        method: str,
        payload: dict[str, Any],
        *,
        creation: bool = False,
    ) -> Optional[dict[str, Any]]:
        token = PaymentService.cryptobot_api_token()
        if not token:
            if creation:
                raise ProviderCreationError(
                    "CryptoBot is not configured", definitive=True
                )
            return None
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(
                    f"https://pay.crypt.bot/api/{method}", json=payload,
                    headers={"Crypto-Pay-API-Token": token},
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or not data.get("ok"):
                        logger.error("CryptoBot %s failed: status=%s", method, response.status)
                        if creation:
                            raise ProviderCreationError(
                                "CryptoBot rejected invoice creation",
                                definitive=_definitive_client_error(response.status),
                            )
                        return None
                    result = data.get("result")
                    if isinstance(result, dict):
                        return result
                    if creation:
                        raise ProviderCreationError(
                            "CryptoBot returned an incomplete invoice response",
                            definitive=False,
                        )
                    return None
        except ProviderCreationError:
            raise
        except Exception:
            logger.exception("CryptoBot %s failed", method)
            if creation:
                raise ProviderCreationError(
                    "CryptoBot invoice creation outcome is unknown", definitive=False
                )
            return None

    @staticmethod
    async def create_cryptobot_payment(*, amount: Decimal, external_order_id: str, description: str) -> Optional[ProviderInvoice]:
        if not PaymentService.provider_enabled("cryptobot"):
            raise ProviderCreationError("CryptoBot is disabled", definitive=True)
        result = await PaymentService._cryptobot_call("createInvoice", {
            "currency_type": "fiat", "fiat": "RUB", "amount": f"{amount:.2f}",
            "description": description[:1024], "payload": external_order_id,
            "expires_in": (
                PaymentService.provider_invoice_lifetime_minutes("cryptobot") * 60
            ),
        }, creation=True)
        if not result:
            raise ProviderCreationError(
                "CryptoBot returned no invoice", definitive=False
            )
        invoice_id = result.get("invoice_id")
        payment_url = result.get("bot_invoice_url") or result.get("pay_url")
        if not invoice_id or not payment_url:
            raise ProviderCreationError(
                "CryptoBot response did not contain invoice id or URL",
                definitive=False,
            )
        return ProviderInvoice(str(invoice_id), str(payment_url))

    @staticmethod
    async def get_cryptobot_invoice(invoice_id: str) -> Optional[dict[str, Any]]:
        result = await PaymentService._cryptobot_call("getInvoices", {"invoice_ids": str(invoice_id)})
        items = result.get("items") if result else None
        return items[0] if isinstance(items, list) and items else None

    @staticmethod
    async def find_cryptobot_invoice_by_payload(
        external_order_id: str,
    ) -> Optional[dict[str, Any]]:
        """Найти счёт CryptoBot, если createInvoice успел пройти без ответа.

        У Crypto Pay нет idempotence key на createInvoice. Поэтому после
        таймаута мы ищем счёт по сохранённому payload, прежде чем допустить
        повторное создание. Если API вернул несколько старых совпадений, не
        выбираем один наугад: это требует ручной сверки и не должно привести к
        зачислению неверного счёта.
        """
        matches: dict[str, dict[str, Any]] = {}
        for status in ("active", "paid", "expired"):
            result = await PaymentService._cryptobot_call(
                "getInvoices", {"status": status, "count": 100}
            )
            items = result.get("items") if result else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("payload") or "") != external_order_id:
                    continue
                invoice_id = str(item.get("invoice_id") or "")
                if invoice_id:
                    matches[invoice_id] = item
        if len(matches) != 1:
            if len(matches) > 1:
                logger.error(
                    "CryptoBot has multiple invoices for external order %s",
                    external_order_id,
                )
            return None
        return next(iter(matches.values()))

    @staticmethod
    async def create_yoomoney_payment(
        *,
        amount: Decimal,
        external_order_id: str,
        description: str,
    ) -> Optional[ProviderInvoice]:
        """Build a ЮMoney QuickPay URL bound to the local payment label.

        QuickPay does not require a server-side create call: the customer is
        redirected to ЮMoney and the wallet later sends a signed HTTP
        notification containing the same ``label``. No payment is accepted
        until that notification passes HMAC and amount validation.
        """
        if not PaymentService.provider_enabled("yoomoney"):
            raise ProviderCreationError("ЮMoney is disabled", definitive=True)
        wallet = str(settings.YOOMONEY_WALLET or "").strip()
        if not wallet:
            raise ProviderCreationError("ЮMoney wallet is not configured", definitive=True)
        params = {
            "receiver": wallet,
            "quickpay-form": "button",
            "targets": description[:200],
            "sum": f"{money(amount):.2f}",
            "label": external_order_id,
            "successURL": PaymentService._return_url(),
        }
        payment_url = f"{YOOMONEY_QUICKPAY_URL}?{urlencode(params)}"
        # The stable local external ID is used as the provider binding until
        # ЮMoney supplies operation_id in its signed notification.
        return ProviderInvoice(payment_id=external_order_id, payment_url=payment_url)

    @staticmethod
    def verify_cryptobot_webhook(raw_body: bytes, signature: str) -> bool:
        token = PaymentService.cryptobot_api_token()
        if not token:
            return False
        secret = hashlib.sha256(token.encode()).digest()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature)

    @staticmethod
    def verify_yoomoney_webhook(data: dict[str, Any]) -> bool:
        """Проверить HMAC-SHA256 HTTP-уведомления ЮMoney.

        ЮMoney подписывает URL-кодированную строку всех параметров в
        алфавитном порядке, кроме самого ``sign``. Важно сохранять пустые
        значения: они входят в подписываемую строку уведомления.
        """
        secret = str(getattr(settings, "YOOMONEY_NOTIFICATION_SECRET", "") or "").strip()
        received = str(data.get("sign") or "").strip().lower()
        if not secret or not received:
            return False
        canonical = urlencode(
            sorted(
                (str(key), "" if value is None else str(value))
                for key, value in data.items()
                if str(key) != "sign"
            ),
            quote_via=quote,
        )
        expected = hmac.new(
            secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, received)

    @staticmethod
    def _public_url(path: str) -> str:
        base_url = settings.PAYMENT_PUBLIC_BASE_URL.rstrip("/")
        if not base_url:
            raise ValueError("PAYMENT_PUBLIC_BASE_URL is not configured")
        return f"{base_url}{path}"

    @staticmethod
    def _return_url() -> str:
        return f"https://t.me/{settings.BOT_USERNAME}"

    @staticmethod
    async def create_yookassa_payment(
        *,
        amount: Decimal,
        payment_record_id: int,
        external_order_id: str,
        description: str,
    ) -> Optional[ProviderInvoice]:
        """Создать идемпотентный платеж ЮKassa.

        ``external_order_id`` используется как Idempotence-Key, поэтому
        повторный клик/сетевой retry не создаёт второй счёт.
        """
        if not PaymentService.provider_enabled("yookassa"):
            raise ProviderCreationError("YooKassa is disabled", definitive=True)

        auth = base64.b64encode(
            f"{settings.YOOKASSA_SHOP_ID}:{settings.YOOKASSA_SECRET_KEY}".encode()
        ).decode()
        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": PaymentService._return_url()},
            "description": description,
            "metadata": {
                "payment_record_id": str(payment_record_id),
                "external_order_id": external_order_id,
            },
        }
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Idempotence-Key": external_order_id,
        }
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(
                    "https://api.yookassa.ru/v3/payments",
                    json=payload,
                    headers=headers,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200:
                        logger.error("YooKassa payment creation failed: status=%s", response.status)
                        raise ProviderCreationError(
                            "YooKassa rejected invoice creation",
                            definitive=_definitive_client_error(response.status),
                        )
        except ProviderCreationError:
            raise
        except Exception:
            logger.exception("YooKassa payment creation failed")
            raise ProviderCreationError(
                "YooKassa invoice creation outcome is unknown", definitive=False
            )

        if not isinstance(data, dict):
            raise ProviderCreationError(
                "YooKassa returned an invalid invoice response", definitive=False
            )
        payment_id = data.get("id")
        confirmation = data.get("confirmation") or {}
        payment_url = (
            confirmation.get("confirmation_url")
            if isinstance(confirmation, dict)
            else None
        )
        if not payment_id or not payment_url:
            logger.error("YooKassa response did not contain payment id or confirmation URL")
            raise ProviderCreationError(
                "YooKassa response did not contain payment id or URL",
                definitive=False,
            )
        return ProviderInvoice(payment_id=str(payment_id), payment_url=str(payment_url))

    @staticmethod
    async def get_yookassa_payment_status(payment_id: str) -> Optional[dict[str, Any]]:
        if not PaymentService.provider_configured("yookassa"):
            return None
        auth = base64.b64encode(
            f"{settings.YOOKASSA_SHOP_ID}:{settings.YOOKASSA_SECRET_KEY}".encode()
        ).decode()
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(
                    f"https://api.yookassa.ru/v3/payments/{payment_id}",
                    headers={"Authorization": f"Basic {auth}"},
                ) as response:
                    if response.status != 200:
                        logger.error("YooKassa status lookup failed: status=%s", response.status)
                        return None
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else None
        except Exception:
            logger.exception("YooKassa payment status lookup failed")
            return None

    @staticmethod
    def _heleket_payload_bytes(payload: dict[str, Any]) -> bytes:
        """Return JSON in the canonical form expected by Heleket's PHP API.

        Heleket calculates its MD5 over ``base64(json_encode(payload))``.  PHP's
        default ``json_encode`` escapes non-ASCII characters and forward slashes,
        so using a pretty UTF-8 Python serialization makes signatures fail for
        otherwise valid descriptions or callback data containing Cyrillic text.
        The exact byte sequence returned here is both signed and sent.
        """
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).replace("/", "\\/")
        return serialized.encode("utf-8")

    @staticmethod
    def _heleket_headers(payload: dict[str, Any]) -> dict[str, str]:
        raw = PaymentService._heleket_payload_bytes(payload)
        signature = hashlib.md5(
            base64.b64encode(raw) + settings.HELEKET_API_KEY.encode("utf-8")
        ).hexdigest()
        return {
            "merchant": settings.HELEKET_MERCHANT_ID,
            "sign": signature,
            "Content-Type": "application/json",
        }

    @staticmethod
    async def _heleket_post(
        path: str,
        payload: dict[str, Any],
        *,
        creation: bool = False,
    ) -> Optional[dict[str, Any]]:
        if not PaymentService.provider_configured("heleket"):
            if creation:
                raise ProviderCreationError("Heleket is not configured", definitive=True)
            return None
        raw = PaymentService._heleket_payload_bytes(payload)
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(
                    f"https://api.heleket.com{path}",
                    data=raw,
                    headers=PaymentService._heleket_headers(payload),
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or data.get("state") != 0:
                        logger.error("Heleket request failed: path=%s status=%s", path, response.status)
                        if creation:
                            # A 200 response with state != 0 is an explicit
                            # provider-side rejection, not a transport timeout.
                            raise ProviderCreationError(
                                "Heleket rejected invoice creation",
                                definitive=(
                                    response.status == 200
                                    or _definitive_client_error(response.status)
                                ),
                            )
                        return None
                    result = data.get("result")
                    if isinstance(result, dict):
                        return result
                    if creation:
                        raise ProviderCreationError(
                            "Heleket returned an incomplete invoice response",
                            definitive=False,
                        )
                    return None
        except ProviderCreationError:
            raise
        except Exception:
            logger.exception("Heleket request failed: path=%s", path)
            if creation:
                raise ProviderCreationError(
                    "Heleket invoice creation outcome is unknown", definitive=False
                )
            return None

    @staticmethod
    async def create_heleket_payment(
        *,
        amount: Decimal,
        external_order_id: str,
        description: str,
    ) -> Optional[ProviderInvoice]:
        if not PaymentService.provider_enabled("heleket"):
            raise ProviderCreationError("Heleket is disabled", definitive=True)
        payload = {
            "amount": f"{amount:.2f}",
            "currency": "RUB",
            "order_id": external_order_id,
            "url_return": PaymentService._return_url(),
            "url_callback": PaymentService._public_url("/webhook/heleket"),
            "lifetime": (
                PaymentService.provider_invoice_lifetime_minutes("heleket") * 60
            ),
            "additional_data": description[:255],
        }
        result = await PaymentService._heleket_post(
            "/v1/payment", payload, creation=True
        )
        if not result:
            raise ProviderCreationError(
                "Heleket returned no invoice", definitive=False
            )
        payment_id = result.get("uuid")
        payment_url = result.get("url")
        if not payment_id or not payment_url:
            logger.error("Heleket response did not contain uuid or URL")
            raise ProviderCreationError(
                "Heleket response did not contain invoice id or URL",
                definitive=False,
            )
        return ProviderInvoice(payment_id=str(payment_id), payment_url=str(payment_url))

    @staticmethod
    async def get_heleket_payment_status(external_order_id: str) -> Optional[dict[str, Any]]:
        return await PaymentService._heleket_post(
            "/v1/payment/info", {"order_id": external_order_id}
        )

    @staticmethod
    def verify_heleket_webhook(data: dict[str, Any]) -> bool:
        """Проверить подпись Heleket из тела webhook."""
        if not PaymentService.provider_configured("heleket"):
            return False
        signature = data.get("sign")
        if not isinstance(signature, str):
            return False
        unsigned_data = {key: value for key, value in data.items() if key != "sign"}
        raw = PaymentService._heleket_payload_bytes(unsigned_data)
        expected = hashlib.md5(
            base64.b64encode(raw) + settings.HELEKET_API_KEY.encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _lava_payload_bytes(payload: dict[str, Any]) -> bytes:
        """Serialize a Lava payload without converting money through float.

        Lava Business documents ``sum`` as a JSON number. ``json.dumps`` does
        not serialize :class:`Decimal`, and converting it to float can change
        a kopeck before the signed request is sent. A private sentinel lets us
        emit exactly the already-quantized numeric token (for example
        ``12.30``) while all other fields keep normal JSON escaping.
        """
        value = payload.get("sum")
        if not isinstance(value, Decimal):
            return json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        token = "__DIGITAL_NETWORKS_LAVA_SUM__"
        serialized_payload = dict(payload)
        serialized_payload["sum"] = token
        raw = json.dumps(
            serialized_payload, ensure_ascii=False, separators=(",", ":")
        )
        raw = raw.replace(
            json.dumps(token, ensure_ascii=False),
            format(value.quantize(Decimal("0.01")), "f"),
            1,
        )
        return raw.encode("utf-8")

    @staticmethod
    def _lava_signature(raw: bytes, secret: str) -> str:
        return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    @staticmethod
    async def create_lava_payment(
        *,
        amount: Decimal,
        external_order_id: str,
        description: str,
    ) -> Optional[ProviderInvoice]:
        if not PaymentService.provider_enabled("lava"):
            raise ProviderCreationError("Lava is disabled", definitive=True)
        payload = {
            "shopId": settings.LAVA_ID,
            "sum": amount.quantize(Decimal("0.01")),
            "orderId": external_order_id,
            "hookUrl": PaymentService._public_url("/webhook/lava"),
            "successUrl": PaymentService._return_url(),
            "failUrl": PaymentService._return_url(),
            "expire": PaymentService.provider_invoice_lifetime_minutes("lava"),
            "customFields": external_order_id,
            "comment": description[:255],
        }
        raw = PaymentService._lava_payload_bytes(payload)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Signature": PaymentService._lava_signature(raw, settings.LAVA_SECRET_KEY),
        }
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(
                    "https://api.lava.ru/business/invoice/create",
                    data=raw,
                    headers=headers,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or not data.get("status_check"):
                        logger.error("Lava payment creation failed: status=%s", response.status)
                        raise ProviderCreationError(
                            "Lava rejected invoice creation",
                            definitive=(
                                response.status == 200
                                or _definitive_client_error(response.status)
                            ),
                        )
        except ProviderCreationError:
            raise
        except Exception:
            logger.exception("Lava payment creation failed")
            raise ProviderCreationError(
                "Lava invoice creation outcome is unknown", definitive=False
            )

        if not isinstance(data, dict):
            raise ProviderCreationError(
                "Lava returned an invalid invoice response", definitive=False
            )
        result = data.get("data") or {}
        if not isinstance(result, dict):
            raise ProviderCreationError(
                "Lava returned an incomplete invoice response", definitive=False
            )
        payment_id = result.get("id")
        payment_url = result.get("url")
        if not payment_id or not payment_url:
            logger.error("Lava response did not contain invoice id or URL")
            raise ProviderCreationError(
                "Lava response did not contain invoice id or URL",
                definitive=False,
            )
        return ProviderInvoice(payment_id=str(payment_id), payment_url=str(payment_url))

    @staticmethod
    async def get_lava_payment_status(
        external_order_id: str,
    ) -> Optional[dict[str, Any]]:
        """Read the final Lava invoice state from the Business API.

        The webhook is signed as well, but this second request binds the
        notification to the actual invoice held by Lava before goods or
        balance are released.
        """
        if not PaymentService.provider_configured("lava"):
            return None
        payload = {
            "shopId": settings.LAVA_ID,
            "orderId": external_order_id,
        }
        raw = PaymentService._lava_payload_bytes(payload)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Signature": PaymentService._lava_signature(raw, settings.LAVA_SECRET_KEY),
        }
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(
                    "https://api.lava.ru/business/invoice/status",
                    data=raw,
                    headers=headers,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or not data.get("status_check"):
                        logger.error("Lava status lookup failed: status=%s", response.status)
                        return None
        except Exception:
            logger.exception("Lava status lookup failed")
            return None

        result = data.get("data")
        return result if isinstance(result, dict) else None

    @staticmethod
    def verify_lava_webhook(
        raw_body: bytes,
        data: dict[str, Any],
        *,
        authorization: str = "",
        header_signature: str = "",
    ) -> bool:
        """Проверить webhook Lava Business и совместимый legacy webhook.

        В legacy формате Lava передает поле ``sign`` — это MD5 от
        ``invoice_id:amount:pay_time:secret_key_2``. В Business API подпись
        передается заголовком ``Authorization``; поддерживаем также заголовок
        ``Signature`` для совместимости с текущими настройками Lava. Заголовок
        сверяется с HMAC-SHA256 точного сырого тела, поэтому JSON нельзя
        распарсить и сериализовать заново до проверки.
        """
        if (
            not PaymentService.provider_configured("lava")
            or not PaymentService._lava_webhook_secrets()
        ):
            return False

        body_signature = str(data.get("sign") or "")
        if body_signature:
            invoice_id = str(data.get("invoice_id") or data.get("invoiceId") or "")
            amount = str(data.get("amount") or "")
            pay_time = str(data.get("pay_time") or data.get("payTime") or "")
            if invoice_id and amount and pay_time:
                return any(
                    hmac.compare_digest(
                        hashlib.md5(
                            f"{invoice_id}:{amount}:{pay_time}:{secret}".encode("utf-8")
                        ).hexdigest(),
                        body_signature,
                    )
                    for secret in PaymentService._lava_webhook_secrets()
                )

        received = (authorization or header_signature).removeprefix("Bearer ").strip()
        if not received:
            return False
        return any(
            hmac.compare_digest(
                PaymentService._lava_signature(raw_body, secret), received
            )
            for secret in PaymentService._lava_webhook_secrets()
        )

    @staticmethod
    async def create_provider_invoice(
        provider: str,
        *,
        amount: Decimal,
        payment_record_id: int,
        external_order_id: str,
        description: str,
    ) -> ProviderInvoice:
        """Создать счёт через единый безопасный dispatch.

        Все вызовы используют один ``external_order_id``. Для YooKassa это
        Idempotence-Key, а для Heleket и Lava это стабильный orderId. У
        CryptoBot повторное создание разрешается только после его отдельной
        сверки по payload в вызывающем state machine.
        """
        if provider == "yookassa":
            invoice = await PaymentService.create_yookassa_payment(
                amount=amount,
                payment_record_id=payment_record_id,
                external_order_id=external_order_id,
                description=description,
            )
        elif provider == "yoomoney":
            invoice = await PaymentService.create_yoomoney_payment(
                amount=amount,
                external_order_id=external_order_id,
                description=description,
            )
        elif provider == "heleket":
            invoice = await PaymentService.create_heleket_payment(
                amount=amount,
                external_order_id=external_order_id,
                description=description,
            )
        elif provider == "lava":
            invoice = await PaymentService.create_lava_payment(
                amount=amount,
                external_order_id=external_order_id,
                description=description,
            )
        elif provider == "cryptobot":
            invoice = await PaymentService.create_cryptobot_payment(
                amount=amount,
                external_order_id=external_order_id,
                description=description,
            )
        else:
            raise ProviderCreationError(
                "Unknown external payment provider", definitive=True
            )
        if not invoice:
            # Individual clients normally raise ProviderCreationError. This is
            # a defensive fallback for an unexpected incomplete integration.
            raise ProviderCreationError(
                "Provider returned no invoice", definitive=False
            )
        return invoice

    @staticmethod
    async def reconcile_provider_invoice(
        provider: str,
        *,
        external_order_id: str,
        provider_payment_id: str | None,
    ) -> ProviderInvoiceStatus:
        """Сверить уже начатое создание счёта без флага ``*_ENABLED``.

        ``unknown`` никогда не означает отказ: транспортный сбой или
        недостаток данных оставляет локальный PENDING до следующей безопасной
        сверки/TTL. Для финального ``succeeded`` возвращаются также сумма и
        валюта из API, чтобы потерянный webhook можно было безопасно
        завершить фоновой задачей.
        """
        if provider == "yookassa":
            if not provider_payment_id:
                # Без ID YooKassa нельзя читать статус. Повторный POST с тем
                # же Idempotence-Key ниже безопасно вернёт тот же объект.
                return ProviderInvoiceStatus("unknown")
            data = await PaymentService.get_yookassa_payment_status(
                provider_payment_id
            )
            if not data:
                return ProviderInvoiceStatus("unknown")
            payment_id = str(data.get("id") or "") or None
            confirmation = data.get("confirmation") or {}
            payment_url = (
                str(confirmation.get("confirmation_url"))
                if isinstance(confirmation, dict)
                and confirmation.get("confirmation_url")
                else None
            )
            amount_data = data.get("amount") or {}
            amount = (
                str(amount_data.get("value"))
                if isinstance(amount_data, dict) and amount_data.get("value") is not None
                else None
            )
            currency = (
                str(amount_data.get("currency") or "").upper() or None
                if isinstance(amount_data, dict)
                else None
            )
            status = str(data.get("status") or "").lower()
            if status == "succeeded":
                return ProviderInvoiceStatus(
                    "succeeded", payment_id, payment_url, amount, currency
                )
            if status == "canceled":
                return ProviderInvoiceStatus("failed", payment_id, payment_url)
            return ProviderInvoiceStatus("pending", payment_id, payment_url)

        if provider == "yoomoney":
            # ЮMoney не предоставляет здесь отдельный invoice-status API.
            # Финальное состояние устанавливается только после подписанного
            # HTTP-уведомления; до него счёт остаётся pending.
            return ProviderInvoiceStatus("unknown")

        if provider == "heleket":
            data = await PaymentService.get_heleket_payment_status(external_order_id)
            if not data:
                return ProviderInvoiceStatus("unknown")
            payment_id = str(data.get("uuid") or "") or None
            payment_url = str(data.get("url") or "") or None
            amount = str(data.get("amount")) if data.get("amount") is not None else None
            currency = str(data.get("currency") or "").upper() or None
            status = str(data.get("status") or "").lower()
            if status in {"fail", "wrong_amount", "cancel", "system_fail", "expired"}:
                return ProviderInvoiceStatus("failed", payment_id, payment_url)
            if status in {"paid", "paid_over"} and data.get("is_final", True):
                return ProviderInvoiceStatus(
                    "succeeded", payment_id, payment_url, amount, currency
                )
            return ProviderInvoiceStatus("pending", payment_id, payment_url)

        if provider == "lava":
            data = await PaymentService.get_lava_payment_status(external_order_id)
            if not data:
                return ProviderInvoiceStatus("unknown")
            payment_id = str(data.get("id") or data.get("invoiceId") or "") or None
            payment_url = str(data.get("url") or "") or None
            amount = data.get("amount", data.get("sum"))
            amount_value = str(amount) if amount is not None else None
            # Lava Business invoices are issued in rubles. If a future API
            # response explicitly carries another currency, do not credit it.
            currency = str(data.get("currency") or "RUB").upper()
            status = str(data.get("status") or "").lower()
            if status in {
                "fail", "failed", "cancel", "canceled", "error", "expired", "rejected",
            }:
                return ProviderInvoiceStatus("failed", payment_id, payment_url)
            if status in {"success", "paid"}:
                return ProviderInvoiceStatus(
                    "succeeded", payment_id, payment_url, amount_value, currency
                )
            return ProviderInvoiceStatus("pending", payment_id, payment_url)

        if provider == "cryptobot":
            data = (
                await PaymentService.get_cryptobot_invoice(provider_payment_id)
                if provider_payment_id
                else await PaymentService.find_cryptobot_invoice_by_payload(
                    external_order_id
                )
            )
            if not data:
                return ProviderInvoiceStatus("unknown")
            payment_id = str(data.get("invoice_id") or "") or None
            payment_url = str(
                data.get("bot_invoice_url") or data.get("pay_url") or ""
            ) or None
            amount = str(data.get("amount")) if data.get("amount") is not None else None
            currency = (
                str(data.get("fiat") or "").upper()
                if data.get("currency_type") == "fiat"
                else None
            )
            status = str(data.get("status") or "").lower()
            if status == "paid":
                return ProviderInvoiceStatus(
                    "succeeded", payment_id, payment_url, amount, currency
                )
            if status in {"expired", "cancel", "canceled", "failed", "fail"}:
                return ProviderInvoiceStatus("failed", payment_id, payment_url)
            return ProviderInvoiceStatus("pending", payment_id, payment_url)

        return ProviderInvoiceStatus("unknown")


PAYMENT_PROVIDERS = {
    "yookassa": {"title": "ЮKassa", "fields": (("YOOKASSA_SHOP_ID", "ID магазина"), ("YOOKASSA_SECRET_KEY", "Секретный ключ"))},
    "yoomoney": {"title": "ЮMoney", "fields": (("YOOMONEY_WALLET", "Номер кошелька"), ("YOOMONEY_NOTIFICATION_SECRET", "Секрет HTTP-уведомлений"))},
    "heleket": {"title": "Heleket", "fields": (("HELEKET_MERCHANT_ID", "ID мерчанта"), ("HELEKET_API_KEY", "API-ключ"))},
    "cryptobot": {"title": "CryptoBot", "fields": (("CRYPTOBOT_API", "API Token Crypto Pay"),)},
    "lava": {"title": "Lava", "fields": (("LAVA_ID", "ID проекта"), ("LAVA_SECRET_KEY", "Секретный ключ"), ("LAVA_ADDITIONAL_KEY", "Дополнительный ключ"), ("LAVA_WEBHOOK_SECRET", "Секрет webhook"))},
}


def payment_setting_key(field: str) -> str:
    return f"payment.{field}"


async def load_payment_settings(session: AsyncSession) -> None:
    fields = [field for provider in PAYMENT_PROVIDERS.values() for field, _ in provider["fields"]]
    # Флаги из env являются начальными значениями. Если администратор менял
    # их из панели, сохранённое в БД значение имеет приоритет и переживает
    # перезапуск Docker-контейнера.
    fields.extend(PAYMENT_ENABLED_FIELDS)
    fields.extend(PAYMENT_BOOLEAN_FIELDS)
    fields.extend(PAYMENT_DECIMAL_FIELDS)
    fields.append("PAYMENT_METHOD_ORDER")
    result = await session.execute(select(Setting).where(Setting.key.in_([payment_setting_key(field) for field in fields])))
    for item in result.scalars():
        field = item.key.removeprefix("payment.")
        if field in fields:
            value = item.value or ""
            if field in PAYMENT_BOOLEAN_FIELDS or field in PAYMENT_ENABLED_FIELDS:
                setattr(settings, field, value.lower() in {"1", "true", "yes"})
            elif field in PAYMENT_DECIMAL_FIELDS:
                try:
                    parsed = Decimal(value)
                except (InvalidOperation, TypeError, ValueError):
                    logger.warning("Ignoring invalid payment rate setting %s", field)
                    continue
                if parsed.is_finite() and parsed > 0:
                    setattr(settings, field, parsed)
            else:
                setattr(settings, field, value)


async def save_payment_setting(session: AsyncSession, field: str, value: str) -> None:
    if field in PAYMENT_ENABLED_FIELDS:
        raise ValueError(f"{field} хранится в .env, а не в базе данных")
    key = payment_setting_key(field)
    result = await session.execute(select(Setting).where(Setting.key == key))
    item = result.scalar_one_or_none()
    if item is None:
        session.add(Setting(key=key, value=value))
    else:
        item.value = value
    setattr(
        settings,
        field,
        value.lower() in {"1", "true", "yes"}
        if field in PAYMENT_BOOLEAN_FIELDS
        else Decimal(value) if field in PAYMENT_DECIMAL_FIELDS else value,
    )


async def save_payment_enabled_setting(
    session: AsyncSession, field: str, enabled: bool
) -> None:
    """Persist a payment toggle in DB and update the live settings object."""
    if field not in PAYMENT_ENABLED_FIELDS:
        raise ValueError(f"{field} не является флагом платёжной системы")
    key = payment_setting_key(field)
    result = await session.execute(select(Setting).where(Setting.key == key))
    item = result.scalar_one_or_none()
    value = "true" if enabled else "false"
    if item is None:
        session.add(Setting(key=key, value=value))
    else:
        item.value = value
    setattr(settings, field, bool(enabled))
