import os

# Локально: PostgreSQL на localhost. В Docker переменную задаёт docker-compose.yml.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/checkstock")
