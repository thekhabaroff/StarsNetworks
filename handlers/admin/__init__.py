"""Сборщик модульной админ-панели.

Обработчики разделены по предметным областям, но используют общий router,
FSM и middleware из handlers.admin_context. Импорт этого модуля сохраняет
прежний контракт handlers.admin.router для bot.py.
"""

from .admin_context import *
from .admin_core import *
from .admin_catalog import *
from .admin_stats import *
from .admin_ledger_users import *
from .admin_products import *
from .admin_payments import *
from .admin_discounts import *
from .admin_referrals import *
from .admin_accounts_roles import *

__all__ = [name for name in globals() if not name.startswith("__")]
