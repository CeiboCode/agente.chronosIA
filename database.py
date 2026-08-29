import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta configurar la variable de entorno {name}.")
    return value


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=_required_env("DB_NAME"),
        user=_required_env("DB_USER"),
        password=_required_env("DB_PASSWORD"),
        port=os.getenv("DB_PORT", "5432"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )
