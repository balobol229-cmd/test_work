"""Наполняет базу большим количеством реалистичных операций, чтобы замерить экраны на объёме.

    uv run python -m loadtest.fill 20000          # 20 000 операций в базу из DATABASE_URL

Сначала сбрасывает данные к тестовым из задания, затем добавляет товары и операции:
приходы, перемещения с приёмкой, заказы FBS с отгрузкой и отменой, поставки FBO.
"""

import random
import sys
import time

from app.core import inventory as inv
from app.core.errors import DomainError
from app.db.models import Product, Store, Warehouse
from app.db.session import SessionLocal
from app.sources.seed import reset, seed


def main(total: int) -> None:
    rnd = random.Random(42)
    with SessionLocal.begin() as s:
        reset(s)
        seed(s)
        store = s.query(Store).one()
        for i in range(4, 41):  # ещё 37 артикулов, чтобы экран был похож на настоящий
            s.add(Product(store_id=store.id, sku=f"SKU-{100 + i}", name=f"Товар {i}"))
        s.add(Warehouse(name="Центр"))
    with SessionLocal() as s:
        products = [p.id for p in s.query(Product)]
        whs = [w.id for w in s.query(Warehouse)]

    def run(fn, *args, **kwargs):
        with SessionLocal.begin() as s:
            return fn(s, *args, **kwargs)

    started, done = time.perf_counter(), 0
    open_fbs, open_transfers, open_fbo = [], [], []
    while done < total:
        p, w = rnd.choice(products), rnd.choice(whs)
        roll = rnd.random()
        try:
            if roll < 0.25:
                run(inv.create_receipt, p, w, rnd.randint(5, 40))
            elif roll < 0.55:
                r = run(inv.create_fbs_order, p, w, rnd.randint(1, 4))
                if r.document.status == "reserved":
                    open_fbs.append(r.document.id)
                else:
                    run(inv.cancel_fbs, r.document.id)  # в реальности заказ ушёл бы с другого склада
            elif roll < 0.75 and open_fbs:
                doc_id = open_fbs.pop(rnd.randrange(len(open_fbs)))
                run(inv.ship_fbs if rnd.random() < 0.85 else inv.cancel_fbs, doc_id)
            elif roll < 0.85:
                w2 = rnd.choice([x for x in whs if x != w])
                open_transfers.append(run(inv.create_transfer, p, w, w2, rnd.randint(1, 5)).document.id)
            elif roll < 0.9 and open_transfers:
                run(inv.receive_transfer, open_transfers.pop(0))
            elif roll < 0.96:
                open_fbo.append(run(inv.create_fbo_shipment, p, w, rnd.randint(1, 5)).document.id)
            elif open_fbo:
                run(inv.accept_fbo, open_fbo.pop(0))
        except DomainError:
            pass
        done += 1
        if done % 5000 == 0:
            print(f"{done} операций, {done / (time.perf_counter() - started):.0f} в секунду", flush=True)
    print(f"Готово: {done} операций за {time.perf_counter() - started:.0f} с")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20000)
