"""Ollama Cloud client configuration."""

from __future__ import annotations

import os

from ollama import AsyncClient

from service.config import get_env_or_dotenv

DEFAULT_OLLAMA_HOST = "https://ollama.com"
# The `-cloud` tag is required when selecting Ollama's hosted cloud variant.
DEFAULT_MODEL = "gpt-oss:120b-cloud"


def create_ollama_client() -> AsyncClient:
    """Create an Ollama client configured for Ollama Cloud."""
    api_key = get_env_or_dotenv("OLLAMA_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return AsyncClient(
        host=os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        headers=headers,
    )
