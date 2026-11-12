"""Тесты идут на настоящем PostgreSQL: учёт держится на транзакциях, блокировках и
CHECK-ограничениях, и мок базы проверил бы не то.

База для тестов — `<имя основной базы>_test`. Она создаётся сама, схема накатывается
миграциями Alembic, перед каждым тестом данные очищаются и заводятся заново.
"""

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_base = make_url(os.environ.get("DATABASE_URL", "postgresql+psycopg://localhost:5432/checkstock"))
_test = _base.set(database=f"{_base.database}_test")
os.environ["DATABASE_URL"] = _test.render_as_string(hide_password=False)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.models import Product, Warehouse  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.sources.seed import reset, seed  # noqa: E402


def _create_database() -> None:
    admin = create_engine(_base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": _test.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{_test.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def schema():
    _create_database()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config("alembic.ini")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def data(schema):
    with SessionLocal.begin() as s:
        reset(s)
        seed(s)


class Ids:
    """Короткий доступ к тестовым товарам и складам: ids.sku["SKU-101"], ids.wh["Север"]."""

    def __init__(self) -> None:
        with SessionLocal() as s:
            self.sku = dict(s.execute(select(Product.sku, Product.id)).all())
            self.wh = dict(s.execute(select(Warehouse.name, Warehouse.id)).all())


@pytest.fixture
def ids() -> Ids:
    return Ids()
