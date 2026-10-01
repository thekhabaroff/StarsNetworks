# syntax=docker/dockerfile:1

# The application is intentionally packaged as an immutable image. Runtime
# configuration and secrets are supplied by Docker Compose through .env.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./
# SQLAlchemy's async engine (used both by the bot and Alembic) requires
# greenlet at runtime.  Keep requirements.txt in the deliberately minimal
# form requested for the project; the immutable production image installs the
# matching latest runtime helper explicitly.
RUN pip install --no-cache-dir -r requirements.txt greenlet

COPY . ./
RUN chmod 0555 /app/start.sh

# Explicitly run the container as root, as required for this deployment.
USER root

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PAYMENT_WEBHOOK_PORT', '18743'), timeout=3).read()" || exit 1

ENTRYPOINT ["/app/start.sh", "--container"]
CMD ["python", "-u", "bot.py"]
