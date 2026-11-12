from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from app.core import views
from app.db.models import Stock
from app.db.session import SessionLocal


def run(fn: Callable, *args, **kwargs):
    """Выполняет операцию ядра в отдельной транзакции, как это делает веб-слой."""
    with SessionLocal.begin() as s:
        return fn(s, *args, **kwargs)


def stock(product_id: int, warehouse_id: int) -> tuple[int, int, int]:
    """(физически, в резерве, доступно)."""
    with SessionLocal() as s:
        st = s.scalar(select(Stock).where(Stock.product_id == product_id, Stock.warehouse_id == warehouse_id))
        return (st.on_hand, st.reserved, st.available) if st else (0, 0, 0)


def row(sku: str) -> views.Row:
    with SessionLocal() as s:
        return next(r for r in views.stock_overview(s).rows if r.product.sku == sku)


def assert_reconciled() -> None:
    with SessionLocal() as s:
        rec = views.reconcile(s)
    problems = [p for c in rec.checks for p in c.problems]
    assert rec.ok, problems
