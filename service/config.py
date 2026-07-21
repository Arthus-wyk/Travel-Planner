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

GOOGLE_MAPS_API_KEY = get_env_or_dotenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_MAPS_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_MAPS_TIMEOUT_SECONDS", "10"))
GOOGLE_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.businessStatus",
        "places.types",
        "places.googleMapsUri",
    ]
)
GOOGLE_ROUTES_FIELD_MASK = ",".join(
    [
        "routes.duration",
        "routes.staticDuration",
        "routes.distanceMeters",
        "routes.localizedValues",
        "routes.legs.distanceMeters",
        "routes.legs.duration",
        "routes.legs.localizedValues",
    ]
)
