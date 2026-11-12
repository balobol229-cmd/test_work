"""HTTP-уровень: страницы открываются, API отвечает понятными кодами и повтор по ключу безопасен."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import assert_reconciled, stock

client = TestClient(app)


@pytest.mark.parametrize("path", ["/", "/operations", "/warehouses", "/history", "/system", "/check", "/docs"])
def test_pages_open(path):
    assert client.get(path).status_code == 200


def test_stock_api_matches_test_data(ids):
    body = client.get("/api/stock").json()
    sku101 = next(p for p in body["products"] if p["sku"] == "SKU-101")
    assert sku101["total"] == 16
    assert body["store"] == {"name": "Магазин 01", "marketplace": "WB"}


def test_fbs_with_idempotency_key_is_created_once(ids):
    payload = {"product_id": ids.sku["SKU-101"], "warehouse_id": ids.wh["Север"], "qty": 3}
    first = client.post("/api/fbs", json=payload, headers={"Idempotency-Key": "abc"})
    again = client.post("/api/fbs", json=payload, headers={"Idempotency-Key": "abc"})
    assert first.status_code == 201
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert first.json()["document"]["id"] == again.json()["document"]["id"]
    assert stock(ids.sku["SKU-101"], ids.wh["Север"]) == (10, 3, 7)


def test_fbo_shortage_returns_409_with_options(ids):
    r = client.post("/api/fbo", json={"product_id": ids.sku["SKU-103"], "warehouse_id": ids.wh["Север"], "qty": 2})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "Shortage"
    assert body["shortage"]["options"][0]["kind"] == "switch"


def test_delete_busy_warehouse_returns_reasons(ids):
    client.post("/api/fbs", json={"product_id": ids.sku["SKU-101"], "warehouse_id": ids.wh["Юг"], "qty": 1})
    r = client.delete(f"/api/warehouses/{ids.wh['Юг']}")
    assert r.status_code == 409
    assert r.json()["blockers"]


def test_html_form_double_submit_is_not_applied_twice(ids):
    form = {"product_id": ids.sku["SKU-102"], "warehouse_id": ids.wh["Юг"], "qty": 3, "key": "form-key"}
    client.post("/operations/receipt", data=form, follow_redirects=False)
    client.post("/operations/receipt", data=form, follow_redirects=False)
    assert stock(ids.sku["SKU-102"], ids.wh["Юг"])[0] == 3
    assert_reconciled()


def test_check_endpoint(ids):
    body = client.get("/api/check").json()
    assert body["ok"] is True and body["units"] == 25
