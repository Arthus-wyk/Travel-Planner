"""Canonical agent-loop entry point.

The mature Ollama loop remains import-compatible while callers migrate from
``service.agent.client`` to this module.
"""

from service.agent.client import QueryEngine, answer_user

__all__ = ["QueryEngine", "answer_user"]
