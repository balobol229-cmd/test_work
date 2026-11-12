"""Уровни просмотра из ТЗ Ракеты: итоги по магазинам, категориям и артикулам согласованы, двойного учёта нет."""

import pytest
from fastapi.testclient import TestClient

from app.core import inventory as inv
from app.core import views
from app.db.session import SessionLocal
from app.main import app
from tests.helpers import run

COLUMNS = ["transit_ff", "transit_mp", "mp", "customer", "waiting", "total"]


def _busy_day(ids) -> None:
    p1, p2, p3 = ids.sku["SKU-101"], ids.sku["SKU-102"], ids.sku["SKU-103"]
    n, s = ids.wh["Север"], ids.wh["Юг"]
    run(inv.create_transfer, p1, n, s, 3)
    run(inv.ship_fbs, run(inv.create_fbs_order, p2, n, 2).document.id)
    run(inv.create_fbs_order, p1, s, 4)
    run(inv.accept_fbo, run(inv.create_fbo_shipment, p3, s, 2).document.id)
    run(inv.create_fbo_shipment, p1, n, 1)
    run(inv.create_fbs_order, p3, n, 1)  # ждёт товара


@pytest.mark.parametrize("busy", [False, True])
def test_all_levels_add_up_to_the_same_totals(ids, busy):
    if busy:
        _busy_day(ids)
    with SessionLocal() as s:
        o = views.stock_overview(s)
    by_level = {level: views.group_rows(o, level) for level in views.LEVELS}

    for level, groups in by_level.items():
        for col in COLUMNS:
            assert sum(getattr(g, col) for g in groups) == o.total(col), (level, col)
        for w in o.warehouses:
            assert sum(g.cell(w.id).on_hand for g in groups) == sum(r.cells[w.id].on_hand for r in o.rows)
            assert sum(g.cell(w.id).reserved for g in groups) == sum(r.cells[w.id].reserved for r in o.rows)
        # Каждый артикул ровно в одной группе.
        skus = [r.product.sku for g in groups for r in g.rows]
        assert sorted(skus) == sorted(r.product.sku for r in o.rows), level


def test_levels_shape(ids):
    with SessionLocal() as s:
        o = views.stock_overview(s)
    assert [g.label for g in views.group_rows(o, "store")] == ["Магазин 01"]
    assert {g.label: g.total for g in views.group_rows(o, "category")} == {
        "Органайзеры": 16,
        "Контейнеры и корзины": 9,
    }
    assert len(views.group_rows(o, "sku")) == 3


def test_levels_api_and_page(ids):
    client = TestClient(app)
    totals = {
        level: sum(g["total"] for g in client.get("/api/stock/levels", params={"level": level}).json()["groups"])
        for level in views.LEVELS
    }
    assert set(totals.values()) == {25}
    assert client.get("/api/stock/levels", params={"level": "bogus"}).status_code == 422
    for level in [*views.LEVELS, "bogus", "<script>"]:
        assert client.get("/", params={"level": level}).status_code == 200
