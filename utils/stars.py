"""Asynchronous Fragment client used to deliver Stars and Premium."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from bot import settings


logger = logging.getLogger(__name__)


class FragmentError(RuntimeError):
    """Fragment error with an explicit indication whether retry is safe."""

    def __init__(self, message: str, *, outcome_unknown: bool = False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def _token_path() -> Path:
    return Path(settings.FRAGMENT_TOKEN_FILE or "/data/auth_token.json")


def _load_token() -> str | None:
    configured = settings.FRAGMENT_CONNECTION_TOKEN.strip()
    if configured:
        return configured
    path = _token_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = payload.get("token")
        return token if isinstance(token, str) and token.strip() else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _base_url() -> str:
    return settings.FRAGMENT_API_URL.rstrip("/")


async def _json_request(
    method: str,
    path: str,
    *,
    payload: dict,
    headers: dict[str, str] | None = None,
) -> dict:
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.request(
                method,
                f"{_base_url()}{path}",
                json=payload,
                headers=headers or {},
            ) as response:
                body = await response.text()
                try:
                    data = json.loads(body) if body else {}
                except ValueError:
                    data = {"detail": body[:500]}
                if response.status >= 400:
                    detail = data.get("detail") or data.get("message") or body[:300]
                    # A gateway/server timeout or error may arrive after the
                    # purchase was accepted upstream. Never allow an automatic
                    # retry unless Fragment has clearly rejected the request.
                    outcome_unknown = response.status in {408, 409} or response.status >= 500
                    raise FragmentError(
                        f"Fragment HTTP {response.status}: {detail}",
                        outcome_unknown=outcome_unknown,
                    )
                if not isinstance(data, dict):
                    raise FragmentError(
                        "Fragment returned an invalid response",
                        outcome_unknown=True,
                    )
                return data
    except asyncio.TimeoutError as exc:
        raise FragmentError("Fragment request timed out", outcome_unknown=True) from exc
    except aiohttp.ClientError as exc:
        raise FragmentError(
            f"Fragment network error: {exc}",
            outcome_unknown=True,
        ) from exc


async def send_stars(target_username: str, quantity: int) -> None:
    """Send Stars through a token from a configured Fragment connection."""
    if quantity <= 0:
        raise FragmentError("Stars quantity must be positive")
    username = target_username.removeprefix("@").strip()
    if not username:
        raise FragmentError("Recipient username is empty")

    token = await asyncio.to_thread(_load_token)
    if not token:
        raise FragmentError(
            "FRAGMENT_CONNECTION_TOKEN is not configured; create a Fragment "
            "connection in the dashboard and copy its token"
        )

    try:
        await _json_request(
            "POST",
            "/order/stars/",
            payload={"username": username, "quantity": quantity, "show_sender": "false"},
            headers={"Authorization": f"JWT {token}", "Content-Type": "application/json"},
        )
    except FragmentError as exc:
        if "401" in str(exc) or "403" in str(exc):
            raise FragmentError(
                "Fragment connection token was rejected; recreate the connection "
                "in the Fragment dashboard",
                outcome_unknown=exc.outcome_unknown,
            ) from exc
        raise
    logger.info("Sent %s Telegram Stars to @%s", quantity, username)


async def send_premium(target_username: str, months: int) -> None:
    """Gift a Telegram Premium subscription through the same Fragment connection."""
    if months not in {3, 6, 12}:
        raise FragmentError("Premium subscription duration must be 3, 6, or 12 months")
    username = target_username.removeprefix("@").strip()
    if not username:
        raise FragmentError("Recipient username is empty")

    token = await asyncio.to_thread(_load_token)
    if not token:
        raise FragmentError(
            "FRAGMENT_CONNECTION_TOKEN is not configured; create a Fragment "
            "connection in the dashboard and copy its token"
        )

    try:
        await _json_request(
            "POST",
            "/order/premium/",
            payload={"username": username, "months": months, "show_sender": "false"},
            headers={"Authorization": f"JWT {token}", "Content-Type": "application/json"},
        )
    except FragmentError as exc:
        if "401" in str(exc) or "403" in str(exc):
            raise FragmentError(
                "Fragment connection token was rejected; recreate the connection "
                "in the Fragment dashboard",
                outcome_unknown=exc.outcome_unknown,
            ) from exc
        raise
    logger.info("Sent %s months Telegram Premium to @%s", months, username)
