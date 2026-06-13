"""Approximate price table for Hermes' paid tool APIs.

Unlike LLM calls (priced precisely from token usage in ``agent.usage_pricing``),
most paid tool APIs bill per request / per result / per character on plans that
vary by account. We therefore keep a small, clearly-labelled table of public
list-price estimates and always report cost with status ``estimated``. The goal
is visibility ("roughly how much did that cost"), not invoice-grade accuracy.

Prices are USD, sourced from each vendor's public pricing pages and rounded to
sensible per-unit figures as of mid-2026. Override any value via
``cost_tracking.prices`` in ``config.yaml`` (see :func:`_config_overrides`).

All numbers are list-price approximations; your actual bill depends on your
plan, free-tier credits, and volume discounts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

from tools.cost_ledger import STATUS_ESTIMATED, STATUS_INCLUDED

# Per-request search price (one web_search call), USD.
_SEARCH_PRICE: Dict[str, float] = {
    "exa": 0.005,         # exa.ai neural search, ~$5 / 1k
    "firecrawl": 0.001,   # firecrawl /search
    "parallel": 0.009,    # parallel.ai agentic search
    "tavily": 0.008,      # tavily, ~1 credit/search
    "perplexity": 0.006,  # perplexity Search API / Sonar (flat fallback)
}

# Per-URL extract price (web_extract, charged per page), USD.
_EXTRACT_PRICE_PER_URL: Dict[str, float] = {
    "exa": 0.001,
    "firecrawl": 0.001,
    "parallel": 0.001,
    "tavily": 0.008,
}

# Per-page crawl price (web_crawl), USD.
_CRAWL_PRICE_PER_PAGE: Dict[str, float] = {
    "firecrawl": 0.001,
    "tavily": 0.008,
}

# Per-image generation price, USD (standard size/quality).
_IMAGE_PRICE_PER_IMAGE: Dict[str, float] = {
    "fal": 0.04,
    "openai": 0.04,       # gpt-image-1 standard
}
_IMAGE_PRICE_DEFAULT = 0.04

# Per-character TTS price, USD. Free/local engines are $0.
_TTS_PRICE_PER_CHAR: Dict[str, float] = {
    "elevenlabs": 0.00003,  # ~$0.30 / 10k chars on mid-tier plans
}

# Perplexity Sonar token pricing (USD per million tokens), used when the API
# returns usage. Falls back to the flat per-request figure above otherwise.
_PERPLEXITY_TOKEN_PRICE = {
    "input_per_million": 1.0,
    "output_per_million": 1.0,
}


def _config_overrides() -> Dict[str, Any]:
    """Read ``cost_tracking.prices`` from config.yaml (best-effort)."""
    try:
        from hermes_cli.config import load_config
        section = load_config().get("cost_tracking", {})
        if isinstance(section, dict):
            prices = section.get("prices")
            if isinstance(prices, dict):
                return prices
    except Exception:
        pass
    return {}


def _lookup(table: Dict[str, float], backend: Optional[str], default: Optional[float]) -> Optional[float]:
    key = (backend or "").lower().strip()
    return table.get(key, default)


def search_cost(backend: Optional[str], *, usage: Optional[dict] = None) -> Tuple[Optional[float], str, str]:
    """Return (amount_usd, status, units_label) for one web_search call."""
    overrides = _config_overrides().get("search", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    key = (backend or "").lower().strip()

    # Perplexity: prefer token-based costing when the API reported usage.
    if key == "perplexity" and isinstance(usage, dict):
        amount = _perplexity_token_cost(usage)
        if amount is not None:
            return amount, STATUS_ESTIMATED, "1 search (tokens)"

    price = overrides.get(key, _lookup(_SEARCH_PRICE, key, None))
    if price is None:
        return None, STATUS_ESTIMATED, "1 search"
    return float(price), STATUS_ESTIMATED, "1 search"


def extract_cost(backend: Optional[str], url_count: int) -> Tuple[Optional[float], str, str]:
    """Return (amount_usd, status, units_label) for a web_extract call."""
    n = max(int(url_count or 0), 0)
    overrides = _config_overrides().get("extract", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    key = (backend or "").lower().strip()
    price = overrides.get(key, _lookup(_EXTRACT_PRICE_PER_URL, key, None))
    units = f"{n} url" if n == 1 else f"{n} urls"
    if price is None or n == 0:
        return (0.0 if n == 0 else None), STATUS_ESTIMATED, units
    return float(price) * n, STATUS_ESTIMATED, units


def crawl_cost(backend: Optional[str], page_count: int) -> Tuple[Optional[float], str, str]:
    """Return (amount_usd, status, units_label) for a web_crawl call."""
    n = max(int(page_count or 0), 0)
    overrides = _config_overrides().get("crawl", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    key = (backend or "").lower().strip()
    price = overrides.get(key, _lookup(_CRAWL_PRICE_PER_PAGE, key, None))
    units = f"{n} page" if n == 1 else f"{n} pages"
    if price is None or n == 0:
        return (0.0 if n == 0 else None), STATUS_ESTIMATED, units
    return float(price) * n, STATUS_ESTIMATED, units


def image_cost(backend: Optional[str], image_count: int) -> Tuple[Optional[float], str, str]:
    """Return (amount_usd, status, units_label) for image generation."""
    n = max(int(image_count or 0), 1)
    overrides = _config_overrides().get("image", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    key = (backend or "").lower().strip()
    price = overrides.get(key, _lookup(_IMAGE_PRICE_PER_IMAGE, key, _IMAGE_PRICE_DEFAULT))
    units = f"{n} image" if n == 1 else f"{n} images"
    return float(price) * n, STATUS_ESTIMATED, units


def tts_cost(provider: Optional[str], char_count: int) -> Tuple[Optional[float], str, str]:
    """Return (amount_usd, status, units_label) for a TTS synthesis call.

    Free/local engines (edge-tts, kitten, neutts) cost $0 → status 'included'.
    """
    n = max(int(char_count or 0), 0)
    overrides = _config_overrides().get("tts", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    key = (provider or "").lower().strip()
    units = f"{n} chars"
    if key not in _TTS_PRICE_PER_CHAR and key not in overrides:
        # Free/local engine.
        return 0.0, STATUS_INCLUDED, units
    price = overrides.get(key, _TTS_PRICE_PER_CHAR.get(key, 0.0))
    return float(price) * n, STATUS_ESTIMATED, units


def _perplexity_token_cost(usage: dict) -> Optional[float]:
    """Estimate Perplexity Sonar token cost from a usage dict, USD."""
    try:
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if prompt == 0 and completion == 0:
        return None
    cost = (
        prompt / 1_000_000 * _PERPLEXITY_TOKEN_PRICE["input_per_million"]
        + completion / 1_000_000 * _PERPLEXITY_TOKEN_PRICE["output_per_million"]
    )
    return cost
