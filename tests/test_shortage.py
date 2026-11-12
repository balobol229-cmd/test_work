"""Нехватка на выбранном ФФ при достаточном общем остатке."""

import pytest

from app.core import inventory as inv
from app.core.errors import Shortage
from app.db.models import DocStatus
from tests.helpers import assert_reconciled, row, run, stock


def test_fbs_order_waits_and_keeps_demand_when_chosen_ff_is_short(ids):
    # SKU-101: Север 10, Юг 6. Заказ на 12 с Севера.
    p = ids.sku["SKU-101"]
    res = run(inv.create_fbs_order, p, ids.wh["Север"], 12)

    assert res.document.status == DocStatus.WAITING
    assert res.shortage.available == 10
    assert res.shortage.total_available == 16
    kinds = [o.kind for o in res.shortage.options]
    assert kinds == ["split", "transfer"]  # целиком 12 нет ни на одном складе
    # Остатки не тронуты, но спрос виден на экране.
    assert stock(p, ids.wh["Север"]) == (10, 0, 10)
    assert row("SKU-101").waiting == 12
    assert_reconciled()


def test_split_reserves_all_parts_at_once(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    order = run(inv.create_fbs_order, p, north, 12, order_ref="WB-2001").document
    res = run(inv.split_fbs, order.id)

    assert (res.document.qty, res.document.status) == (10, DocStatus.RESERVED)
    assert [(d.qty, d.from_warehouse_id, d.order_ref) for d in res.parts] == [(2, south, "WB-2001")]
    assert stock(p, north) == (10, 10, 0)
    assert stock(p, south) == (6, 2, 4)
    assert row("SKU-101").waiting == 0
    assert_reconciled()


def test_switch_to_ff_that_has_everything(ids):
    # SKU-103: на Севере 0, на Юге 4.
    p, north, south = ids.sku["SKU-103"], ids.wh["Север"], ids.wh["Юг"]
    res = run(inv.create_fbs_order, p, north, 3)
    switch = res.shortage.options[0]
    assert (switch.kind, switch.warehouse_id) == ("switch", south)

    run(inv.reserve_fbs, res.document.id, warehouse_id=south)
    assert stock(p, south) == (4, 3, 1)
    assert_reconciled()


def test_transfer_option_then_reserve_after_receiving(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    order = run(inv.create_fbs_order, p, north, 12).document
    transfer = run(inv.create_transfer, p, south, north, 2).document
    with pytest.raises(Shortage):
        run(inv.reserve_fbs, order.id)  # перемещение ещё едет
    run(inv.receive_transfer, transfer.id)
    run(inv.reserve_fbs, order.id)
    assert stock(p, north) == (12, 12, 0)
    assert_reconciled()


def test_not_enough_anywhere_order_waits_for_receipt(ids):
    # SKU-102: всего 5. Заказ на 6.
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    res = run(inv.create_fbs_order, p, north, 6)
    assert res.shortage.options == []
    with pytest.raises(Shortage):
        run(inv.split_fbs, res.document.id)

    run(inv.create_receipt, p, north, 1)
    run(inv.reserve_fbs, res.document.id)
    assert stock(p, north) == (6, 6, 0)
    assert_reconciled()


def test_fbo_shortage_is_rejected_with_options_and_changes_nothing(ids):
    p, north = ids.sku["SKU-103"], ids.wh["Север"]
    with pytest.raises(Shortage) as err:
        run(inv.create_fbo_shipment, p, north, 2)
    info = err.value.details["shortage"]
    assert [o.kind for o in info.options] == ["switch", "transfer"]
    assert row("SKU-103").transit_mp == 0
    assert_reconciled()


def test_waiting_order_can_be_cancelled(ids):
    res = run(inv.create_fbs_order, ids.sku["SKU-102"], ids.wh["Юг"], 1)
    run(inv.cancel_fbs, res.document.id)
    assert row("SKU-102").waiting == 0
    assert_reconciled()
