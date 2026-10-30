from __future__ import annotations

import os
from pathlib import Path


def get_env_or_dotenv(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value

    dotenv_path = Path(__file__).resolve().parents[1] / ".env"
    if not dotenv_path.is_file():
        return default

    with dotenv_path.open("r", encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key.strip() != name:
                continue
            parsed_value = raw_value.strip()
            if (
                len(parsed_value) >= 2
                and parsed_value[0] == parsed_value[-1]
                and parsed_value[0] in {"'", '"'}
            ):
                parsed_value = parsed_value[1:-1]
            return parsed_value

    return default


DB_HOST = os.getenv("TRAVEL_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("TRAVEL_DB_PORT", "3306"))
DB_USER = os.getenv("TRAVEL_DB_USER", "root")
DB_PASSWORD = os.getenv("TRAVEL_DB_PASSWORD", "1234")
DB_NAME = os.getenv("TRAVEL_DB_NAME", "custom_travel_planner")
DB_CHARSET = os.getenv("TRAVEL_DB_CHARSET", "utf8mb4")
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("TRAVEL_DB_CONNECT_TIMEOUT_SECONDS", "5"))
VALID_DB_CHARSETS = {"utf8", "utf8mb4"}

AMAP_API_KEY = get_env_or_dotenv("AMAP_API_KEY", "")
AMAP_TIMEOUT_SECONDS = int(get_env_or_dotenv("AMAP_TIMEOUT_SECONDS", "10"))
