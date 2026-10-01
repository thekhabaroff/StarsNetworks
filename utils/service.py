"""Сервис выдачи аккаунтов"""
from io import BytesIO
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from database.models import Account, Product
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


async def reserve_accounts(
    session: AsyncSession,
    product_id: int,
    quantity: int,
    order_id: int = None
) -> List[Account]:
    """
    Резервирование аккаунтов для заказа
    Использует SELECT FOR UPDATE для предотвращения race condition
    order_id может быть None при первоначальном резервировании
    Работает с уже переданной сессией (без создания вложенной транзакции)
    """
    # Блокируем строки для обновления (SELECT FOR UPDATE)
    stmt = (
        select(Account)
        .where(
            Account.product_id == product_id,
            Account.is_sold == False,
            Account.is_blocked == False,
        )
        .limit(quantity)
        .with_for_update()
    )
    result = await session.execute(stmt)
    accounts = result.scalars().all()

    if len(accounts) < quantity:
        raise ValueError(f"Недостаточно товара на складе. Доступно: {len(accounts)}, требуется: {quantity}")

    # Проверяем stock_count перед обновлением
    stmt_product = select(Product).where(Product.id == product_id).with_for_update()
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    if not product:
        raise ValueError(f"Товар с ID {product_id} не найден")

    if product.stock_count < quantity:
        raise ValueError(f"Недостаточно товара на складе. Доступно: {product.stock_count}, требуется: {quantity}")

    # Резервируем аккаунты
    account_ids = [acc.id for acc in accounts]
    update_values = {
        "is_sold": True,
        "sold_at": datetime.now()
    }
    if order_id:
        update_values["order_id"] = order_id

    await session.execute(
        update(Account)
        .where(Account.id.in_(account_ids))
        .values(**update_values)
    )

    # Обновляем количество на складе
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(stock_count=Product.stock_count - quantity)
    )

    return accounts


async def get_accounts_for_order(session: AsyncSession, order_id: int) -> List[Account]:
    """Получить состав заказа из БД в стабильном порядке."""
    stmt = (
        select(Account)
        .where(Account.order_id == order_id)
        .order_by(Account.id)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def create_accounts_file(accounts: List[Account]) -> BytesIO:
    """
    Создать текстовый файл с аккаунтами
    Каждый аккаунт на отдельной строке в формате логин:пароль
    """
    file_content = "\n".join([acc.account_data for acc in accounts])
    file_obj = BytesIO(file_content.encode('utf-8'))
    file_obj.name = f"starsnetworks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return file_obj


async def upload_accounts_from_file(
    session: AsyncSession,
    product_id: int,
    file_content: str
) -> tuple[int, int]:
    """
    Загрузить аккаунты из файла (поддерживаются TXT и CSV).

    Форматы:
    - TXT: каждая строка = один аккаунт (например: login:password)
    - CSV: каждая строка = один аккаунт, колонки:
        login;password[;комментарий...]
      Все непустые колонки объединяются через ":" и сохраняются в поле account_data.

    Возвращает: (успешно загружено, пропущено дублей)
    """
    text = file_content.strip()
    lines: list[str] = []

    # Пытаемся разобрать как CSV (поддержка ; , | и табов)
    try:
        import csv
        import io

        if not text:
            return 0, 0

        sample = text[:1024]
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t,")
        reader = csv.reader(io.StringIO(text), dialect)

        for row in reader:
            # Убираем пустые колонки
            cols = [col.strip() for col in row if col and col.strip()]
            if not cols:
                continue

            if len(cols) == 1:
                normalized = cols[0]
            else:
                # login:password:comment...
                normalized = ":".join(cols)

            lines.append(normalized)
    except (csv.Error, UnicodeError, ValueError) as exc:
        logger.debug("CSV parsing failed; falling back to line-based import: %s", exc)
        # Если не получилось распознать CSV — работаем построчно как с обычным TXT
        lines = [ln.strip() for ln in text.split('\n') if ln.strip()]

    unique_accounts = set()
    duplicates = 0
    loaded = 0

    # Получаем существующие аккаунты для проверки дублей (включая проданные и заблокированные)
    stmt = select(Account.account_data).where(Account.product_id == product_id)
    result = await session.execute(stmt)
    existing_accounts = set(result.scalars().all())

    # Работаем с уже переданной сессией (без создания вложенной транзакции)
    for normalized in lines:
        # Проверка на дубликаты
        if normalized in unique_accounts or normalized in existing_accounts:
            duplicates += 1
            continue

        unique_accounts.add(normalized)

        # Создаем запись аккаунта
        account = Account(
            product_id=product_id,
            account_data=normalized,
            is_sold=False
        )
        session.add(account)
        loaded += 1

    # Обновляем количество на складе
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(stock_count=Product.stock_count + loaded)
    )

    return loaded, duplicates
