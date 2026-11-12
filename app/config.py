import os


def _database_url() -> str:
    # Локально: PostgreSQL на localhost. В Docker и на хостинге адрес задаёт переменная окружения.
    url = os.getenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/checkstock")
    # Хостинги (Render и др.) отдают адрес вида postgres://… или postgresql://…,
    # а SQLAlchemy нужно явно указать драйвер psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


DATABASE_URL = _database_url()
