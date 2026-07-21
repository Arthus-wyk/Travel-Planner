"""External provider adapters."""

from service.integrations.travel_google_maps import compute_route, search_places
from service.utils.ollama_client import create_ollama_client

__all__ = ["compute_route", "create_ollama_client", "search_places"]
