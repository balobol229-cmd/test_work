"""Повторное подтверждение не списывает товар дважды, в том числе при одновременных запросах."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core import inventory as inv
from app.core.errors import IdempotencyConflict, InvalidOperation
from app.db.models import DocStatus
from tests.helpers import assert_reconciled, row, run, stock


def test_ship_twice_writes_off_once(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    order = run(inv.create_fbs_order, p, north, 3).document
    assert not run(inv.ship_fbs, order.id).replayed
    assert run(inv.ship_fbs, order.id).replayed
    assert stock(p, north) == (7, 0, 7)
    assert row("SKU-101").customer == 3
    assert_reconciled()


def test_receive_and_accept_twice(ids):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    t = run(inv.create_transfer, p, north, south, 2).document
    run(inv.receive_transfer, t.id)
    assert run(inv.receive_transfer, t.id).replayed
    assert stock(p, south) == (8, 0, 8)

    f = run(inv.create_fbo_shipment, p, north, 1).document
    run(inv.accept_fbo, f.id)
    assert run(inv.accept_fbo, f.id).replayed
    assert row("SKU-101").mp == 1
    assert_reconciled()


def test_cancel_twice_and_ship_after_cancel(ids):
    order = run(inv.create_fbs_order, ids.sku["SKU-101"], ids.wh["Север"], 3).document
    run(inv.cancel_fbs, order.id)
    assert run(inv.cancel_fbs, order.id).replayed
    with pytest.raises(InvalidOperation):
        run(inv.ship_fbs, order.id)
    assert stock(ids.sku["SKU-101"], ids.wh["Север"]) == (10, 0, 10)


def test_same_key_creates_one_document(ids):
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    a = run(inv.create_receipt, p, north, 4, key="form-123")
    b = run(inv.create_receipt, p, north, 4, key="form-123")
    assert b.replayed and a.document.id == b.document.id
    assert stock(p, north)[0] == 9
    with pytest.raises(IdempotencyConflict):
        run(inv.create_receipt, p, north, 5, key="form-123")
    assert_reconciled()


def _parallel(n: int, fn):
    barrier = Barrier(n)

    def go(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 — нам нужен сам результат каждого потока
            return exc

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(go, range(n)))


def test_concurrent_ship_writes_off_once(ids):
    p, north = ids.sku["SKU-101"], ids.wh["Север"]
    order = run(inv.create_fbs_order, p, north, 5).document
    results = _parallel(4, lambda _: run(inv.ship_fbs, order.id))
    assert sorted(r.replayed for r in results) == [False, True, True, True]
    assert stock(p, north) == (5, 0, 5)
    assert_reconciled()


def test_concurrent_orders_cannot_reserve_same_units(ids):
    # SKU-102: на Севере 5. Два заказа по 4 одновременно — хватит только на один.
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    results = _parallel(2, lambda _: run(inv.create_fbs_order, p, north, 4))
    statuses = sorted(r.document.status for r in results)
    assert statuses == [DocStatus.RESERVED, DocStatus.WAITING]
    assert stock(p, north) == (5, 4, 1)
    assert_reconciled()


def test_concurrent_double_submit_same_key(ids):
    p, north = ids.sku["SKU-102"], ids.wh["Север"]
    results = _parallel(3, lambda _: run(inv.create_receipt, p, north, 2, key="double-click"))
    assert len({r.document.id for r in results}) == 1
    assert stock(p, north)[0] == 7
    assert_reconciled()


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_accept_and_delete_fbo_apply_only_one(ids, attempt):
    """Регрессия: одновременные «Принять» и «Удалить» не должны провести движение дважды."""
    p, south = ids.sku["SKU-103"], ids.wh["Юг"]
    f = run(inv.create_fbo_shipment, p, south, 2).document
    results = _parallel(2, lambda i: run(inv.accept_fbo if i == 0 else inv.delete_document, f.id))
    applied = [r for r in results if not isinstance(r, Exception)]
    assert len(applied) >= 1
    r = row("SKU-103")
    assert (r.transit_mp, r.mp + stock(p, south)[0]) == (0, 4)
    assert_reconciled()


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_receive_and_delete_transfer_apply_only_one(ids, attempt):
    p, north, south = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    t = run(inv.create_transfer, p, north, south, 3).document
    _parallel(2, lambda i: run(inv.receive_transfer if i == 0 else inv.delete_document, t.id))
    assert stock(p, north)[0] + stock(p, south)[0] == 16
    assert row("SKU-101").transit_ff == 0
    assert_reconciled()
