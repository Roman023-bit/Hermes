# Agent Profiles — Role-Based Delegation & Per-Subagent Model Routing

**Date:** 2026-06-15
**Status:** Approved design (pre-implementation)
**Area:** Orchestration / multi-agent delegation
**Approach:** A — declarative role catalog (chosen over B auto-router and C combined)

## Problem

Hermes can already spawn subagents (`tools/delegate_tool.py`), but every child in a
batch is built with `model=creds["model"]`, where `creds` is resolved **once** from
`delegation.model` (currently `openai/gpt-5.3-codex`). Two consequences:

1. **Roles are a fiction for model selection.** The subagent `role` parameter
   (`delegate_tool.py:307,538,2482`) controls *only* whether a child may delegate
   further (`leaf` vs `orchestrator`). It does not select model, toolset, or prompt.
   The existing `model_aliases` (`reviewer`, `strateg`, `research`, `copywriter`, …)
   are CLI-only model switches and are not wired into delegation.
2. **No specialization.** All subagents run the same model with no role-specific
   toolset, reasoning effort, or system-prompt framing. The orchestrator exists, but
   there is no *team of specialists*.

This makes the user's two stated pain points — **multi-agent** and **model
selection** — one underlying gap.

## Goal

Let delegation address a named **profile** (specialist) that resolves to a concrete
`{model, provider, toolsets, reasoning_effort, prompt}`, so each subagent in a batch
can run on the model and tooling appropriate to its job, while reusing the existing
`model_aliases` and credential-resolution machinery.

Non-goals (explicitly deferred): complexity-based auto-routing (Approach B) and an
orchestrator that auto-assigns profiles (Approach C). The design is built so B can be
layered on later without rework.

## Architecture — two orthogonal axes

| Axis | Parameter | Controls | Values |
|------|-----------|----------|--------|
| **Who** (specialization) | `profile` *(new)* | model + toolsets + reasoning + prompt fragment | `researcher`, `reviewer`, `strategist`, `coder`, `writer`, `fast`, `analyst`, `tester` |
| **Can delegate further** | `role` *(unchanged)* | leaf vs orchestrator | `leaf` / `orchestrator` |

The two axes are independent: e.g. `profile=researcher, role=leaf` or
`profile=strategist, role=orchestrator`.

### Model resolution precedence (per subagent)

```
explicit per-task model/toolsets  >  agent_profiles[profile]  >  delegation.*  >  inherit from parent
```

This replaces the single shared `creds["model"]` at `delegate_tool.py:1957`.

### Reuse

- `model_aliases` — a profile's `model` may be an alias name.
- `_resolve_delegation_credentials` — called per resolved profile (via a merged config
  overlay) instead of once, so provider/auth resolution is unchanged.
- The existing system-prompt fragment mechanism used for `role='orchestrator'`
  (`delegate_tool.py:575`) — extended to inject the profile prompt.

### Isolation principle

The profile catalog is pure data. The resolver is a single pure function
(`profile_name → {model, provider, toolsets, reasoning_effort, prompt}`) with no agent
construction, unit-tested in isolation. `delegate_task` only calls it.

## Config schema — `agent_profiles:`

New top-level block in `config.yaml`. Per profile (only `model` required):

```yaml
agent_profiles:
  <name>:
    model: <alias | provider/model>   # required; alias resolved via model_aliases
    provider: <str>                   # optional; else from alias / delegation / parent
    toolsets: [<str>, ...]            # optional; default = inherit parent toolsets
    reasoning_effort: low|medium|high # optional; default = delegation / parent
    prompt: <str>                     # optional; fragment added to child system prompt
```

**`model` resolution rules:**
1. If the string matches a key in `model_aliases` → take `{model, provider}` from it.
2. Otherwise treat as `provider/model` directly.
3. An explicit `provider:` in the profile always overrides.

### Default catalog (shipped; user-editable)

```yaml
agent_profiles:
  researcher:
    model: research            # gemini-3-pro-preview
    toolsets: [web]
    reasoning_effort: medium
    prompt: "You are a rigorous researcher. Gather evidence, cite sources, separate fact from inference, flag uncertainty."
  reviewer:
    model: reviewer            # gpt-5.3-codex
    toolsets: [terminal, file]
    reasoning_effort: high
    prompt: "You are a critical code reviewer. Hunt for correctness bugs, edge cases, and security issues. Be specific with file:line."
  strategist:
    model: strateg             # opus-4.8
    reasoning_effort: high
    prompt: "You are a strategist. Think in trade-offs and second-order effects. Give a recommendation, not a survey."
  coder:
    model: reviewer            # gpt-5.3-codex
    toolsets: [terminal, file]
    reasoning_effort: medium
    prompt: "You are a focused implementer. Match surrounding code style. Make minimal, correct changes."
  writer:
    model: copywriter          # sonnet-4.6
    toolsets: [web]
    prompt: "You are a sharp copywriter. Clear, concrete, no filler."
  fast:
    model: flash               # gemini-3.5-flash
    reasoning_effort: low
    prompt: "You are a quick worker for simple, well-defined tasks. Be concise."
  analyst:
    model: strateg             # opus-4.8
    toolsets: [terminal, file]
    reasoning_effort: high
    prompt: "You are a data analyst. Query precisely, verify numbers, show your method, and state caveats and sample sizes."
  tester:
    model: reviewer            # gpt-5.3-codex
    toolsets: [terminal, file]
    reasoning_effort: high
    prompt: "You are a test engineer. Write focused failing tests first, cover edge cases, and run them to confirm behavior before claiming a pass."
```

### Error handling

- Unknown `profile` → soft degrade: log warning + fall back to `delegation.*` defaults
  (mirrors `_normalize_role`). The task is not failed.
- Profile references a missing alias/provider → clear `ValueError` listing available
  profiles (consistent with `_resolve_delegation_credentials`).
- Empty / missing `agent_profiles:` or no `profile` in the call → behavior is identical
  to today (full backward compatibility).

### Conflict precedence for `toolsets`

`explicit per-task toolsets` > `profile.toolsets` > `inherit from parent`.

## Code changes

### New module `tools/agent_profiles.py`

- `resolve_profile(profile_name, cfg) -> dict | None`
  Reads `agent_profiles[name]`, resolves `model` through `model_aliases`, returns
  `{model, provider, toolsets, reasoning_effort, prompt}`. Unknown name → `None` + warning.
- `list_profiles(cfg) -> list[str]` — for the tool description (like `_TOOLSET_LIST_STR`).

### `tools/delegate_tool.py`

1. **Tool schema** (`:2410+`): add `profile` (string) at top-level and in
   `tasks[].properties`; description includes the dynamic list of available profiles.
   `role` is left unchanged.
2. **`delegate_task` signature** (`:1819`) and per-task normalization: `profile`
   per-task beats top-level (mirrors `role` handling).
3. **Per-task credential resolution** (`:1897` / `:1957`): instead of resolving
   `_resolve_delegation_credentials` once, overlay the profile onto the delegation
   config per task and resolve:
   ```
   merged = {**delegation_cfg, **profile_overrides}
   creds  = _resolve_delegation_credentials(merged, parent_agent)   # cache by (model, provider)
   ```
   This reuses the entire existing provider-resolution path; no new auth logic.
4. **toolsets**: `t.get("toolsets") or profile.toolsets or toolsets`.
5. **reasoning_effort**: profile overrides `delegation.reasoning_effort` in the block at `:1010`.
6. **prompt fragment**: thread `profile.prompt` into `_build_child_agent` → the system
   prompt builder, alongside the existing orchestrator fragment (`:575`).

### Config files

- `agent_profiles:` (8 profiles) added to `AppData\Local\hermes\config.yaml` (the live
  runtime reads it immediately) **and** to the default config template in the repo.

## Testing

- `tests/tools/test_agent_profiles.py` (unit, pure resolver):
  - known profile → correct `{model, provider, toolsets, reasoning_effort, prompt}`
  - `model` = alias name → resolved via `model_aliases`
  - `model` = `provider/model` literal → used as-is
  - explicit `provider` overrides alias provider
  - unknown profile → `None`
  - missing/empty `agent_profiles` → `None`
- Extend `tests/tools/test_delegate.py`:
  - `delegate_task(profile=...)` builds a child with the profile's model/toolset
  - per-task `profile` beats top-level `profile`
  - explicit `toolsets` beats `profile.toolsets`
  - profile prompt fragment appears in the child system prompt
  - mocks consistent with existing delegation tests.

## Compatibility & deployment

- No `agent_profiles` / no `profile` argument → behavior identical to today.
- Code (resolver, schema) lives in the runtime pip package. After repo changes, the
  package must be **synced into `AppData\Local\hermes`** (reinstall/copy) or the live
  Hermes will not see the code. The config-only part (`agent_profiles:`) is picked up
  immediately. This is a distinct implementation step.
- `pytest` is not installed in the runtime `pypi-venv`; tests run from the repo. If
  pytest remains unavailable, validate via the inline driver with the same mocks (as
  done for the `hermes send` fix).

## Future extension (out of scope)

Approach B (complexity-based auto-router) can sit on top: a lightweight classifier
picks a `profile`/tier for the main agent and for subagents that were spawned without
an explicit `profile`. The profile catalog defined here is its target vocabulary.
