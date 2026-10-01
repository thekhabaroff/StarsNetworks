"""Настройки ссылок и справочных разделов, доступных пользователям."""
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Setting


DEFAULT_COMMUNITY_SOURCE = ""
DEFAULT_SUPPORT_SOURCE = ""

INTERACTION_KEYS = {
    "community": {
        "enabled": "interaction.community.enabled",
        "source": "interaction.community.source",
    },
    "support": {
        "enabled": "interaction.support.enabled",
        "source": "interaction.support.source",
    },
    "rules": {
        "enabled": "interaction.rules.enabled",
        "source": "interaction.rules.source",
    },
    "privacy": {
        "enabled": "interaction.privacy.enabled",
        "source": "interaction.privacy.source",
    },
    "faq": {
        "enabled": "interaction.faq.enabled",
    },
}


@dataclass(frozen=True)
class InteractionConfig:
    community_enabled: bool
    community_source: str
    support_enabled: bool
    support_source: str
    rules_enabled: bool
    rules_source: str
    privacy_enabled: bool
    privacy_source: str
    faq_enabled: bool


def normalize_external_source(value: str) -> str:
    """Проверить и привести ссылку на Telegram или внешний сайт к единому виду."""
    source = value.strip()
    if source.startswith("@"):
        source = f"https://t.me/{source[1:]}"
    elif source.lower().startswith("t.me/"):
        source = f"https://{source}"

    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https", "tg"}:
        raise ValueError("Укажите ссылку в формате https://..., t.me/... или @username.")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise ValueError("В ссылке отсутствует адрес сайта или Telegram-канала.")
    if parsed.scheme == "tg" and not (parsed.netloc or parsed.path):
        raise ValueError("Некорректная Telegram-ссылка.")
    return source


async def _setting_value(session: AsyncSession, key: str, default: str = "") -> str:
    setting = await session.scalar(select(Setting).where(Setting.key == key))
    return setting.value if setting and setting.value is not None else default


async def get_interaction_config(session: AsyncSession) -> InteractionConfig:
    """Получить настройки с безопасными значениями, сохраняющими старое поведение."""
    def enabled(value: str) -> bool:
        return value.strip().lower() not in {"0", "false", "off", "no"}

    community_enabled = enabled(await _setting_value(
        session, INTERACTION_KEYS["community"]["enabled"], "true"
    ))
    support_enabled = enabled(await _setting_value(
        session, INTERACTION_KEYS["support"]["enabled"], "true"
    ))
    rules_enabled = enabled(await _setting_value(
        session, INTERACTION_KEYS["rules"]["enabled"], "true"
    ))
    faq_enabled = enabled(await _setting_value(
        session, INTERACTION_KEYS["faq"]["enabled"], "true"
    ))
    # Новая политика скрыта, пока владелец не добавит источник и явно не
    # включит её в пункте управления.
    privacy_enabled = enabled(await _setting_value(
        session, INTERACTION_KEYS["privacy"]["enabled"], "false"
    ))
    return InteractionConfig(
        community_enabled=community_enabled,
        community_source=(
            os.environ.get("COMMUNITY_ID", "").strip()
            or await _setting_value(
                session, INTERACTION_KEYS["community"]["source"], DEFAULT_COMMUNITY_SOURCE
            )
        ),
        support_enabled=support_enabled,
        support_source=(
            os.environ.get("SUPPORT_ID", "").strip()
            or await _setting_value(
                session, INTERACTION_KEYS["support"]["source"], DEFAULT_SUPPORT_SOURCE
            )
        ),
        rules_enabled=rules_enabled,
        rules_source=(
            os.environ.get("TERMS_OF_SERVICE", "").strip()
            or await _setting_value(session, INTERACTION_KEYS["rules"]["source"])
        ),
        privacy_enabled=privacy_enabled,
        privacy_source=(
            os.environ.get("PRIVACY_POLICY", "").strip()
            or await _setting_value(session, INTERACTION_KEYS["privacy"]["source"])
        ),
        faq_enabled=faq_enabled,
    )


async def save_interaction_setting(session: AsyncSession, key: str, value: str) -> None:
    """Сохранить одну настройку взаимодействия без дубликатов."""
    setting = await session.scalar(select(Setting).where(Setting.key == key))
    if setting:
        setting.value = value
    else:
        session.add(Setting(key=key, value=value))
