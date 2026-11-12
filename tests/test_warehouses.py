"""Склады ФФ: добавление, переименование, удаление с остатком."""

import pytest

from app.core import inventory as inv
from app.core import views
from app.core.errors import InvalidOperation, ValidationFailed, WarehouseBusy
from app.db.session import SessionLocal
from tests.helpers import assert_reconciled, row, run, stock


def test_create_and_rename(ids):
    wh = run(inv.create_warehouse, "  Центр ")
    assert wh.name == "Центр"
    run(inv.rename_warehouse, wh.id, "Центр-2")
    with pytest.raises(ValidationFailed):
        run(inv.create_warehouse, "север")  # имя занято, регистр не важен
    with pytest.raises(ValidationFailed):
        run(inv.rename_warehouse, wh.id, "Юг")


def test_rename_keeps_history(ids):
    run(inv.rename_warehouse, ids.wh["Север"], "Север-1")
    with SessionLocal() as s:
        moves = views.history(s)
        labels = {views.place_label(m.to_loc, m.to_warehouse) for m in moves}
    assert "Север-1" in labels


def test_delete_with_stock_keeps_stock_and_history(ids):
    p, south = ids.sku["SKU-101"], ids.wh["Юг"]
    res = run(inv.archive_warehouse, south)
    assert ("SKU-101", 6) in res.left_on_hand

    # Склад исчез из активных, но его остаток виден и учитывается.
    with SessionLocal() as s:
        overview = views.stock_overview(s)
        archived = next(w for w in overview.warehouses if w.id == south)
        assert not archived.is_active
        assert any(m.to_warehouse_id == south for m in views.history(s))
    assert row("SKU-101").cells[south].on_hand == 6
    assert row("SKU-101").total == 16
    assert_reconciled()

    # Принимать на удалённый склад нельзя, вывезти остаток можно.
    with pytest.raises(InvalidOperation):
        run(inv.create_receipt, p, south, 1)
    with pytest.raises(InvalidOperation):
        run(inv.create_fbs_order, p, south, 1)
    t = run(inv.create_transfer, p, south, ids.wh["Север"], 6).document
    run(inv.receive_transfer, t.id)
    assert stock(p, ids.wh["Север"]) == (16, 0, 16)
    assert_reconciled()


def test_deleted_name_can_be_reused(ids):
    run(inv.archive_warehouse, ids.wh["Юг"])
    new = run(inv.create_warehouse, "Юг")
    assert new.id != ids.wh["Юг"]


def test_delete_blocked_by_reserve(ids):
    order = run(inv.create_fbs_order, ids.sku["SKU-101"], ids.wh["Юг"], 2).document
    with pytest.raises(WarehouseBusy) as err:
        run(inv.archive_warehouse, ids.wh["Юг"])
    assert "резерв" in err.value.message
    run(inv.ship_fbs, order.id)
    run(inv.archive_warehouse, ids.wh["Юг"])


def test_delete_blocked_by_incoming_transfer(ids):
    run(inv.create_transfer, ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"], 1)
    with pytest.raises(WarehouseBusy):
        run(inv.archive_warehouse, ids.wh["Юг"])


def test_delete_blocked_by_waiting_order(ids):
    run(inv.create_fbs_order, ids.sku["SKU-102"], ids.wh["Юг"], 1)
    with pytest.raises(WarehouseBusy):
        run(inv.archive_warehouse, ids.wh["Юг"])


def test_shortage_options_skip_deleted_warehouse(ids):
    run(inv.archive_warehouse, ids.wh["Юг"])
    res = run(inv.create_fbs_order, ids.sku["SKU-103"], ids.wh["Север"], 1)
    assert res.shortage.total_available == 0
    assert res.shortage.options == []


def test_delete_twice_is_safe(ids):
    run(inv.archive_warehouse, ids.wh["Юг"])
    assert run(inv.archive_warehouse, ids.wh["Юг"]).replayed
