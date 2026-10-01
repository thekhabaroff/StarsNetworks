"""Логирование"""
import logging
import sys

# Docker collects stdout/stderr and applies the configured size/retention
# policy.  Avoid a local FileHandler so the application works in a read-only
# container under a non-root user.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)


async def log_error_to_db(session, level: str, message: str, user_id: int = None, traceback: str = None):
    """Записать ошибку в БД"""
    try:
        from database.models import Log
        log_entry = Log(
            level=level,
            message=message,
            user_id=user_id,
            traceback=traceback
        )
        session.add(log_entry)
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to log to database: {e}")
