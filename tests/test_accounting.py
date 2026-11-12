"""Учёт после прихода, резерва, перемещения, отправки и приёмки (40% оценки)."""

import pytest

from app.core import inventory as inv
from app.core.errors import Shortage
from app.db.models import DocStatus
from tests.helpers import assert_reconciled, row, run, stock


def test_test_data_from_assignment(ids):
    expected = {"SKU-101": (10, 6), "SKU-102": (5, 0), "SKU-103": (0, 4)}
    for sku, (north, south) in expected.items():
        assert stock(ids.sku[sku], ids.wh["Север"])[0] == north
        assert stock(ids.sku[sku], ids.wh["Юг"])[0] == south
    assert_reconciled()


def test_receipt_adds_to_warehouse(ids):
    run(inv.create_receipt, ids.sku["SKU-102"], ids.wh["Юг"], 7)
    assert stock(ids.sku["SKU-102"], ids.wh["Юг"]) == (7, 0, 7)
    assert_reconciled()


def test_transfer_is_in_transit_until_received(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    res = run(inv.create_transfer, p, north, south, 3)

    assert stock(p, north) == (7, 0, 7)
    assert stock(p, south) == (6, 0, 6)  # ещё не доехало
    assert row("SKU-101").transit_ff == 3
    assert row("SKU-101").total == 16  # ни одна единица не потерялась
    assert_reconciled()

    run(inv.receive_transfer, res.document.id)
    assert stock(p, south) == (9, 0, 9)
    assert row("SKU-101").transit_ff == 0
    assert_reconciled()


def test_transfer_more_than_available_changes_nothing(ids):
    p = ids.sku["SKU-102"]
    with pytest.raises(Shortage):
        run(inv.create_transfer, p, ids.wh["Север"], ids.wh["Юг"], 6)
    assert stock(p, ids.wh["Север"]) == (5, 0, 5)
    assert_reconciled()


def test_fbs_reserve_blocks_goods_then_ships_to_customer(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    res = run(inv.create_fbs_order, p, north, 4, order_ref="WB-1001")
    assert res.document.status == DocStatus.RESERVED
    # Физически товар ещё на складе, но продать его второй раз нельзя.
    assert stock(p, north) == (10, 4, 6)
    assert_reconciled()

    run(inv.ship_fbs, res.document.id)
    assert stock(p, north) == (6, 0, 6)
    assert row("SKU-101").customer == 4
    assert_reconciled()


def test_fbs_cancel_returns_goods_to_available(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    res = run(inv.create_fbs_order, p, north, 4)
    run(inv.cancel_fbs, res.document.id)
    assert stock(p, north) == (10, 0, 10)
    assert_reconciled()


def test_reserved_goods_are_not_available_for_fbo(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    run(inv.create_fbs_order, p, north, 8)
    with pytest.raises(Shortage) as err:
        run(inv.create_fbo_shipment, p, north, 5)
    assert err.value.details["shortage"].available == 2
    assert stock(p, north) == (10, 8, 2)


def test_fbo_leaves_warehouse_and_is_in_transit_until_accepted(ids):
    p, south = ids.sku["SKU-103"], ids.wh["Юг"]
    res = run(inv.create_fbo_shipment, p, south, 3)
    assert stock(p, south) == (1, 0, 1)
    assert row("SKU-103").transit_mp == 3
    assert_reconciled()

    run(inv.accept_fbo, res.document.id)
    assert row("SKU-103").transit_mp == 0
    assert row("SKU-103").mp == 3
    assert_reconciled()


def test_full_day_scenario_reconciles_after_every_step(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    run(inv.create_receipt, p, north, 5)                          # Север 15, Юг 6
    assert_reconciled()
    transfer = run(inv.create_transfer, p, north, south, 4)       # Север 11, в пути 4
    assert_reconciled()
    run(inv.create_fbs_order, p, south, 6)                        # Юг: резерв 6
    assert_reconciled()
    run(inv.receive_transfer, transfer.document.id)               # Юг 10
    assert_reconciled()
    run(inv.create_fbo_shipment, p, north, 7)                     # Север 4, FBO в пути 7
    assert_reconciled()

    r = row("SKU-101")
    assert (r.cells[north].on_hand, r.cells[south].on_hand) == (4, 10)
    assert (r.cells[south].reserved, r.transit_ff, r.transit_mp) == (6, 0, 7)
    assert r.total == 21  # 16 из задания + 5 прихода
