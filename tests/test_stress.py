"""Стресс параллельности: много потоков одновременно работают с одними и теми же товарами.

Каждый поток в случайном порядке делает приходы, перемещения, резервы, отгрузки, отмены,
FBO и удаления. Отказы предметной области («не хватает», «уже отправлен») — нормальны.
Тест падает, если случилось что-то другое: дедлок, нарушение ограничения базы, ошибка 500.
В конце остатки сходятся с журналом и документами.
"""

import os
import random
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread

from sqlalchemy import select

from app.core import inventory as inv
from app.core import views
from app.core.errors import DomainError
from app.db.models import Document, Stock
from app.db.session import SessionLocal, engine
from tests.helpers import assert_reconciled, run

THREADS = int(os.getenv("STRESS_THREADS", "24"))
OPS_PER_THREAD = int(os.getenv("STRESS_OPS", "40"))
# Зерно случайное, но печатается при падении: STRESS_SEED=<число> повторит тот же прогон.
SEED = int(os.getenv("STRESS_SEED", random.randrange(1_000_000)))


def _random_op(rnd: random.Random, products: list[int], whs: list[int]) -> None:
    p, w, w2 = rnd.choice(products), rnd.choice(whs), rnd.choice(whs)
    q = rnd.randint(1, 6)
    with SessionLocal() as s:
        docs = s.execute(select(Document.id, Document.kind).where(Document.product_id.in_(products))).all()
    doc = rnd.choice(docs) if docs else None
    ops = [
        lambda: run(inv.create_receipt, p, w, q),
        lambda: run(inv.create_transfer, p, w, w2, q),
        lambda: run(inv.create_fbs_order, p, w, q),
        lambda: run(inv.create_fbo_shipment, p, w, q),
    ]
    if doc:
        doc_id, kind = doc
        ops += {
            "transfer": [lambda: run(inv.receive_transfer, doc_id)],
            "fbs": [
                lambda: run(inv.ship_fbs, doc_id),
                lambda: run(inv.cancel_fbs, doc_id),
                lambda: run(inv.split_fbs, doc_id),
                lambda: run(inv.reserve_fbs, doc_id),
            ],
            "fbo": [lambda: run(inv.accept_fbo, doc_id)],
            "receipt": [],
        }[kind]
        ops.append(lambda: run(inv.delete_document, doc_id))
    rnd.choice(ops)()


def test_many_threads_hammer_the_same_goods(ids):
    engine.pool.dispose()
    products = [ids.sku["SKU-101"], ids.sku["SKU-102"]]
    whs = [ids.wh["Север"], ids.wh["Юг"]]
    barrier = Barrier(THREADS)
    domain_refusals = 0

    def worker(n: int) -> list[BaseException]:
        nonlocal domain_refusals
        rnd = random.Random(SEED * 1000 + n)
        unexpected = []
        barrier.wait()
        for _ in range(OPS_PER_THREAD):
            try:
                _random_op(rnd, products, whs)
            except DomainError:
                domain_refusals += 1
            except Exception as exc:  # дедлок, нарушение ограничения базы и т. п.
                unexpected.append(exc)
        return unexpected

    # Пока идёт запись, отдельный поток всё время запускает сверку: она читает один снимок
    # базы и не должна поднимать ложную тревогу посреди чужих транзакций.
    stop, false_alarms, checks = Event(), [], [0]

    def auditor() -> None:
        while not stop.is_set():
            with SessionLocal() as s:
                rec = views.reconcile(s)
            checks[0] += 1
            if not rec.ok:
                false_alarms.append([p for c in rec.checks for p in c.problems])

    watcher = Thread(target=auditor)
    watcher.start()
    with ThreadPoolExecutor(THREADS) as pool:
        errors = [e for result in pool.map(worker, range(THREADS)) for e in result]
    stop.set()
    watcher.join()

    assert checks[0] > 0
    assert false_alarms == [], (f"STRESS_SEED={SEED}", false_alarms[:2])

    assert errors == [], (f"STRESS_SEED={SEED}", errors[:3])
    assert domain_refusals > 0  # сценарии действительно упирались в нехватку и повторы
    with SessionLocal() as s:
        assert all(st.on_hand >= 0 and 0 <= st.reserved <= st.on_hand for st in s.scalars(select(Stock)))
    try:
        assert_reconciled()
    except AssertionError as exc:
        raise AssertionError(f"STRESS_SEED={SEED}: {exc}") from exc
