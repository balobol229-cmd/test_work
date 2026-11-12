"""Нагрузочный тест: «менеджеры», которые одновременно смотрят остатки и проводят операции.

Запуск против поднятого приложения (например, `docker compose up -d`):

    uv run --group load locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless -u 100 -r 20 -t 60s

После прогона: `curl localhost:8000/api/check` — сверка должна остаться зелёной.

Отказы предметной области (409 «не хватает», 404 «уже нет») считаются нормальным ответом:
это бизнес-результат, а не сбой. Сбой — это 5xx или таймаут.
"""

import random
import uuid

from locust import HttpUser, between, task


class Manager(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self) -> None:
        stock = self.client.get("/api/stock", name="/api/stock").json()
        self.products = [p["id"] for p in stock["products"]]
        self.warehouses = [w["id"] for w in stock["warehouses"] if not w["deleted"]]
        self.open_fbs: list[int] = []
        self.open_transfers: list[int] = []

    # --- что менеджер смотрит --------------------------------------------------

    @task(6)
    def stock_page(self) -> None:
        self.client.get("/", name="GET / (остатки)")

    @task(3)
    def operations_page(self) -> None:
        self.client.get("/operations", name="GET /operations")

    @task(2)
    def history_page(self) -> None:
        self.client.get("/history", name="GET /history")

    @task(2)
    def check(self) -> None:
        self.client.get("/api/check", name="GET /api/check")

    # --- что менеджер делает ---------------------------------------------------

    def _post(self, url: str, name: str, **kwargs):
        with self.client.post(url, name=name, catch_response=True, **kwargs) as r:
            if r.status_code in (200, 201, 404, 409, 422):
                r.success()  # бизнес-отказ — тоже корректный ответ
            return r

    def _body(self, qty_max: int = 5) -> dict:
        return {
            "product_id": random.choice(self.products),
            "warehouse_id": random.choice(self.warehouses),
            "qty": random.randint(1, qty_max),
        }

    @task(2)
    def receipt(self) -> None:
        self._post(
            "/api/receipts", "POST /api/receipts", json=self._body(20), headers={"Idempotency-Key": uuid.uuid4().hex}
        )

    @task(4)
    def fbs_order(self) -> None:
        r = self._post("/api/fbs", "POST /api/fbs", json=self._body(), headers={"Idempotency-Key": uuid.uuid4().hex})
        if r.status_code in (200, 201):
            self.open_fbs.append(r.json()["document"]["id"])

    @task(3)
    def fbs_ship_or_cancel(self) -> None:
        if not self.open_fbs:
            return
        doc_id = self.open_fbs.pop(random.randrange(len(self.open_fbs)))
        action = random.choice(["ship", "ship", "cancel", "split"])
        self._post(f"/api/fbs/{doc_id}/{action}", f"POST /api/fbs/[id]/{action}")

    @task(2)
    def transfer(self) -> None:
        if len(self.warehouses) < 2:
            return
        src, dst = random.sample(self.warehouses, 2)
        body = {
            "product_id": random.choice(self.products),
            "from_warehouse_id": src,
            "to_warehouse_id": dst,
            "qty": random.randint(1, 3),
        }
        r = self._post("/api/transfers", "POST /api/transfers", json=body)
        if r.status_code in (200, 201):
            self.open_transfers.append(r.json()["document"]["id"])

    @task(2)
    def receive(self) -> None:
        if self.open_transfers:
            doc_id = self.open_transfers.pop(0)
            self._post(f"/api/transfers/{doc_id}/receive", "POST /api/transfers/[id]/receive")

    @task(1)
    def fbo(self) -> None:
        self._post("/api/fbo", "POST /api/fbo", json=self._body(3))

    @task(1)
    def double_ship(self) -> None:
        """Повторное подтверждение под нагрузкой: второй запрос должен вернуть replayed."""
        r = self._post("/api/fbs", "POST /api/fbs", json=self._body(2))
        if r.status_code in (200, 201) and r.json()["document"]["status"] == "reserved":
            doc_id = r.json()["document"]["id"]
            self._post(f"/api/fbs/{doc_id}/ship", "POST /api/fbs/[id]/ship")
            again = self._post(f"/api/fbs/{doc_id}/ship", "POST /api/fbs/[id]/ship (повтор)")
            if again.status_code == 200 and not again.json()["replayed"]:
                again.failure("повторное подтверждение списало товар второй раз")
