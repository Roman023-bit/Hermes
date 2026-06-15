#!/usr/bin/env python3
"""Agent profile resolver.

A profile maps a specialist name to a concrete delegation target:
``{model, provider, toolsets, reasoning_effort, prompt}``. Profiles live in
the top-level ``agent_profiles:`` config block and may reference entries in
``model_aliases`` by name. This module is pure data resolution — it never
constructs agents — so it is unit-tested in isolation.

Resolution rules for a profile's ``model`` field:
  1. If the string matches a key in ``model_aliases`` -> take that alias's
     ``model`` and ``provider``.
  2. Otherwise treat the string as a literal ``provider/model`` value.
  3. An explicit ``provider`` in the profile always overrides.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def load_full_config() -> dict:
    """Return the full runtime config (not just the delegation subtree).

    Mirrors tools.delegate_tool._load_config, but returns the top-level dict
    so ``agent_profiles`` and ``model_aliases`` are visible. Checks the live
    CLI_CONFIG first, then the persistent config.
    """
    try:
        from cli import CLI_CONFIG

        if CLI_CONFIG:
            return CLI_CONFIG
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        return load_config()
    except Exception:
        return {}


def resolve_profile(profile_name: Optional[str], cfg: dict) -> Optional[Dict[str, Any]]:
    """Resolve ``profile_name`` against ``cfg`` (the full config dict).

    Returns ``{model, provider, toolsets, reasoning_effort, prompt}`` or
    ``None`` when the name is falsy or not found (caller falls back to the
    delegation defaults). ``model`` is alias-resolved; ``provider`` is the
    explicit profile provider, else the alias provider, else ``None``.
    """
    if not profile_name:
        return None
    profiles = (cfg or {}).get("agent_profiles") or {}
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        logger.warning(
            "Unknown agent profile %r; falling back to delegation defaults. "
            "Available: %s",
            profile_name,
            ", ".join(list_profiles(cfg)) or "(none)",
        )
        return None

    raw_model = str(entry.get("model") or "").strip()
    explicit_provider = str(entry.get("provider") or "").strip() or None

    model = raw_model
    alias_provider = None
    aliases = (cfg or {}).get("model_aliases") or {}
    alias = aliases.get(raw_model)
    if isinstance(alias, dict):
        model = str(alias.get("model") or raw_model).strip()
        alias_provider = str(alias.get("provider") or "").strip() or None

    toolsets = entry.get("toolsets")
    if toolsets is not None and not isinstance(toolsets, list):
        toolsets = None

    return {
        "model": model or None,
        "provider": explicit_provider or alias_provider,
        "toolsets": toolsets,
        "reasoning_effort": str(entry.get("reasoning_effort") or "").strip() or None,
        "prompt": str(entry.get("prompt") or "").strip() or None,
    }


def list_profiles(cfg: dict) -> List[str]:
    """Sorted names of configured profiles (for tool-schema descriptions)."""
    profiles = (cfg or {}).get("agent_profiles") or {}
    return sorted(profiles.keys())
