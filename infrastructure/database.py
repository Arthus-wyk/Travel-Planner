"""Database connection and schema lifecycle."""

from service.storage.travel_db import check_database_on_startup, connect, ensure_schema

__all__ = ["check_database_on_startup", "connect", "ensure_schema"]
