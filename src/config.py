"""Central configuration, read from environment variables.

When running inside Docker, compose injects these. When running a script
directly on the host for debugging, python-dotenv loads them from .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # harmless inside Docker (no .env there); helpful on the host

POSTGRES_USER = os.getenv("POSTGRES_USER", "portfolio")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "portfolio")
POSTGRES_DB = os.getenv("POSTGRES_DB", "portfolio")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")

EXCEL_PATH = os.getenv("EXCEL_PATH", "/data/sample_portfolio.xlsx")
BASE_CURRENCY = os.getenv("BASE_CURRENCY", "EUR")

DATABASE_URL = (
    f"postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
