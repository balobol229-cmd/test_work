"""Ошибки предметной области. Веб-слой превращает их в HTTP-ответы и сообщения в интерфейсе."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(DomainError):
    status_code = 404


class ValidationFailed(DomainError):
    status_code = 422


class InvalidOperation(DomainError):
    """Операция невозможна в текущем состоянии: например, принять уже отменённое."""

    status_code = 409


class Shortage(DomainError):
    """На выбранном ФФ не хватает доступного товара. В details — варианты решения."""

    status_code = 409


class WarehouseBusy(DomainError):
    """Склад нельзя удалить: на нём есть незавершённые операции."""

    status_code = 409


class IdempotencyConflict(DomainError):
    """Ключ повтора уже использован для другой операции."""

    status_code = 409
