# Agent Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `delegate_task` address a named `profile` that resolves to a concrete `{model, provider, toolsets, reasoning_effort, prompt}`, so each subagent runs on the model and tooling suited to its job.

**Architecture:** A new pure resolver module (`tools/agent_profiles.py`) reads a declarative `agent_profiles:` config block (reusing `model_aliases`). `tools/delegate_tool.py` gains a `profile` parameter (orthogonal to the existing leaf/orchestrator `role`); per-task it resolves the profile into credentials (via the existing `_resolve_delegation_credentials`), toolsets, reasoning effort, and a system-prompt fragment. No `agent_profiles` / no `profile` argument → behavior is unchanged.

**Tech Stack:** Python 3.11, `unittest` (run via `python -m pytest`), existing Hermes delegation/runtime-provider machinery.

**Spec:** `docs/superpowers/specs/2026-06-15-agent-profiles-design.md`

**Test runner note:** Tests live in the repo and run with `python -m pytest`. If `pytest` is unavailable in the active venv, each test file is also runnable as `python tests/tools/test_agent_profiles.py` (the existing `tests/tools/test_delegate.py` follows this dual pattern, see its module docstring).

---

### Task 1: Profile resolver module

**Files:**
- Create: `tools/agent_profiles.py`
- Test: `tests/tools/test_agent_profiles.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_agent_profiles.py`:

```python
#!/usr/bin/env python3
"""Tests for the agent-profile resolver (pure, no agent construction)."""

import unittest

from tools.agent_profiles import resolve_profile, list_profiles


def _cfg():
    return {
        "model_aliases": {
            "research": {"model": "google/gemini-3-pro-preview", "provider": "openrouter"},
            "reviewer": {"model": "openai/gpt-5.3-codex", "provider": "openrouter"},
        },
        "agent_profiles": {
            "researcher": {
                "model": "research",
                "toolsets": ["web"],
                "reasoning_effort": "medium",
                "prompt": "You are a rigorous researcher.",
            },
            "coder": {
                "model": "anthropic/claude-sonnet-4.6",  # literal provider/model
            },
            "forced": {
                "model": "research",
                "provider": "nous",  # explicit provider overrides alias provider
            },
        },
    }


class TestResolveProfile(unittest.TestCase):
    def test_known_profile_full_fields(self):
        p = resolve_profile("researcher", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")
        self.assertEqual(p["provider"], "openrouter")
        self.assertEqual(p["toolsets"], ["web"])
        self.assertEqual(p["reasoning_effort"], "medium")
        self.assertEqual(p["prompt"], "You are a rigorous researcher.")

    def test_model_is_alias_resolved(self):
        p = resolve_profile("researcher", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")

    def test_model_is_literal_when_not_an_alias(self):
        p = resolve_profile("coder", _cfg())
        self.assertEqual(p["model"], "anthropic/claude-sonnet-4.6")
        self.assertIsNone(p["provider"])

    def test_explicit_provider_overrides_alias_provider(self):
        p = resolve_profile("forced", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")
        self.assertEqual(p["provider"], "nous")

    def test_unknown_profile_returns_none(self):
        self.assertIsNone(resolve_profile("nope", _cfg()))

    def test_missing_block_returns_none(self):
        self.assertIsNone(resolve_profile("researcher", {}))

    def test_none_name_returns_none(self):
        self.assertIsNone(resolve_profile(None, _cfg()))


class TestListProfiles(unittest.TestCase):
    def test_lists_sorted_names(self):
        self.assertEqual(list_profiles(_cfg()), ["coder", "forced", "researcher"])

    def test_empty_when_missing(self):
        self.assertEqual(list_profiles({}), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_agent_profiles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.agent_profiles'`

- [ ] **Step 3: Write the resolver module**

Create `tools/agent_profiles.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_agent_profiles.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/agent_profiles.py tests/tools/test_agent_profiles.py
git commit -m "feat(delegate): add agent-profile resolver"
```

---

### Task 2: Inject profile prompt into child system prompt

**Files:**
- Modify: `tools/delegate_tool.py:533-606` (`_build_child_system_prompt`)
- Test: `tests/tools/test_delegate.py` (append to `TestChildSystemPrompt`)

- [ ] **Step 1: Write the failing test**

Append to class `TestChildSystemPrompt` in `tests/tools/test_delegate.py`:

```python
    def test_profile_prompt_injected(self):
        prompt = _build_child_system_prompt(
            "Fix the tests", profile_prompt="You are a critical code reviewer."
        )
        self.assertIn("ROLE:", prompt)
        self.assertIn("You are a critical code reviewer.", prompt)
        # Role framing precedes the task block.
        self.assertLess(prompt.index("ROLE:"), prompt.index("YOUR TASK"))

    def test_no_profile_prompt_no_role_block(self):
        prompt = _build_child_system_prompt("Fix the tests")
        self.assertNotIn("ROLE:", prompt)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_delegate.py -k profile_prompt -v`
Expected: FAIL — `TypeError: _build_child_system_prompt() got an unexpected keyword argument 'profile_prompt'`

- [ ] **Step 3: Add the parameter and injection**

In `tools/delegate_tool.py`, change the signature of `_build_child_system_prompt` (line 533-541) to add `profile_prompt`:

```python
def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    role: str = "leaf",
    max_spawn_depth: int = 2,
    child_depth: int = 1,
    profile_prompt: Optional[str] = None,
) -> str:
```

Then replace the initial `parts = [...]` block (lines 550-554) with:

```python
    parts = ["You are a focused subagent working on a specific delegated task."]
    if profile_prompt and profile_prompt.strip():
        parts.append(f"\nROLE:\n{profile_prompt.strip()}")
    parts.append("")
    parts.append(f"YOUR TASK:\n{goal}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_delegate.py -k "profile_prompt or goal_only or goal_with_context" -v`
Expected: PASS (existing prompt tests still pass + 2 new)

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate.py
git commit -m "feat(delegate): inject profile prompt into child system prompt"
```

---

### Task 3: Merge a profile onto the delegation credential config

**Files:**
- Modify: `tools/delegate_tool.py` (add `_merge_profile_into_cfg` near `_resolve_delegation_credentials`, ~line 2236)
- Test: `tests/tools/test_delegate.py` (new class)

- [ ] **Step 1: Write the failing test**

Append a new class to `tests/tools/test_delegate.py`:

```python
class TestMergeProfileIntoCfg(unittest.TestCase):
    def test_profile_overrides_model_and_provider(self):
        from tools.delegate_tool import _merge_profile_into_cfg

        base = {"model": "openai/gpt-5.3-codex", "provider": "openrouter", "max_iterations": 50}
        profile = {"model": "google/gemini-3-pro-preview", "provider": "nous"}
        merged = _merge_profile_into_cfg(base, profile)
        self.assertEqual(merged["model"], "google/gemini-3-pro-preview")
        self.assertEqual(merged["provider"], "nous")
        self.assertEqual(merged["max_iterations"], 50)  # untouched
        self.assertEqual(base["model"], "openai/gpt-5.3-codex")  # base not mutated

    def test_none_profile_returns_copy_of_base(self):
        from tools.delegate_tool import _merge_profile_into_cfg

        base = {"model": "x", "provider": "y"}
        merged = _merge_profile_into_cfg(base, None)
        self.assertEqual(merged, base)
        self.assertIsNot(merged, base)

    def test_profile_without_provider_keeps_base_provider(self):
        from tools.delegate_tool import _merge_profile_into_cfg

        base = {"model": "x", "provider": "openrouter"}
        merged = _merge_profile_into_cfg(base, {"model": "z", "provider": None})
        self.assertEqual(merged["model"], "z")
        self.assertEqual(merged["provider"], "openrouter")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_delegate.py -k MergeProfile -v`
Expected: FAIL — `ImportError: cannot import name '_merge_profile_into_cfg'`

- [ ] **Step 3: Add the helper**

In `tools/delegate_tool.py`, add directly above `def _resolve_delegation_credentials` (line 2236):

```python
def _merge_profile_into_cfg(delegation_cfg: dict, profile: Optional[dict]) -> dict:
    """Overlay a resolved profile's model/provider onto the delegation cfg.

    Returns a new dict (never mutates ``delegation_cfg``). Only ``model`` and
    ``provider`` are overlaid, and only when the profile sets them, so the
    result feeds straight into ``_resolve_delegation_credentials`` reusing the
    existing provider-resolution path. ``None``/empty profile -> a plain copy.
    """
    merged = dict(delegation_cfg or {})
    if profile:
        if profile.get("model"):
            merged["model"] = profile["model"]
        if profile.get("provider"):
            merged["provider"] = profile["provider"]
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_delegate.py -k MergeProfile -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate.py
git commit -m "feat(delegate): add profile->delegation cfg overlay helper"
```

---

### Task 4: Thread profile into `_build_child_agent`

**Files:**
- Modify: `tools/delegate_tool.py:834-855` (signature), `:932-939` (prompt call), `:1010-1025` (reasoning effort)
- Test: `tests/tools/test_delegate.py` (new class)

- [ ] **Step 1: Write the failing test**

Append a new class to `tests/tools/test_delegate.py`:

```python
class TestBuildChildAgentProfile(unittest.TestCase):
    def test_profile_prompt_reaches_system_prompt(self):
        from unittest.mock import patch
        import tools.delegate_tool as dt

        captured = {}

        def _fake_prompt(goal, context=None, **kw):
            captured.update(kw)
            return "stub-prompt"

        parent = _make_mock_parent()
        parent.enabled_toolsets = ["terminal", "file"]
        with patch.object(dt, "_build_child_system_prompt", side_effect=_fake_prompt), \
             patch.object(dt, "AIAgent", create=True) as _agent:
            dt._build_child_agent(
                task_index=0,
                goal="do it",
                context=None,
                toolsets=None,
                model="google/gemini-3-pro-preview",
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                profile_prompt="You are a researcher.",
                profile_reasoning_effort="high",
            )
        self.assertEqual(captured.get("profile_prompt"), "You are a researcher.")
```

> Note: `_build_child_agent` calls `AIAgent(...)` from `run_agent`. The patch
> on `dt.AIAgent` neutralizes construction; we only assert the prompt kwarg.
> If `AIAgent` is imported lazily inside the function, patch the import site
> instead (see the existing `TestBuildChildAgent` tests in this file for the
> established patch target).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_delegate.py -k BuildChildAgentProfile -v`
Expected: FAIL — `TypeError: _build_child_agent() got an unexpected keyword argument 'profile_prompt'`

- [ ] **Step 3: Add the parameters and wire them through**

In `tools/delegate_tool.py`, extend the `_build_child_agent` signature (after `role: str = "leaf",` at line 854):

```python
    role: str = "leaf",
    profile_prompt: Optional[str] = None,
    profile_reasoning_effort: Optional[str] = None,
):
```

In the `_build_child_system_prompt(...)` call (line 932-939), add the prompt arg:

```python
    child_prompt = _build_child_system_prompt(
        goal,
        context,
        workspace_path=workspace_hint,
        role=effective_role,
        max_spawn_depth=max_spawn,
        child_depth=child_depth,
        profile_prompt=profile_prompt,
    )
```

In the reasoning-effort block (line 1014), prefer the profile value over the delegation default:

```python
        delegation_effort = str(
            profile_reasoning_effort or delegation_cfg.get("reasoning_effort") or ""
        ).strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_delegate.py -k BuildChildAgentProfile -v`
Expected: PASS

> If Step 4 fails because the `AIAgent` patch target is wrong, adjust the
> patch to match how the existing `_build_child_agent` tests in this file
> stub agent construction, then re-run. Do not change the production code to
> fit the test.

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate.py
git commit -m "feat(delegate): thread profile prompt and reasoning into child agent"
```

---

### Task 5: Add `profile` to the tool schema

**Files:**
- Modify: `tools/delegate_tool.py:2410-2494` (`DELEGATE_TASK_SCHEMA`)
- Test: `tests/tools/test_delegate.py` (extend `TestDelegateRequirements`)

- [ ] **Step 1: Write the failing test**

Append to class `TestDelegateRequirements` in `tests/tools/test_delegate.py`:

```python
    def test_schema_has_profile(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("profile", props)
        self.assertEqual(props["profile"]["type"], "string")
        # per-task profile override exists too
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("profile", task_props)
        # role axis is untouched
        self.assertIn("role", props)
        self.assertEqual(props["role"]["enum"], ["leaf", "orchestrator"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_delegate.py -k test_schema_has_profile -v`
Expected: FAIL — `KeyError: 'profile'`

- [ ] **Step 3: Add `profile` to the schema**

In `tools/delegate_tool.py`, inside the `tasks.items.properties` object, add after the per-task `role` block (line 2465-2469):

```python
                        "profile": {
                            "type": "string",
                            "description": "Per-task specialist profile (see top-level 'profile').",
                        },
```

And add a top-level `profile` property after the top-level `role` block (after line 2494):

```python
            "profile": {
                "type": "string",
                "description": (
                    "Specialist profile for the subagent — selects its model, "
                    "toolset, reasoning effort, and role framing from the "
                    "agent_profiles config. Orthogonal to 'role' (leaf vs "
                    "orchestrator): a profile says WHO the worker is, role says "
                    "whether it may delegate further. Unknown/omitted profiles "
                    "fall back to the delegation defaults. Per-task 'profile' "
                    "overrides this top-level value."
                ),
            },
```

> The available profile names are intentionally NOT hardcoded into the
> description string (they are user-configurable). Keep the description
> generic as written above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_delegate.py -k "test_schema_has_profile or test_schema_valid" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate.py
git commit -m "feat(delegate): expose profile parameter in tool schema"
```

---

### Task 6: Wire `profile` through `delegate_task`

**Files:**
- Modify: `tools/delegate_tool.py:1819-1829` (signature), `:1856-1857` (normalization), `:1896-1916` (creds + single-task dict), `:1947-1974` (per-task build loop)
- Test: `tests/tools/test_delegate.py` (new class)

- [ ] **Step 1: Write the failing test**

Append a new class to `tests/tools/test_delegate.py`:

```python
class TestDelegateProfileWiring(unittest.TestCase):
    """Verify delegate_task resolves per-task profile -> model before build."""

    class _StopBuild(Exception):
        pass

    def _run(self, tasks=None, goal=None, profile=None):
        from unittest.mock import patch
        import tools.delegate_tool as dt

        captured = []

        def _fake_build(**kw):
            captured.append(kw)
            raise self._StopBuild()

        full_cfg = {
            "delegation": {"model": "openai/gpt-5.3-codex", "provider": "openrouter",
                           "max_iterations": 50},
            "model_aliases": {
                "research": {"model": "google/gemini-3-pro-preview", "provider": "openrouter"},
            },
            "agent_profiles": {
                "researcher": {"model": "research", "toolsets": ["web"],
                               "prompt": "You are a researcher."},
            },
        }
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["terminal", "file", "web"]
        with patch.object(dt, "_load_config", return_value=full_cfg["delegation"]), \
             patch("tools.agent_profiles.load_full_config", return_value=full_cfg), \
             patch.object(dt, "_resolve_delegation_credentials",
                          side_effect=lambda cfg, pa: {"model": cfg.get("model"),
                                                        "provider": cfg.get("provider"),
                                                        "base_url": None, "api_key": None,
                                                        "api_mode": None}), \
             patch.object(dt, "_build_child_agent", side_effect=_fake_build):
            try:
                dt.delegate_task(tasks=tasks, goal=goal, profile=profile, parent_agent=parent)
            except self._StopBuild:
                pass
        return captured

    def test_single_task_profile_sets_model_and_prompt(self):
        cap = self._run(goal="research X", profile="researcher")
        self.assertEqual(cap[0]["model"], "google/gemini-3-pro-preview")
        self.assertEqual(cap[0]["profile_prompt"], "You are a researcher.")
        self.assertEqual(cap[0]["toolsets"], ["web"])

    def test_per_task_profile_beats_top_level(self):
        cap = self._run(
            tasks=[{"goal": "a", "profile": "researcher"}],
            profile=None,
        )
        self.assertEqual(cap[0]["model"], "google/gemini-3-pro-preview")

    def test_explicit_toolsets_beat_profile(self):
        cap = self._run(
            tasks=[{"goal": "a", "profile": "researcher", "toolsets": ["terminal"]}],
        )
        self.assertEqual(cap[0]["toolsets"], ["terminal"])

    def test_no_profile_uses_delegation_default_model(self):
        cap = self._run(goal="plain task")
        self.assertEqual(cap[0]["model"], "openai/gpt-5.3-codex")
        self.assertIsNone(cap[0].get("profile_prompt"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_delegate.py -k DelegateProfileWiring -v`
Expected: FAIL — `delegate_task() got an unexpected keyword argument 'profile'`

- [ ] **Step 3: Wire `profile` through `delegate_task`**

3a. Extend the signature (line 1819-1829) — add `profile` after `role`:

```python
    role: Optional[str] = None,
    profile: Optional[str] = None,
    parent_agent=None,
) -> str:
```

3b. After `top_role = _normalize_role(role)` (line 1857), add:

```python
    top_profile = profile
    from tools.agent_profiles import resolve_profile, load_full_config

    _full_cfg = load_full_config()
    _profile_creds_cache: Dict[Any, Any] = {}

    def _creds_for_profile(prof: Optional[dict]):
        key = (prof.get("model") if prof else None, prof.get("provider") if prof else None)
        if key not in _profile_creds_cache:
            merged = _merge_profile_into_cfg(cfg, prof)
            _profile_creds_cache[key] = _resolve_delegation_credentials(merged, parent_agent)
        return _profile_creds_cache[key]
```

> `cfg` is defined just below at line 1876 (`cfg = _load_config()`). Move the
> three lines above (`top_profile`, the import, and the cache/closure) to
> directly AFTER `cfg = _load_config()` so `cfg` is in scope. The default
> `creds` resolution at line 1896-1899 stays as the no-profile fallback.

3c. In the single-task normalization (line 1913-1916), carry the profile:

```python
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [
            {"goal": goal, "context": context, "toolsets": toolsets,
             "role": top_role, "profile": top_profile}
        ]
```

3d. In the per-task build loop (line 1947-1974), resolve the profile per task and
pass profile-derived values to `_build_child_agent`. Replace the loop body's
`effective_role`/`_build_child_agent(...)` section with:

```python
        for i, t in enumerate(task_list):
            task_acp_args = t.get("acp_args") if "acp_args" in t else None
            effective_role = _normalize_role(t.get("role") or top_role)
            profile_name = t.get("profile") or top_profile
            prof = resolve_profile(profile_name, _full_cfg)
            task_creds = _creds_for_profile(prof) if prof else creds
            task_toolsets = t.get("toolsets") or (prof.get("toolsets") if prof else None) or toolsets
            child = _build_child_agent(
                task_index=i,
                goal=t["goal"],
                context=t.get("context"),
                toolsets=task_toolsets,
                model=task_creds["model"],
                max_iterations=effective_max_iter,
                task_count=n_tasks,
                parent_agent=parent_agent,
                override_provider=task_creds["provider"],
                override_base_url=task_creds["base_url"],
                override_api_key=task_creds["api_key"],
                override_api_mode=task_creds["api_mode"],
                override_acp_command=t.get("acp_command")
                or acp_command
                or task_creds.get("command"),
                override_acp_args=(
                    task_acp_args
                    if task_acp_args is not None
                    else (acp_args if acp_args is not None else task_creds.get("args"))
                ),
                role=effective_role,
                profile_prompt=(prof.get("prompt") if prof else None),
                profile_reasoning_effort=(prof.get("reasoning_effort") if prof else None),
            )
            child._delegate_saved_tool_names = _parent_tool_names
            children.append((i, t, child))
```

> This replaces the use of the single `creds[...]` (line 1957-1972) with the
> per-task `task_creds`. The top-level `creds` resolution at line 1896-1899
> remains as the fallback when a task has no profile.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_delegate.py -k DelegateProfileWiring -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full delegate suite to check for regressions**

Run: `python -m pytest tests/tools/test_delegate.py -v`
Expected: PASS (all pre-existing tests + new ones)

- [ ] **Step 6: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate.py
git commit -m "feat(delegate): resolve per-task profile to model/toolset/prompt"
```

---

### Task 7: Ship the default profile catalog (config)

**Files:**
- Modify: `cli-config.yaml.example` (documented block near `model_aliases`, ~line 989)
- Modify: `C:\Users\mizan\AppData\Local\hermes\config.yaml` (live runtime, append top-level block)

- [ ] **Step 1: Add a documented block to `cli-config.yaml.example`**

Immediately after the `model_aliases:` example block (ends ~line 997+), add:

```yaml
# agent_profiles:
#   # Specialist profiles for delegate_task(profile=...). Each maps a name to a
#   # model (alias or provider/model), optional provider, toolsets, reasoning
#   # effort, and a system-prompt fragment. Orthogonal to leaf/orchestrator role.
#   researcher:
#     model: research            # alias from model_aliases, or provider/model
#     toolsets: [web]
#     reasoning_effort: medium
#     prompt: "You are a rigorous researcher. Gather evidence, cite sources, separate fact from inference, flag uncertainty."
#   reviewer:
#     model: reviewer
#     toolsets: [terminal, file]
#     reasoning_effort: high
#     prompt: "You are a critical code reviewer. Hunt for correctness bugs, edge cases, and security issues. Be specific with file:line."
#   strategist:
#     model: strateg
#     reasoning_effort: high
#     prompt: "You are a strategist. Think in trade-offs and second-order effects. Give a recommendation, not a survey."
#   coder:
#     model: reviewer
#     toolsets: [terminal, file]
#     reasoning_effort: medium
#     prompt: "You are a focused implementer. Match surrounding code style. Make minimal, correct changes."
#   writer:
#     model: copywriter
#     toolsets: [web]
#     prompt: "You are a sharp copywriter. Clear, concrete, no filler."
#   fast:
#     model: flash
#     reasoning_effort: low
#     prompt: "You are a quick worker for simple, well-defined tasks. Be concise."
#   analyst:
#     model: strateg
#     toolsets: [terminal, file]
#     reasoning_effort: high
#     prompt: "You are a data analyst. Query precisely, verify numbers, show your method, and state caveats and sample sizes."
#   tester:
#     model: reviewer
#     toolsets: [terminal, file]
#     reasoning_effort: high
#     prompt: "You are a test engineer. Write focused failing tests first, cover edge cases, and run them to confirm behavior before claiming a pass."
```

- [ ] **Step 2: Add the live block to the runtime config**

Append to `C:\Users\mizan\AppData\Local\hermes\config.yaml` (top-level, e.g. after the `model_aliases:` block) the SAME 8-profile catalog but UNcommented (real YAML, mirroring the names/values above). Preserve existing indentation style (2 spaces).

- [ ] **Step 3: Verify the runtime config parses**

Run: `python -c "import yaml,io; yaml.safe_load(open(r'C:\Users\mizan\AppData\Local\hermes\config.yaml', encoding='utf-8')); print('config OK')"`
Expected: `config OK`

- [ ] **Step 4: Verify the resolver sees the live profiles**

Run: `python -c "from tools.agent_profiles import resolve_profile; import yaml; cfg=yaml.safe_load(open(r'C:\Users\mizan\AppData\Local\hermes\config.yaml', encoding='utf-8')); print(resolve_profile('researcher', cfg))"`
Expected: a dict with `model` resolved to the alias target (e.g. `google/gemini-3-pro-preview`) and `toolsets` `['web']`.

- [ ] **Step 5: Commit (repo template only)**

```bash
git add cli-config.yaml.example
git commit -m "docs(config): document agent_profiles catalog"
```

> The runtime `config.yaml` under `AppData` is not part of the repo; do not
> `git add` it. It is edited in place so the live gateway picks it up.

---

### Task 8: Deploy code to the live runtime and smoke-test

**Files:** none (deployment + verification)

- [ ] **Step 1: Sync the changed package files into the runtime**

The live runtime imports from the installed package at `HERMES_HOME` /
`pypi-venv`, NOT from this repo. Copy the two changed source files into the
installed package location (find it first):

Run: `python -c "import tools.delegate_tool as d; print(d.__file__)"` from the
runtime venv to locate the installed `tools/` directory, then copy
`tools/agent_profiles.py` and `tools/delegate_tool.py` over the installed
versions. (If the runtime instead runs from an editable/`-e` install pointing
at this repo, this step is a no-op — verify with the path printed above.)

- [ ] **Step 2: Restart the gateway**

Restart the Hermes gateway (same procedure used previously) and confirm clean
startup in `gateway.log` — no import errors from `tools.agent_profiles` or
`tools.delegate_tool`.

- [ ] **Step 3: Live smoke test**

From a Hermes session, issue a delegation that names a profile, e.g.
"delegate to the `researcher` profile: summarize X". Confirm in the run/agent
output that the child reports the expected model (e.g. gemini-3-pro) and that
the role framing appears. Confirm a delegation WITHOUT a profile still uses the
`delegation.model` default (regression check).

- [ ] **Step 4: Final commit / branch wrap-up**

Ensure all repo commits are on `feat/agent-profiles`. Hand off per the
finishing-a-development-branch skill (merge to main or open a PR per the
user's preference).

---

## Self-Review

**Spec coverage:**
- Two orthogonal axes (`profile` vs `role`) → Tasks 5, 6 (schema + wiring; `role` untouched, asserted in Task 5).
- `agent_profiles` schema + alias resolution + explicit-provider override → Task 1.
- Default 8-profile catalog → Task 7.
- Model resolution precedence (explicit > profile > delegation > inherit) → Task 6 (`task_creds`, `task_toolsets`).
- toolsets precedence (explicit > profile > inherit) → Task 6 (`test_explicit_toolsets_beat_profile`).
- reasoning_effort override → Task 4.
- prompt fragment injection → Tasks 2, 4, 6.
- Soft-degrade on unknown profile / empty block → Task 1 (`test_unknown_profile_returns_none`, `test_missing_block_returns_none`) + Task 6 (`prof` falsy → `creds` fallback).
- Backward compatibility (no profile) → Task 6 (`test_no_profile_uses_delegation_default_model`).
- Per-profile credential caching → Task 6 (`_creds_for_profile` cache).
- Deployment to runtime → Task 8.

**Placeholder scan:** No TBD/TODO; every code step shows full code. The one
discovery step (Task 8 Step 1, locating the installed package path) is an
inherent environment lookup, not a code placeholder, and includes the exact
command to resolve it.

**Type consistency:** `resolve_profile` returns keys `{model, provider, toolsets,
reasoning_effort, prompt}` (Task 1) — consumed with those exact keys in Task 6
(`prof.get("prompt")`, `prof.get("toolsets")`, `prof.get("reasoning_effort")`)
and Task 4 params (`profile_prompt`, `profile_reasoning_effort`).
`_merge_profile_into_cfg(delegation_cfg, profile)` signature (Task 3) matches
its call in `_creds_for_profile` (Task 6). `_build_child_agent` new params
`profile_prompt`/`profile_reasoning_effort` (Task 4) match the call site
(Task 6) and the `_build_child_system_prompt(profile_prompt=...)` arg (Task 2).
