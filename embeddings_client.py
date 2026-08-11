"""
Shared embedding HTTP client for gamecode-rag.

Backends:
  - openrouter (default): https://openrouter.ai/api/v1/embeddings + OPENROUTER_API_KEY
  - openai_compatible: any OpenAI-style /v1/embeddings (Ollama, LM Studio, vLLM, TEI, …)

Env:
  EMBEDDING_BACKEND     openrouter | openai_compatible
  EMBEDDING_MODEL       model id (must match ingest + query; re-ingest if changed)
  EMBEDDING_BASE_URL    base URL for openai_compatible (e.g. http://127.0.0.1:11434/v1)
  EMBEDDING_API_KEY     optional bearer for local servers that require a key (default "ollama" / empty)
  OPENROUTER_API_KEY    required for openrouter embeddings and for re-ranker
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("gamecode-rag-embeddings")

EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "openrouter").strip().lower()
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "").rstrip("/")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

OPENROUTER_EMBED_URL = "https://openrouter.ai/api/v1/embeddings"


def embedding_endpoint() -> str:
    if EMBEDDING_BACKEND in ("openai_compatible", "local", "ollama"):
        if not EMBEDDING_BASE_URL:
            raise ValueError(
                "EMBEDDING_BACKEND is openai_compatible but EMBEDDING_BASE_URL is empty. "
                "Example: http://127.0.0.1:11434/v1"
            )
        base = EMBEDDING_BASE_URL if EMBEDDING_BASE_URL.endswith("/") else EMBEDDING_BASE_URL + "/"
        return urljoin(base, "embeddings")
    # default openrouter
    return OPENROUTER_EMBED_URL


def embedding_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if EMBEDDING_BACKEND in ("openai_compatible", "local", "ollama"):
        # Many local servers ignore auth; some want any non-empty Bearer.
        key = EMBEDDING_API_KEY or "ollama"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is required for EMBEDDING_BACKEND=openrouter")
    headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
    return headers


def embeddings_ready() -> tuple[bool, str]:
    """Return (ok, error_message)."""
    try:
        if EMBEDDING_BACKEND in ("openai_compatible", "local", "ollama"):
            if not EMBEDDING_BASE_URL:
                return False, "Set EMBEDDING_BASE_URL for local/openai_compatible embeddings."
            return True, ""
        if not OPENROUTER_API_KEY:
            return False, "OPENROUTER_API_KEY is not set (needed for OpenRouter embeddings)."
        return True, ""
    except Exception as e:
        return False, str(e)


def log_embedding_config() -> None:
    logger.info(
        "Embeddings: backend=%s model=%s endpoint=%s",
        EMBEDDING_BACKEND,
        EMBEDDING_MODEL,
        embedding_endpoint(),
    )


async def fetch_embeddings(client: httpx.AsyncClient, texts: list[str], timeout: float = 120.0) -> Optional[list[list[float]]]:
    """OpenAI-compatible embeddings call. `texts` is a list of strings."""
    if not texts:
        return []
    cleaned = [t if (t and str(t).strip()) else "empty" for t in texts]
    payload = {"model": EMBEDDING_MODEL, "input": cleaned}
    try:
        headers = embedding_headers()
        url = embedding_endpoint()
        response = await client.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        items = data.get("data")
        if not items:
            logger.error("Embeddings response missing 'data': %s", data)
            return None
        # Ensure order by index if present
        items = sorted(items, key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]
    except httpx.HTTPStatusError as e:
        logger.error("HTTP Error embedding: %s %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("Error getting embeddings: %s", e, exc_info=True)
        return None


async def fetch_embedding(client: httpx.AsyncClient, text: str = "", timeout: float = 30.0) -> Optional[list[float]]:
    batch = await fetch_embeddings(client, [text or "empty query"], timeout=timeout)
    if not batch:
        return None
    return batch[0]
