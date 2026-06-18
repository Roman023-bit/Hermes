"""Perplexity web search — plugin form (search-only).

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Perplexity
exposes a Search API (and Sonar answer models that return citations) but no
content-extraction/crawl API, so this provider supports search only;
web_extract/web_crawl fall back to another configured backend.

Requires ``PERPLEXITY_API_KEY``. Optional: ``PERPLEXITY_BASE_URL`` (default
``https://api.perplexity.ai``) and ``PERPLEXITY_SEARCH_MODEL`` (default
``sonar``).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _perplexity_base_url() -> str:
    return os.getenv("PERPLEXITY_BASE_URL", "https://api.perplexity.ai")


def _normalize_perplexity_search_results(response: Any, limit: int = 5) -> Dict[str, Any]:
    """Normalize a Perplexity response to the standard web search format.

    Handles both response shapes Perplexity can return:

    * Search API (``/search``):  ``{"results": [{title, url, snippet, ...}]}``
    * Sonar chat (``/chat/completions``): ``{"search_results": [{title, url}],
      "citations": ["https://...", ...], "choices": [...]}``

    Maps either onto ``{success, data: {web: [{title, url, description,
    position}]}}`` — the shared web-search result shape.
    """
    items: List[Any] = []
    if isinstance(response, dict):
        raw = response.get("results")
        if not isinstance(raw, list) or not raw:
            raw = response.get("search_results")
        if isinstance(raw, list) and raw:
            items = raw
        elif isinstance(response.get("citations"), list):
            items = response["citations"]

    web_results: List[Dict[str, Any]] = []
    for i, r in enumerate(items[:limit]):
        if isinstance(r, str):
            web_results.append({"title": "", "url": r, "description": "", "position": i + 1})
        elif isinstance(r, dict):
            web_results.append({
                "title": r.get("title", "") or "",
                "url": r.get("url", "") or "",
                "description": (
                    r.get("snippet")
                    or r.get("excerpt")
                    or r.get("description")
                    or r.get("text")
                    or ""
                ),
                "position": i + 1,
            })

    result: Dict[str, Any] = {"success": True, "data": {"web": web_results}}
    # Propagate token usage when present (Sonar /chat/completions reports it;
    # the dedicated /search endpoint does not) so tool_pricing can price the
    # call by tokens instead of the flat per-request fallback.
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        result["usage"] = response["usage"]
    return result


class PerplexityWebSearchProvider(WebSearchProvider):
    """Perplexity Search API provider (search-only)."""

    @property
    def name(self) -> str:
        return "perplexity"

    @property
    def display_name(self) -> str:
        return "Perplexity"

    def is_available(self) -> bool:
        """True when ``PERPLEXITY_API_KEY`` is set. No network I/O — runs on
        every web_search dispatch and ``hermes tools`` repaint."""
        return bool(os.getenv("PERPLEXITY_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search via Perplexity's Search API, falling back to Sonar chat.

        Prefers the dedicated ``/search`` endpoint; when the account lacks
        access (403/404) or it returns nothing, harvests citations from a
        Sonar ``/chat/completions`` answer instead. Errors are returned as
        ``{"success": False, "error": ...}`` rather than raised.
        """
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return {"success": False, "error": "Interrupted"}
        except Exception:
            pass

        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            return {
                "success": False,
                "error": (
                    "PERPLEXITY_API_KEY environment variable not set. "
                    "Get your API key at https://www.perplexity.ai/account/api"
                ),
            }

        base = _perplexity_base_url().rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            n = min(max(int(limit), 1), 20)
        except (TypeError, ValueError):
            n = 5
        logger.info("Perplexity search: '%s' (limit=%d)", query, n)

        # 1) Dedicated Search API — returns ranked links directly.
        try:
            resp = httpx.post(
                f"{base}/search",
                headers=headers,
                json={"query": query, "max_results": n},
                timeout=60,
            )
            # 403/404 → account has no Search API access; fall through to chat.
            if resp.status_code not in (403, 404):
                resp.raise_for_status()
                normalized = _normalize_perplexity_search_results(resp.json(), n)
                if normalized["data"]["web"]:
                    return normalized
        except httpx.HTTPError as exc:
            logger.info(
                "Perplexity /search unavailable (%s); falling back to Sonar chat.",
                str(exc)[:80],
            )

        # 2) Fallback: Sonar answer model, harvest the citations it searched.
        model = (os.getenv("PERPLEXITY_SEARCH_MODEL", "sonar").strip() or "sonar")
        try:
            resp = httpx.post(
                f"{base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": query}]},
                timeout=60,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"Perplexity search failed: {exc}"}
        return _normalize_perplexity_search_results(resp.json(), n)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Perplexity",
            "badge": "paid · search only",
            "tag": "Search only — web_extract/crawl fall back to another configured backend",
            "env_vars": [
                {
                    "key": "PERPLEXITY_API_KEY",
                    "prompt": "Perplexity API key",
                    "url": "https://www.perplexity.ai/account/api",
                },
            ],
        }
