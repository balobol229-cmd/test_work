.PHONY: up down test reset logs dev dev-test

# --- Docker: ничего, кроме Docker, ставить не нужно ---------------------------

up:            ## поднять базу и приложение на http://localhost:8000
	docker compose up --build -d
	@echo "CheckStock: http://localhost:8000"

down:          ## остановить и удалить данные
	docker compose down -v

test:          ## прогнать тесты внутри контейнера на PostgreSQL из compose
	docker compose run --rm --build app pytest

reset:         ## вернуть данные к тестовым из задания
	docker compose exec app python -m app.sources.seed --reset

logs:
	docker compose logs -f app

# --- Локально: Python 3.12, uv и PostgreSQL на localhost ------------------------

dev:
	createdb checkstock 2>/dev/null || true
	uv sync
	uv run alembic upgrade head
	uv run python -m app.sources.seed
	uv run uvicorn app.main:app --reload

dev-test:
	uv run pytest
