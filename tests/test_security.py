"""Плохой ввод и попытки атак: сервер не падает (никаких 500), данные не меняются, чужой код не выполняется.

TestClient пробрасывает исключения сервера, поэтому любая ошибка 500 здесь — упавший тест.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import SessionLocal
from app.main import app
from tests.helpers import assert_reconciled, stock

client = TestClient(app)

JUNK = ["99999999999", "2147483648", "-1", "0", "1.5", "abc", "", " ", "1e9", "NaN", "１", "0x10", "' OR 1=1 --"]


def _post(url: str, **data):
    return client.post(url, data=data, follow_redirects=False)


# --- Числа и id ----------------------------------------------------------------


def test_huge_qty_in_form_is_rejected_politely(ids):
    r = _post(
        "/operations/receipt",
        product_id=ids.sku["SKU-101"],
        warehouse_id=ids.wh["Север"],
        qty="99999999999",
        key="huge",
    )
    assert r.status_code == 303  # обратно на страницу с сообщением, а не 500
    page = client.get(r.headers["location"]).text
    assert "Слишком большое количество" in page
    assert stock(ids.sku["SKU-101"], ids.wh["Север"])[0] == 10


@pytest.mark.parametrize("value", JUNK)
@pytest.mark.parametrize("field", ["product_id", "warehouse_id", "qty"])
def test_junk_in_every_receipt_field_never_crashes(ids, field, value):
    data = {"product_id": ids.sku["SKU-101"], "warehouse_id": ids.wh["Север"], "qty": 1, "key": f"junk-{field}-{value}"}
    data[field] = value
    r = _post("/operations/receipt", **data)
    assert r.status_code == 303
    assert_reconciled()


@pytest.mark.parametrize("value", JUNK)
def test_junk_in_other_forms_never_crashes(ids, value):
    p, n, s = ids.sku["SKU-101"], ids.wh["Север"], ids.wh["Юг"]
    forms = [
        ("/operations/transfer", {"product_id": p, "from_id": value, "to_id": s, "qty": value, "key": "t" + value}),
        (
            "/operations/fbs",
            {"product_id": value, "warehouse_id": n, "qty": value, "key": "f" + value, "order_ref": value},
        ),
        ("/operations/fbo", {"product_id": p, "warehouse_id": value, "qty": value, "key": "o" + value}),
        ("/operations/fbs/99999999999/reserve", {"warehouse_id": value}),
        ("/warehouses", {"name": value}),
    ]
    for url, data in forms:
        assert _post(url, **data).status_code == 303, url
    assert_reconciled()


@pytest.mark.parametrize(
    "url",
    [
        "/operations/transfer/99999999999/receive",
        "/operations/fbs/99999999999/ship",
        "/operations/fbs/-5/split",
        "/operations/fbo/0/accept",
        "/documents/99999999999/delete",
        "/warehouses/99999999999/rename",
        "/warehouses/99999999999/delete",
    ],
)
def test_unknown_or_huge_ids_in_urls(url):
    assert _post(url, name="x").status_code == 303


def test_api_rejects_bad_numbers(ids):
    base = {"product_id": ids.sku["SKU-101"], "warehouse_id": ids.wh["Север"]}
    for qty in [99999999999, 0, -3, 1.5, "5", True, None]:
        assert client.post("/api/receipts", json={**base, "qty": qty}).status_code == 422, qty
    assert client.post("/api/receipts", json={**base, "qty": 1, "product_id": 99999999999}).status_code == 404
    assert client.post("/api/fbs/99999999999/ship").status_code == 404
    assert client.post("/api/documents/99999999999/cancel").status_code == 404
    assert stock(ids.sku["SKU-101"], ids.wh["Север"])[0] == 10


def test_api_rejects_too_long_texts(ids):
    body = {"product_id": ids.sku["SKU-101"], "warehouse_id": ids.wh["Север"], "qty": 1}
    assert client.post("/api/fbs", json={**body, "order_ref": "x" * 65}).status_code == 422
    assert client.post("/api/receipts", json=body, headers={"Idempotency-Key": "k" * 101}).status_code == 422
    assert client.post("/api/warehouses", json={"name": "x" * 101}).status_code == 422


# --- XSS ---------------------------------------------------------------------

XSS = ["<script>alert(1)</script>", "Юг'); alert(1); ('", '"><img src=x onerror=alert(1)>']


@pytest.mark.parametrize("payload", XSS)
def test_xss_in_warehouse_name_is_escaped(payload):
    _post("/warehouses", name=payload)
    for page in ["/warehouses", "/", "/operations", "/history"]:
        html = client.get(page).text
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror" not in html
        # Название никогда не попадает внутрь JS-кода обработчиков.
        assert "alert(1); ('" not in html.split("onsubmit=")[-1].split(">")[0]


@pytest.mark.parametrize("payload", XSS)
def test_xss_in_order_ref_is_escaped(ids, payload):
    _post(
        "/operations/fbs",
        product_id=ids.sku["SKU-101"],
        warehouse_id=ids.wh["Север"],
        qty=1,
        key="xss" + payload,
        order_ref=payload,
    )
    for page in ["/operations", "/history"]:
        html = client.get(page).text
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror" not in html


def test_inline_handlers_contain_no_user_data():
    _post("/warehouses", name="Склад'); alert(1); ('")
    html = client.get("/warehouses").text
    handlers = [chunk.split('"')[1] for chunk in html.split("onsubmit=")[1:]]
    assert handlers and all(h == "return confirm(this.dataset.confirm)" for h in handlers)


# --- SQL-инъекции и прочее ----------------------------------------------------


def test_sql_injection_is_just_text(ids):
    payload = "x'); DROP TABLE stock; --"
    _post("/warehouses", name=payload)
    client.get("/history", params={"product_id": "1 OR 1=1"})
    with SessionLocal() as s:
        assert s.scalar(text("SELECT count(*) FROM stock")) > 0
        assert s.scalar(text("SELECT count(*) FROM warehouses WHERE name = :n"), {"n": payload}) == 1
    assert_reconciled()


@pytest.mark.parametrize("back", ["//evil.example", "https://evil.example", "javascript:alert(1)"])
def test_no_open_redirect_after_delete(ids, back):
    r = _post("/documents/99999999/delete", back=back)
    assert r.headers["location"] == "/operations"


def test_broken_flash_cookie_is_ignored():
    client.cookies.set("flash", "not-base64!!")
    assert client.get("/").status_code == 200
    client.cookies.clear()
