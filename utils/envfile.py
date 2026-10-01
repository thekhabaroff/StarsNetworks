"""Безопасное обновление разрешённых значений в смонтированном .env."""
import os
from pathlib import Path
from typing import Iterable


INTERACTION_ENV_KEYS = {
    "community": "COMMUNITY_ID",
    "support": "SUPPORT_ID",
    "rules": "TERMS_OF_SERVICE",
    "privacy": "PRIVACY_POLICY",
}

DEFAULT_INTERACTION_SOURCES = {
    "SUPPORT_ID": "",
    "COMMUNITY_ID": "",
}

PAYMENT_ENABLED_ENV_KEYS = frozenset({
    "PAYMENT_YOOMONEY_ENABLED",
    "PAYMENT_YOOKASSA_ENABLED",
    "PAYMENT_HELEKET_ENABLED",
    "PAYMENT_LAVA_ENABLED",
    "PAYMENT_CRYPTOBOT_ENABLED",
    "PAYMENT_STARS_ENABLED",
})


def _env_path() -> Path:
    configured = Path(os.environ.get("ENV_FILE_PATH", "/app/.env"))
    if configured.exists():
        return configured
    return Path(".env")


def _save_env_value(key: str, value: str, section: str) -> None:
    """Точечно обновить разрешённую однострочную переменную в .env.

    Контейнер получает файл как writable bind mount, поэтому содержимое
    перезаписывается через тот же inode, без атомарного rename. Все остальные
    переменные и комментарии остаются без изменений.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("Значение не должно содержать переносы строк.")

    path = _env_path()
    if not path.exists():
        # Keep compatibility with deployments that provide secrets only via
        # ``env_file`` and do not mount the host .env. The setting remains live
        # for this process; the next restart reads the host .env again.
        os.environ[key] = value
        return

    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    replacement = f"{prefix}{value}"
    replaced = False
    output = []
    for line in lines:
        if line.startswith(prefix):
            output.append(replacement)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and output[-1]:
            output.append("")
        output.append(f"# {section}")
        output.append(replacement)

    # Запись поверх уже существующего bind mount сохраняет связь с файлом на
    # хосте; атомарный rename здесь использовать нельзя.
    with path.open("r+", encoding="utf-8") as env_file:
        env_file.seek(0)
        env_file.write("\n".join(output) + "\n")
        env_file.truncate()
        env_file.flush()
        os.fsync(env_file.fileno())

    # Docker не обновляет process environment на лету, поэтому синхронизируем
    # текущее приложение. После следующего перезапуска Compose прочитает .env.
    os.environ[key] = value


def save_interaction_source_to_env(kind: str, value: str) -> None:
    """Записать источник взаимодействия в .env без затрагивания секретов."""
    key = INTERACTION_ENV_KEYS.get(kind)
    if not key:
        raise ValueError("Этот раздел не имеет переменной окружения.")
    _save_env_value(key, value, "INTERACTION")


def save_payment_enabled_to_env(field: str, enabled: bool) -> None:
    """Сохранить флаг доступности оплаты в .env и текущем окружении."""
    if field not in PAYMENT_ENABLED_ENV_KEYS:
        raise ValueError("Неизвестный флаг платёжной системы.")
    _save_env_value(field, "true" if enabled else "false", "PAYMENT FLAGS")


def save_admin_ids_to_env(user_ids: Iterable[int]) -> str:
    """Сохранить полный список администраторов и вернуть его строковое значение."""
    normalized_ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    value = ",".join(str(user_id) for user_id in normalized_ids)
    _save_env_value("ADMIN_IDS", value, "TELEGRAM")
    return value


def ensure_default_interaction_sources() -> None:
    """Перенести исторические стандартные ссылки в .env при первом запуске."""
    for key, value in DEFAULT_INTERACTION_SOURCES.items():
        if os.environ.get(key, "").strip():
            continue
        kind = next(kind for kind, env_key in INTERACTION_ENV_KEYS.items() if env_key == key)
        save_interaction_source_to_env(kind, value)
