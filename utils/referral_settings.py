"""Настройки реферальной программы, хранимые в базе данных."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Setting


REFERRAL_LEVELS_KEY = "referral.levels"
REFERRAL_CONDITION_KEY = "referral.condition"
REFERRAL_REWARD_PERCENT_KEY = "referral.reward_percent"
REFERRAL_CASHBACK_ENABLED_KEY = "referral.cashback_enabled"
REFERRAL_CASHBACK_PERCENT_KEY = "referral.cashback_percent"
REFERRAL_INVITATION_TEXT_KEY = "referral.invitation_text"

REFERRAL_SETTING_KEYS = (
    REFERRAL_LEVELS_KEY,
    REFERRAL_CONDITION_KEY,
    REFERRAL_REWARD_PERCENT_KEY,
    REFERRAL_CASHBACK_ENABLED_KEY,
    REFERRAL_CASHBACK_PERCENT_KEY,
    REFERRAL_INVITATION_TEXT_KEY,
)

DEFAULT_INVITATION_TEXT = (
    "Присоединяйся к Stars Networks и покупай Telegram Stars!"
)
DEFAULT_REFERRAL_REWARD_PERCENT = Decimal("10")


@dataclass(frozen=True)
class ReferralProgramConfig:
    levels: int
    condition: str
    reward_percent: Decimal
    cashback_enabled: bool
    cashback_percent: Decimal
    invitation_text: str

    @property
    def condition_label(self) -> str:
        return "первый платёж" if self.condition == "first" else "каждый платёж"


def _percent(value: str | None, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if not parsed.is_finite():
        return default
    return min(Decimal("100"), max(Decimal("0"), parsed))


async def get_referral_program_config(session: AsyncSession) -> ReferralProgramConfig:
    """Получить нормализованные настройки с безопасными значениями по умолчанию."""
    result = await session.execute(
        select(Setting).where(Setting.key.in_(REFERRAL_SETTING_KEYS))
    )
    values = {setting.key: setting.value for setting in result.scalars()}
    levels = 2 if values.get(REFERRAL_LEVELS_KEY) == "2" else 1
    condition = "first" if values.get(REFERRAL_CONDITION_KEY) == "first" else "every"
    invitation_text = (values.get(REFERRAL_INVITATION_TEXT_KEY) or DEFAULT_INVITATION_TEXT).strip()
    if not invitation_text:
        invitation_text = DEFAULT_INVITATION_TEXT
    return ReferralProgramConfig(
        levels=levels,
        condition=condition,
        reward_percent=_percent(
            values.get(REFERRAL_REWARD_PERCENT_KEY), DEFAULT_REFERRAL_REWARD_PERCENT
        ),
        cashback_enabled=values.get(REFERRAL_CASHBACK_ENABLED_KEY) == "true",
        cashback_percent=_percent(values.get(REFERRAL_CASHBACK_PERCENT_KEY), Decimal("0")),
        invitation_text=invitation_text[:3500],
    )


async def save_referral_setting(
    session: AsyncSession, key: str, value: str
) -> None:
    """Сохранить одну настройку без самостоятельного коммита транзакции."""
    if key not in REFERRAL_SETTING_KEYS:
        raise ValueError("Неизвестная настройка реферальной программы")
    result = await session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        session.add(Setting(key=key, value=value))
    else:
        setting.value = value


def render_invitation_text(template: str, referral_link: str) -> str:
    """Подготовить текст для Telegram share без дублирования ссылки.

    Ссылка передаётся Telegram отдельным параметром URL и приложение добавляет
    её к сообщению самостоятельно. Убираем также старый шаблонный маркер,
    чтобы приглашения, сохранённые до этого изменения, не дублировали ссылку.
    """
    return " ".join(
        template.replace("{referral_link}", "").replace(referral_link, "").split()
    )
