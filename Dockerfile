FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# requirements.txt собран из uv.lock: `uv export --no-hashes --format requirements-txt`.
# В образ входят и pytest с httpx, чтобы тесты запускались той же командой в контейнере.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml alembic.ini ./
COPY migrations migrations
COPY app app
COPY tests tests

EXPOSE 8000

# Миграции → тестовые данные (только в пустую базу) → сервер.
CMD ["sh", "-c", "alembic upgrade head && python -m app.sources.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
