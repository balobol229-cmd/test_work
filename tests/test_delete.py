"""«Удалить» в интерфейсе — отмена обратным движением. История не стирается, сверка сходится."""

import pytest

from app.core import inventory as inv
from app.core import views
from app.core.errors import InvalidOperation
from app.db.models import DocStatus
from app.db.session import SessionLocal
from tests.helpers import assert_reconciled, row, run, stock


def test_delete_wrong_receipt(ids):
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    wrong = run(inv.create_receipt, p, north, 66661).document
    res = run(inv.delete_document, wrong.id)

    assert res.document.status == DocStatus.CANCELLED
    assert stock(p, north) == (5, 0, 5)
    with SessionLocal() as s:
        actions = [m.action for m in views.history(s, p)]
    assert actions[:2] == ["receipt_cancel", "receipt"]  # обе записи остались в истории
    assert_reconciled()
    assert run(inv.delete_document, wrong.id).replayed


def test_cannot_delete_receipt_already_reserved(ids):
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    receipt = run(inv.create_receipt, p, north, 3).document
    run(inv.create_fbs_order, p, north, 7)  # свободно останется 1
    with pytest.raises(InvalidOperation):
        run(inv.delete_document, receipt.id)
    assert_reconciled()


def test_delete_transfer_in_transit_returns_goods(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    t = run(inv.create_transfer, p, north, south, 4).document
    run(inv.delete_document, t.id)
    assert stock(p, north) == (10, 0, 10)
    assert row("SKU-101").transit_ff == 0
    assert_reconciled()


def test_cannot_delete_received_transfer(ids):
    t = run(inv.create_transfer, ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"], 1).document
    run(inv.receive_transfer, t.id)
    with pytest.raises(InvalidOperation):
        run(inv.delete_document, t.id)


def test_delete_fbo_in_transit_returns_goods(ids):
    p, south = ids.sku["SKU-103"], ids.wh["Юг"]
    f = run(inv.create_fbo_shipment, p, south, 3).document
    run(inv.delete_document, f.id)
    assert stock(p, south) == (4, 0, 4)
    assert row("SKU-103").transit_mp == 0
    assert_reconciled()


def test_delete_fbs_order_releases_reserve(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    order = run(inv.create_fbs_order, p, north, 5).document
    run(inv.delete_document, order.id)
    assert stock(p, north) == (10, 0, 10)
    assert_reconciled()
