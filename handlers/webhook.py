"""Безопасные HTTP webhook для Telegram и платежных провайдеров."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import web
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import settings
from database.db import async_session_maker, database_is_ready
from database.models import Payment
from utils.checkout import (
    CheckoutError,
    CompletedOrder,
    FAILED_PAYMENT_STATUS,
    PENDING_PAYMENT_STATUS,
    _locked_order,
    _locked_user,
    cancel_pending_batch,
    cancel_pending_order,
    complete_balance_topup,
    complete_external_batch_payment,
    complete_external_order_payment,
)
from utils.fulfillment import deliver_completed_order
from utils.notifications import notify_balance_topup
from utils.payments import PaymentService

logger = logging.getLogger(__name__)


def _same_amount(value: Any, expected: Decimal | float | int | str) -> bool:
    """Сравнить денежную сумму провайдера с ожидаемой без ошибки float."""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) == Decimal(
            str(expected)
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False


async def _find_pending_payment(
    session: AsyncSession,
    *,
    method: str,
    provider_payment_id: str | None = None,
    external_order_id: str | None = None,
    lock: bool = False,
) -> Payment | None:
    stmt = select(Payment).where(Payment.payment_method == method)
    if provider_payment_id:
        stmt = stmt.where(Payment.payment_id == provider_payment_id)
    elif external_order_id:
        stmt = stmt.where(Payment.external_order_id == external_order_id)
    else:
        return None
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _mark_pending_payment_failed(
    session: AsyncSession,
    *,
    method: str,
    provider_payment_id: str | None = None,
    external_order_id: str | None = None,
) -> tuple[int | None, int | None]:
    """Fail a provider invoice using the same lock order as payment success.

    A preliminary read is deliberately not locked.  It only gives us the
    owner/order IDs needed to acquire the canonical locks: User -> Order ->
    Payment.  The payment is matched again after the lock.
    """
    # When an external order ID is available it is the stable local lookup:
    # payment_id may still be NULL in the small window after remote invoice
    # creation.  The provider ID is nevertheless checked under the lock below.
    lookup_provider_payment_id = (
        provider_payment_id if external_order_id is None else None
    )
    payment_preview = await _find_pending_payment(
        session,
        method=method,
        provider_payment_id=lookup_provider_payment_id,
        external_order_id=external_order_id,
    )
    if not payment_preview:
        return None, None

    buyer = await _locked_user(session, payment_preview.user_id)
    order = None
    if payment_preview.order_id:
        order = await _locked_order(session, payment_preview.order_id)

    payment = await _find_pending_payment(
        session,
        method=method,
        provider_payment_id=lookup_provider_payment_id,
        external_order_id=external_order_id,
        lock=True,
    )
    if not payment or payment.user_id != buyer.id:
        return None, None
    if order and payment.order_id != order.id:
        return None, None
    if provider_payment_id and payment.payment_id not in {
        None,
        provider_payment_id,
    }:
        return None, None
    if payment.status != PENDING_PAYMENT_STATUS:
        return None, None

    payment.status = FAILED_PAYMENT_STATUS
    payment.completed_at = datetime.now()
    return payment.order_id, payment.batch_id


async def _fail_provider_payment_and_release_order(
    session: AsyncSession,
    *,
    method: str,
    provider_payment_id: str | None = None,
    external_order_id: str | None = None,
) -> None:
    """Record a final provider failure and release a safe local reservation."""
    order_id, batch_id = await _mark_pending_payment_failed(
        session,
        method=method,
        provider_payment_id=provider_payment_id,
        external_order_id=external_order_id,
    )
    if not order_id and not batch_id:
        return
    try:
        # Provider failure was verified by this webhook already. Do not start a
        # second HTTP reconciliation while this transaction holds the payment
        # lock; it would also risk rolling back the FAILED state above.
        if batch_id:
            await cancel_pending_batch(session, batch_id)
        else:
            await cancel_pending_order(session, order_id, reconcile_provider=False)
    except CheckoutError as exc:
        # Keep the provider failure even if a damaged legacy reservation needs
        # manual reconciliation.  Rolling it back would make repeated webhook
        # retries re-open a final invoice.
        logger.warning("Could not release failed payment reservation order=%s batch=%s: %s", order_id, batch_id, exc)


async def _complete_validated_payment(
    *,
    bot: Bot,
    payment_record_id: int,
    local_amount: Decimal | float | int | str,
    is_order_payment: bool,
    payment_method: str,
    external_order_id: str,
    provider_payment_id: str,
    provider_amount: Decimal | float | int | str,
) -> tuple[bool, str]:
    """Блокирует строку Payment и завершает оплату ровно один раз."""
    completed_order: CompletedOrder | None = None
    completed_orders: list[CompletedOrder] = []
    topup_user = None
    try:
        async with async_session_maker() as session:
            # Повторная проверка выполняется в той же транзакции, что и
            # изменение баланса/заказа. Внутри checkout строка блокируется.
            if is_order_payment:
                payment_row = await session.get(Payment, payment_record_id)
                if payment_row and payment_row.batch_id:
                    completed_orders = await complete_external_batch_payment(
                        session,
                        payment_id=payment_record_id,
                        provider_payment_id=provider_payment_id,
                        payment_method=payment_method,
                        external_order_id=external_order_id,
                        provider_amount=provider_amount,
                    )
                else:
                    completed_order = await complete_external_order_payment(
                        session,
                        payment_id=payment_record_id,
                        provider_payment_id=provider_payment_id,
                        payment_method=payment_method,
                        external_order_id=external_order_id,
                        provider_amount=provider_amount,
                    )
            else:
                topup_user = await complete_balance_topup(
                    session,
                    payment_id=payment_record_id,
                    provider_payment_id=provider_payment_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                    provider_amount=provider_amount,
                )
            await session.commit()
    except CheckoutError as exc:
        logger.warning("Payment %s was rejected: %s", payment_record_id, exc)
        return False, str(exc)
    except Exception:
        logger.exception("Payment %s could not be completed", payment_record_id)
        return False, "Ошибка обработки платежа"

    if completed_order and not completed_order.already_completed:
        await deliver_completed_order(bot, completed_order)
    for item in completed_orders:
        if not item.already_completed:
            await deliver_completed_order(bot, item)
    if not completed_order and not completed_orders and topup_user:
        try:
            await notify_balance_topup(None, topup_user, local_amount, bot)
        except Exception:
            logger.exception("Could not notify about topup %s", payment_record_id)
    return True, "OK"


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    """Webhook ЮKassa с серверной сверкой статуса через API ЮKassa."""
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return web.Response(status=400, text="Invalid JSON")
        body_payment = payload.get("object") or {}
        if not isinstance(body_payment, dict):
            return web.Response(status=400, text="Invalid payment object")
        provider_payment_id = str(body_payment.get("id") or "")
        if not provider_payment_id:
            return web.Response(status=400, text="Missing payment id")

        # ЮKassa рекомендует проверять статус объекта по API. Не доверяем
        # событию, сумме или metadata из входящего тела.
        provider_payment = await PaymentService.get_yookassa_payment_status(provider_payment_id)
        if not provider_payment:
            return web.Response(status=400, text="Payment was not verified")

        metadata = provider_payment.get("metadata") or {}
        if not isinstance(metadata, dict):
            return web.Response(status=400, text="Invalid payment metadata")
        payment_record_id = str(metadata.get("payment_record_id") or "")
        external_order_id = str(metadata.get("external_order_id") or "")
        if not payment_record_id.isdigit() or not external_order_id:
            return web.Response(status=400, text="Invalid payment metadata")

        # The incoming event itself is not authenticated.  Only the status
        # fetched from YooKassa's API is allowed to change local state.
        if provider_payment.get("status") == "canceled":
            async with async_session_maker() as session:
                await _fail_provider_payment_and_release_order(
                    session,
                    method="yookassa",
                    provider_payment_id=provider_payment_id,
                    external_order_id=external_order_id,
                )
                await session.commit()
            return web.Response(text="OK")
        if provider_payment.get("status") != "succeeded":
            return web.Response(text="OK")

        if (provider_payment.get("amount") or {}).get("currency") != "RUB":
            return web.Response(status=400, text="Invalid currency")
        provider_amount = (provider_payment.get("amount") or {}).get("value")

        async with async_session_maker() as session:
            payment = await session.get(Payment, int(payment_record_id))
            if not payment or payment.payment_method != "yookassa":
                return web.Response(status=400, text="Unknown payment")
            if (
                payment.payment_id not in {None, provider_payment_id}
                or payment.external_order_id != external_order_id
            ):
                return web.Response(status=400, text="Payment binding mismatch")
            if not _same_amount(provider_amount, payment.amount):
                return web.Response(status=400, text="Amount mismatch")
            local_amount = payment.amount
            is_order_payment = bool(payment.order_id or payment.batch_id)

        success, message = await _complete_validated_payment(
            bot=request.app["bot"],
            payment_record_id=int(payment_record_id),
            local_amount=local_amount,
            is_order_payment=is_order_payment,
            payment_method="yookassa",
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
            provider_amount=provider_amount,
        )
        return web.Response(status=200 if success else 409, text=message)
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")
    except Exception:
        logger.exception("YooKassa webhook processing failed")
        return web.Response(status=500, text="Internal error")


async def handle_heleket_webhook(request: web.Request) -> web.Response:
    """Webhook Heleket: подпись + повторная сверка счета по API."""
    try:
        raw_body = await request.read()
        payload = json.loads(raw_body)
        if not isinstance(payload, dict) or not PaymentService.verify_heleket_webhook(payload):
            return web.Response(status=401, text="Invalid signature")

        external_order_id = str(payload.get("order_id") or "")
        provider_payment_id = str(payload.get("uuid") or "")
        if not external_order_id or not provider_payment_id:
            return web.Response(status=400, text="Missing payment fields")

        async with async_session_maker() as session:
            payment = await _find_pending_payment(
                session, method="heleket", external_order_id=external_order_id
            )
            if not payment or payment.payment_id not in {None, provider_payment_id}:
                return web.Response(status=400, text="Unknown payment")
            payment_record_id = payment.id
            local_amount = payment.amount
            is_order_payment = bool(payment.order_id or payment.batch_id)

        verified = await PaymentService.get_heleket_payment_status(external_order_id)
        if not verified:
            return web.Response(status=400, text="Payment was not verified")
        status = str(verified.get("status") or "").lower()
        if (
            str(verified.get("uuid") or "") != provider_payment_id
            or str(verified.get("order_id") or "") != external_order_id
        ):
            return web.Response(status=400, text="Payment binding mismatch")
        if status in {"fail", "wrong_amount", "cancel", "system_fail", "expired"}:
            async with async_session_maker() as session:
                await _fail_provider_payment_and_release_order(
                    session,
                    method="heleket",
                    provider_payment_id=provider_payment_id,
                    external_order_id=external_order_id,
                )
                await session.commit()
            return web.Response(text="OK")
        if status not in {"paid", "paid_over"} or not verified.get("is_final"):
            return web.Response(text="OK")
        provider_amount = verified.get("amount")
        if verified.get("currency") != "RUB" or not _same_amount(provider_amount, local_amount):
            return web.Response(status=400, text="Amount mismatch")

        success, message = await _complete_validated_payment(
            bot=request.app["bot"],
            payment_record_id=payment_record_id,
            local_amount=local_amount,
            is_order_payment=is_order_payment,
            payment_method="heleket",
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
            provider_amount=provider_amount,
        )
        return web.Response(status=200 if success else 409, text=message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.Response(status=400, text="Invalid JSON")
    except Exception:
        logger.exception("Heleket webhook processing failed")
        return web.Response(status=500, text="Internal error")


async def handle_lava_webhook(request: web.Request) -> web.Response:
    """Webhook Lava Business API, проверенный дополнительным ключом."""
    try:
        raw_body = await request.read()
        payload = json.loads(raw_body)
        authorization = request.headers.get("Authorization", "")
        signature = request.headers.get("Signature", "")
        if not isinstance(payload, dict) or not PaymentService.verify_lava_webhook(
            raw_body,
            payload,
            authorization=authorization,
            header_signature=signature,
        ):
            return web.Response(status=401, text="Invalid signature")

        external_order_id = str(payload.get("orderId") or payload.get("order_id") or "")
        provider_payment_id = str(payload.get("invoiceId") or payload.get("invoice_id") or "")
        if not external_order_id or not provider_payment_id:
            return web.Response(status=400, text="Missing payment fields")
        async with async_session_maker() as session:
            payment = await _find_pending_payment(
                session, method="lava", external_order_id=external_order_id
            )
            if not payment or payment.payment_id not in {None, provider_payment_id}:
                return web.Response(status=400, text="Unknown payment")
            payment_record_id = payment.id
            local_amount = payment.amount
            is_order_payment = bool(payment.order_id or payment.batch_id)

        # A valid raw-body signature proves the sender; the status API proves
        # that the particular Lava invoice is final and has the same amount.
        verified = await PaymentService.get_lava_payment_status(external_order_id)
        if not verified:
            return web.Response(status=409, text="Payment was not verified")
        verified_payment_id = str(
            verified.get("id") or verified.get("invoiceId") or ""
        )
        verified_external_order_id = str(
            verified.get("order_id") or verified.get("orderId") or ""
        )
        if (
            verified_payment_id != provider_payment_id
            or verified_external_order_id != external_order_id
        ):
            return web.Response(status=400, text="Payment binding mismatch")

        status = str(verified.get("status") or "").lower()
        if status in {
            "fail",
            "failed",
            "cancel",
            "canceled",
            "error",
            "expired",
            "rejected",
        }:
            async with async_session_maker() as session:
                await _fail_provider_payment_and_release_order(
                    session,
                    method="lava",
                    provider_payment_id=provider_payment_id,
                    external_order_id=external_order_id,
                )
                await session.commit()
            return web.Response(text="OK")
        provider_amount = verified.get("amount")
        if status not in {"success", "paid"}:
            # Return non-200 so Lava retries a signed callback that arrived
            # before the status endpoint reached its final state.
            return web.Response(status=409, text="Payment is not final")
        if not _same_amount(provider_amount, local_amount):
            return web.Response(status=400, text="Amount mismatch")

        success, message = await _complete_validated_payment(
            bot=request.app["bot"],
            payment_record_id=payment_record_id,
            local_amount=local_amount,
            is_order_payment=is_order_payment,
            payment_method="lava",
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
            provider_amount=provider_amount,
        )
        return web.Response(status=200 if success else 409, text=message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.Response(status=400, text="Invalid JSON")
    except Exception:
        logger.exception("Lava webhook processing failed")
        return web.Response(status=500, text="Internal error")


async def handle_yoomoney_webhook(request: web.Request) -> web.Response:
    """HTTP-уведомление ЮMoney с HMAC и строгой сверкой суммы/метки."""
    try:
        form = await request.post()
        data = {str(key): str(value) for key, value in form.items()}
        if not PaymentService.verify_yoomoney_webhook(data):
            return web.Response(status=401, text="Invalid signature")

        notification_type = data.get("notification_type", "")
        if notification_type not in {"p2p-incoming", "card-incoming"}:
            # Signed non-incoming notifications are valid but do not represent
            # a customer payment that can settle a local order.
            return web.Response(text="OK")
        if data.get("currency") != "643":
            return web.Response(status=400, text="Invalid currency")
        if data.get("codepro", "false").lower() == "true":
            return web.Response(status=400, text="Protected payment is not accepted")
        if data.get("unaccepted", "false").lower() == "true":
            return web.Response(status=400, text="Unaccepted payment is not accepted")

        external_order_id = data.get("label", "").strip()
        operation_id = data.get("operation_id", "").strip()
        if not external_order_id or not external_order_id.startswith("yoomoney-"):
            return web.Response(status=400, text="Invalid payment label")
        if not operation_id:
            return web.Response(status=400, text="Missing operation id")

        async with async_session_maker() as session:
            payment = await _find_pending_payment(
                session, method="yoomoney", external_order_id=external_order_id
            )
            if not payment:
                # ЮMoney retries notifications. An already completed payment
                # is safe to acknowledge; an unknown label must be retried or
                # investigated instead of crediting anything.
                return web.Response(status=400, text="Unknown payment")
            if payment.payment_id not in {None, external_order_id}:
                return web.Response(status=400, text="Payment binding mismatch")
            payment_record_id = payment.id
            local_amount = payment.amount
            is_order_payment = bool(payment.order_id or payment.batch_id)

        provider_amount = data.get("amount", "")
        if not _same_amount(provider_amount, local_amount):
            return web.Response(status=400, text="Amount mismatch")

        # The local label is the immutable binding stored at invoice creation;
        # operation_id is retained only as the provider's audit identifier.
        success, message = await _complete_validated_payment(
            bot=request.app["bot"],
            payment_record_id=payment_record_id,
            local_amount=local_amount,
            is_order_payment=is_order_payment,
            payment_method="yoomoney",
            external_order_id=external_order_id,
            provider_payment_id=external_order_id,
            provider_amount=provider_amount,
        )
        return web.Response(status=200 if success else 409, text=message)
    except (ValueError, TypeError):
        return web.Response(status=400, text="Invalid notification")
    except Exception:
        logger.exception("ЮMoney webhook processing failed")
        return web.Response(status=500, text="Internal error")


async def handle_cryptobot_webhook(request: web.Request) -> web.Response:
    """Crypto Pay: HMAC и сверка paid/expired/cancelled счёта по API."""
    try:
        raw_body = await request.read()
        signature = request.headers.get("crypto-pay-api-signature", "")
        if not PaymentService.verify_cryptobot_webhook(raw_body, signature):
            return web.Response(status=401, text="Invalid signature")
        update = json.loads(raw_body)
        payload = update.get("payload") if isinstance(update, dict) else None
        # Crypto Pay normally sends invoice_paid, but treating any signed
        # invoice update as a status hint lets us release an expired/cancelled
        # invoice too. The final decision still comes only from getInvoices.
        if not isinstance(payload, dict):
            return web.Response(text="OK")
        invoice_id = str(payload.get("invoice_id") or "")
        external_order_id = str(payload.get("payload") or "")
        if not invoice_id or not external_order_id:
            return web.Response(status=400, text="Missing payment fields")
        async with async_session_maker() as session:
            payment = await _find_pending_payment(session, method="cryptobot", external_order_id=external_order_id)
            if not payment or payment.payment_id not in {None, invoice_id}:
                return web.Response(status=400, text="Unknown payment")
            payment_record_id, local_amount, is_order_payment = payment.id, payment.amount, bool(payment.order_id or payment.batch_id)
        verified = await PaymentService.get_cryptobot_invoice(invoice_id)
        if not verified:
            return web.Response(status=409, text="Payment was not verified")
        if str(verified.get("invoice_id")) != invoice_id or str(verified.get("payload")) != external_order_id:
            return web.Response(status=400, text="Payment binding mismatch")
        status = str(verified.get("status", "")).lower()
        if status in {"expired", "cancel", "canceled", "failed", "fail"}:
            async with async_session_maker() as session:
                await _fail_provider_payment_and_release_order(
                    session,
                    method="cryptobot",
                    provider_payment_id=invoice_id,
                    external_order_id=external_order_id,
                )
                await session.commit()
            return web.Response(text="OK")
        if status != "paid":
            return web.Response(text="OK")
        if verified.get("currency_type") != "fiat" or verified.get("fiat") != "RUB" or not _same_amount(verified.get("amount"), local_amount):
            return web.Response(status=400, text="Amount mismatch")
        success, message = await _complete_validated_payment(
            bot=request.app["bot"], payment_record_id=payment_record_id, local_amount=local_amount,
            is_order_payment=is_order_payment, payment_method="cryptobot", external_order_id=external_order_id,
            provider_payment_id=invoice_id, provider_amount=verified.get("amount"),
        )
        return web.Response(status=200 if success else 409, text=message)
    except Exception:
        logger.exception("CryptoBot webhook processing failed")
        return web.Response(status=500, text="Internal error")


def create_webhook_app(bot: Bot | None = None, dispatcher=None) -> web.Application:
    """Создать aiohttp-приложение webhook без незащищенных совместимых путей."""
    app = web.Application()
    # Mutate the nested runtime state after AppRunner startup instead of
    # replacing aiohttp application keys, which is deprecated after freeze.
    app["runtime_state"] = {"ready": False}
    # This flag is set only after bot.py has built and populated Dispatcher.
    # Keep it explicit rather than treating any arbitrary object in app as a
    # ready update processor.
    app["dispatcher_ready"] = dispatcher is not None
    if bot is not None:
        app["bot"] = bot
    if dispatcher is not None:
        app["dispatcher"] = dispatcher

    app.router.add_post("/webhook/yookassa", handle_yookassa_webhook)
    app.router.add_post("/webhook/heleket", handle_heleket_webhook)
    app.router.add_post("/webhook/lava", handle_lava_webhook)
    app.router.add_post("/webhook/yoomoney", handle_yoomoney_webhook)
    app.router.add_post("/webhook/cryptobot", handle_cryptobot_webhook)

    async def handle_telegram_webhook(request: web.Request) -> web.Response:
        if not dispatcher or not bot:
            return web.Response(status=500, text="Dispatcher not configured")
        expected_secret = settings.TELEGRAM_WEBHOOK_SECRET_TOKEN
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_secret or not hmac_compare(expected_secret, received_secret):
            logger.warning("Rejected Telegram webhook with invalid secret token")
            return web.Response(status=401, text="Unauthorized")
        try:
            update_data = await request.json()
            from aiogram.types import Update

            await dispatcher.feed_update(bot, Update(**update_data))
            return web.Response(text="OK")
        except Exception:
            logger.exception("Telegram webhook processing failed")
            return web.Response(status=500, text="Internal error")

    async def health(request: web.Request) -> web.Response:
        """Readiness endpoint: HTTP server, dispatcher and DB must be usable."""
        if not request.app["runtime_state"]["ready"]:
            return web.Response(status=503, text="Not ready")
        if request.app.get("bot") is None:
            return web.Response(status=503, text="Bot is not configured")
        # The internal HTTP server is started only after the active dispatcher
        # has been built in both webhook and polling modes. Treating a missing
        # dispatcher as healthy would let Docker keep a container that can
        # receive payment callbacks but cannot process Telegram updates.
        if (
            request.app.get("dispatcher") is None
            or not request.app.get("dispatcher_ready")
        ):
            return web.Response(status=503, text="Dispatcher is not configured")
        if not await database_is_ready():
            return web.Response(status=503, text="Database is not ready")
        return web.Response(text="OK")

    app.router.add_post("/webhook/telegram", handle_telegram_webhook)
    app.router.add_get("/health", health)
    return app


def hmac_compare(expected: str, received: str) -> bool:
    """Constant-time compare, isolated to make it easy to test."""
    import hmac

    return bool(received) and hmac.compare_digest(expected, received)
