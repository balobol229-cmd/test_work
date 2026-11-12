"""Случайные сценарии (property-based, Hypothesis).

Робот выполняет тысячи случайных последовательностей операций: приходы, перемещения, заказы,
разделения, отгрузки, отмены, удаления, новые и удалённые склады. Рядом он ведёт свою
«теневую» модель склада — простой словарь, независимый от кода приложения.

После каждого шага проверяется:
- остатки в базе равны теневой модели (физически и в резерве, по каждому товару и складу);
- сверка трёх источников зелёная;
- каждый отказ заслуженный: «не хватает» — только когда в модели правда не хватает,
  «склад удалён» — только для удалённого и т. д. И наоборот: если модель говорит «можно»,
  операция обязана пройти.

Если Hypothesis найдёт нарушение, он сам сократит сценарий до минимального и покажет его.
Глубина прогона: переменная HYPOTHESIS_PROFILE=deep (по умолчанию быстрый профиль для CI).
"""

from __future__ import annotations

import os
from collections import defaultdict

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from sqlalchemy import select

from app.core import inventory as inv
from app.core import views
from app.core.errors import InvalidOperation, Shortage, ValidationFailed, WarehouseBusy
from app.db.models import DocStatus, Stock
from app.db.session import SessionLocal
from app.sources.seed import reset, seed
from tests.conftest import Ids
from tests.helpers import run

settings.register_profile(
    "default",
    max_examples=25,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "deep",
    max_examples=300,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

idx = st.integers(min_value=0, max_value=50)
qty = st.integers(min_value=1, max_value=14)


def expect(error, fn, *args, **kwargs):
    """Операция должна упасть именно с этой ошибкой предметной области."""
    try:
        run(fn, *args, **kwargs)
    except error:
        return
    raise AssertionError(f"{fn.__name__}{args} должна была отказать с {error.__name__}")


class Inventory(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        with SessionLocal.begin() as s:
            reset(s)
            seed(s)
        ids = Ids()
        self.products = list(ids.sku.values())
        self.whs = list(ids.wh.values())
        self.active = set(self.whs)
        self.on: dict[tuple[int, int], int] = defaultdict(int)
        self.res: dict[tuple[int, int], int] = defaultdict(int)
        with SessionLocal() as s:
            for row in s.scalars(select(Stock)):
                self.on[row.product_id, row.warehouse_id] = row.on_hand
        self.docs: dict[int, dict] = {}
        self.new_wh = 0

    # --- помощники модели ---------------------------------------------------

    def p(self, i: int) -> int:
        return self.products[i % len(self.products)]

    def w(self, i: int) -> int:
        return self.whs[i % len(self.whs)]

    def avail(self, p: int, w: int) -> int:
        return self.on[p, w] - self.res[p, w]

    def doc(self, i: int, kind: str) -> tuple[int, dict] | None:
        ids = sorted(d for d, v in self.docs.items() if v["kind"] == kind)
        return (ids[i % len(ids)], self.docs[ids[i % len(ids)]]) if ids else None

    def remember(self, d, **extra) -> None:
        self.docs[d.id] = {
            "kind": d.kind,
            "status": d.status,
            "p": d.product_id,
            "q": d.qty,
            "frm": d.from_warehouse_id,
            "to": d.to_warehouse_id,
            **extra,
        }

    # --- приход -------------------------------------------------------------

    @rule(pi=idx, wi=idx, q=qty)
    def receipt(self, pi, wi, q):
        p, w = self.p(pi), self.w(wi)
        if w not in self.active:
            return expect(InvalidOperation, inv.create_receipt, p, w, q)
        d = run(inv.create_receipt, p, w, q).document
        self.on[p, w] += q
        self.remember(d)

    # --- перемещения ----------------------------------------------------------

    @rule(pi=idx, fi=idx, ti=idx, q=qty)
    def transfer(self, pi, fi, ti, q):
        p, f, t = self.p(pi), self.w(fi), self.w(ti)
        if f == t:
            return expect(ValidationFailed, inv.create_transfer, p, f, t, q)
        if t not in self.active:
            return expect(InvalidOperation, inv.create_transfer, p, f, t, q)
        if self.avail(p, f) < q:
            return expect(Shortage, inv.create_transfer, p, f, t, q)
        d = run(inv.create_transfer, p, f, t, q).document
        self.on[p, f] -= q
        self.remember(d)

    @rule(di=idx)
    def receive(self, di):
        if not (found := self.doc(di, "transfer")):
            return
        doc_id, d = found
        if d["status"] == DocStatus.CANCELLED:
            return expect(InvalidOperation, inv.receive_transfer, doc_id)
        res = run(inv.receive_transfer, doc_id)
        assert res.replayed == (d["status"] == DocStatus.RECEIVED)
        if not res.replayed:
            self.on[d["p"], d["to"]] += d["q"]
            d["status"] = DocStatus.RECEIVED

    # --- FBS ------------------------------------------------------------------

    @rule(pi=idx, wi=idx, q=qty)
    def fbs_order(self, pi, wi, q):
        p, w = self.p(pi), self.w(wi)
        if w not in self.active:
            return expect(InvalidOperation, inv.create_fbs_order, p, w, q)
        enough = self.avail(p, w) >= q
        res = run(inv.create_fbs_order, p, w, q)
        assert res.document.status == (DocStatus.RESERVED if enough else DocStatus.WAITING)
        if enough:
            self.res[p, w] += q
        self.remember(res.document)

    @rule(di=idx, wi=st.none() | idx)
    def fbs_reserve(self, di, wi):
        if not (found := self.doc(di, "fbs")):
            return
        doc_id, d = found
        target = self.w(wi) if wi is not None else d["frm"]
        if d["status"] in (DocStatus.SHIPPED, DocStatus.CANCELLED):
            return expect(InvalidOperation, inv.reserve_fbs, doc_id, warehouse_id=target)
        if d["status"] == DocStatus.RESERVED:
            assert run(inv.reserve_fbs, doc_id, warehouse_id=target).replayed
            return
        if target not in self.active:
            return expect(InvalidOperation, inv.reserve_fbs, doc_id, warehouse_id=target)
        if self.avail(d["p"], target) < d["q"]:
            return expect(Shortage, inv.reserve_fbs, doc_id, warehouse_id=target)
        run(inv.reserve_fbs, doc_id, warehouse_id=target)
        self.res[d["p"], target] += d["q"]
        d.update(status=DocStatus.RESERVED, frm=target)

    @rule(di=idx)
    def fbs_split(self, di):
        if not (found := self.doc(di, "fbs")):
            return
        doc_id, d = found
        if d["status"] != DocStatus.WAITING:
            if d["status"] == DocStatus.RESERVED:
                assert run(inv.split_fbs, doc_id).replayed
            else:
                expect(InvalidOperation, inv.split_fbs, doc_id)
            return
        p, q = d["p"], d["q"]
        total = sum(self.avail(p, w) for w in self.active)
        if total < q:
            return expect(Shortage, inv.split_fbs, doc_id)
        before = {w: self.avail(p, w) for w in self.active}
        res = run(inv.split_fbs, doc_id)
        parts = [res.document, *res.parts]
        # Свойства разделения: сумма та же, склады разные и активные, на каждом хватало товара.
        assert sum(x.qty for x in parts) == q
        assert len({x.from_warehouse_id for x in parts}) == len(parts)
        for x in parts:
            assert x.status == DocStatus.RESERVED
            assert x.from_warehouse_id in self.active
            assert x.qty <= before[x.from_warehouse_id]
            self.res[p, x.from_warehouse_id] += x.qty
        self.remember(res.document)
        for x in res.parts:
            self.remember(x)

    @rule(di=idx)
    def fbs_ship(self, di):
        if not (found := self.doc(di, "fbs")):
            return
        doc_id, d = found
        if d["status"] not in (DocStatus.RESERVED, DocStatus.SHIPPED):
            return expect(InvalidOperation, inv.ship_fbs, doc_id)
        res = run(inv.ship_fbs, doc_id)
        assert res.replayed == (d["status"] == DocStatus.SHIPPED)
        if not res.replayed:
            self.on[d["p"], d["frm"]] -= d["q"]
            self.res[d["p"], d["frm"]] -= d["q"]
            d["status"] = DocStatus.SHIPPED

    def _cancel_fbs(self, doc_id, d, fn):
        if d["status"] == DocStatus.SHIPPED:
            return expect(InvalidOperation, fn, doc_id)
        res = run(fn, doc_id)
        assert res.replayed == (d["status"] == DocStatus.CANCELLED)
        if d["status"] == DocStatus.RESERVED:
            self.res[d["p"], d["frm"]] -= d["q"]
        d["status"] = DocStatus.CANCELLED

    @rule(di=idx)
    def fbs_cancel(self, di):
        if found := self.doc(di, "fbs"):
            self._cancel_fbs(*found, inv.cancel_fbs)

    # --- FBO ------------------------------------------------------------------

    @rule(pi=idx, wi=idx, q=qty)
    def fbo(self, pi, wi, q):
        p, w = self.p(pi), self.w(wi)
        if w not in self.active:
            return expect(InvalidOperation, inv.create_fbo_shipment, p, w, q)
        if self.avail(p, w) < q:
            return expect(Shortage, inv.create_fbo_shipment, p, w, q)
        d = run(inv.create_fbo_shipment, p, w, q).document
        self.on[p, w] -= q
        self.remember(d)

    @rule(di=idx)
    def fbo_accept(self, di):
        if not (found := self.doc(di, "fbo")):
            return
        doc_id, d = found
        if d["status"] == DocStatus.CANCELLED:
            return expect(InvalidOperation, inv.accept_fbo, doc_id)
        res = run(inv.accept_fbo, doc_id)
        assert res.replayed == (d["status"] == DocStatus.ACCEPTED)
        d["status"] = DocStatus.ACCEPTED

    # --- «Удалить» любой документ ----------------------------------------------

    @rule(di=idx, kind=st.sampled_from(["receipt", "transfer", "fbs", "fbo"]))
    def delete(self, di, kind):
        if not (found := self.doc(di, kind)):
            return
        doc_id, d = found
        if kind == "fbs":
            return self._cancel_fbs(doc_id, d, inv.delete_document)
        if d["status"] == DocStatus.CANCELLED:
            assert run(inv.delete_document, doc_id).replayed
            return
        if kind == "receipt":
            if self.avail(d["p"], d["to"]) < d["q"]:
                return expect(InvalidOperation, inv.delete_document, doc_id)
            self.on[d["p"], d["to"]] -= d["q"]
        elif d["status"] in (DocStatus.RECEIVED, DocStatus.ACCEPTED):
            return expect(InvalidOperation, inv.delete_document, doc_id)
        else:  # перемещение или FBO в пути возвращается на склад отправки
            self.on[d["p"], d["frm"]] += d["q"]
        run(inv.delete_document, doc_id)
        d["status"] = DocStatus.CANCELLED

    # --- Склады ---------------------------------------------------------------

    @rule()
    def new_warehouse(self):
        self.new_wh += 1
        if self.new_wh > 3:
            return
        wh = run(inv.create_warehouse, f"Склад {self.new_wh}")
        self.whs.append(wh.id)
        self.active.add(wh.id)

    @rule(wi=idx)
    def archive(self, wi):
        w = self.w(wi)
        if w not in self.active:
            assert run(inv.archive_warehouse, w).replayed
            return
        busy = (
            any(self.res[p, w] for p in self.products)
            or any(
                d["kind"] == "transfer" and d["status"] == DocStatus.IN_TRANSIT and d["to"] == w
                for d in self.docs.values()
            )
            or any(
                d["kind"] == "fbs" and d["status"] == DocStatus.WAITING and d["frm"] == w for d in self.docs.values()
            )
        )
        if busy:
            return expect(WarehouseBusy, inv.archive_warehouse, w)
        run(inv.archive_warehouse, w)
        self.active.discard(w)

    # --- Проверки после каждого шага --------------------------------------------

    @invariant()
    def database_matches_model(self):
        with SessionLocal() as s:
            for row in s.scalars(select(Stock)):
                key = (row.product_id, row.warehouse_id)
                assert (row.on_hand, row.reserved) == (self.on[key], self.res[key]), key

    @invariant()
    def reconciliation_is_green(self):
        with SessionLocal() as s:
            rec = views.reconcile(s)
        assert rec.ok, [p for c in rec.checks for p in c.problems]


TestRandomScenarios = Inventory.TestCase
