"""Perplexity web search plugin — bundled, auto-loaded (search-only).

Perplexity exposes a Search API (and Sonar answer models that return
citations) but no content-extraction/crawl API, so this backend supports
search only; web_extract/web_crawl fall back to another configured backend.
"""

from __future__ import annotations

from plugins.web.perplexity.provider import PerplexityWebSearchProvider


def register(ctx) -> None:
    """Register the Perplexity provider with the plugin context."""
    ctx.register_web_search_provider(PerplexityWebSearchProvider())
