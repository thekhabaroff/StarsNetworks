"""Создание незашифрованного PostgreSQL-дампа из Docker Compose.

Это вспомогательная команда для Docker-развёртывания проекта. Дамп намеренно
не шифруется согласно текущему требованию: доступ к каталогу ``backups`` должен
быть ограничен правами ОС и не должен передаваться третьим лицам.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys


BACKUP_DIR = Path("backups")
KEEP_LAST = 10


def backup_postgres() -> bool:
    """Выполнить pg_dump в контейнере ``db`` и сохранить SQL-дамп на хосте."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"starsnetworks_{timestamp}.sql"

    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"',
    ]
    try:
        with backup_file.open("wb") as output:
            result = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
    except FileNotFoundError:
        print("❌ Docker Compose не найден. Установите Docker Engine и Compose v2.")
        return False
    except OSError as exc:
        print(f"❌ Не удалось создать дамп: {exc}")
        return False

    if result.returncode != 0:
        backup_file.unlink(missing_ok=True)
        error = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"❌ pg_dump завершился с ошибкой: {error or result.returncode}")
        return False

    for old_backup in sorted(BACKUP_DIR.glob("starsnetworks_*.sql"))[:-KEEP_LAST]:
        old_backup.unlink()

    size_kb = backup_file.stat().st_size / 1024
    print(f"✅ Дамп создан: {backup_file} ({size_kb:.1f} KiB)")
    return True


def list_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("starsnetworks_*.sql"), reverse=True)
    if not backups:
        print("📭 Резервных копий не найдено")
        return

    for index, backup in enumerate(backups, 1):
        size_kb = backup.stat().st_size / 1024
        modified = datetime.fromtimestamp(backup.stat().st_mtime)
        print(f"{index}. {backup.name} — {size_kb:.1f} KiB, {modified:%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        list_backups()
    else:
        backup_postgres()
