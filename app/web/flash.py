"""Одноразовое сообщение после POST → redirect (паттерн Post/Redirect/Get)."""

from __future__ import annotations

import base64
import json

from fastapi import Request
from fastapi.responses import RedirectResponse

COOKIE = "flash"


def redirect(url: str, kind: str, text: str, **extra) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    payload = json.dumps({"kind": kind, "text": text, **extra}, ensure_ascii=False)
    response.set_cookie(COOKIE, base64.urlsafe_b64encode(payload.encode()).decode(), max_age=60, httponly=True, samesite="lax")
    return response


def pop(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(raw.encode()))
    except ValueError:
        return None
