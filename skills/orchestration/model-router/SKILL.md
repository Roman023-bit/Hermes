---
name: model-router
description: "Route each request to the cost/quality-optimal model: pick the lane, switch the main model via /model alias between tasks, and delegate review/research sub-tasks to the global delegation.model. Load whenever choosing which model to run."
version: 1.0.0
author: Roman
license: MIT
metadata:
  hermes:
    tags: [Orchestration, Model Routing, Cost Optimization, Delegation, Code Review, Marketing]
    category: orchestration
---

# Model Router — cost/quality-optimal model selection

Decide WHICH model handles each request. Two routing layers:

1. **Main model:** between tasks, switch with `/model <alias>`.
   Never switch mid-task — it breaks the prompt cache.
2. **Subagents (delegation):** `delegate_task(...)` does NOT take a per-call
   `model`. Every delegated child runs on the GLOBAL `delegation.model` from
   config (here: `reviewer` = gpt-5.3-codex), in an isolated context so the
   main session's cache is untouched. To send delegated work to a different
   model, change `delegation.model` in config — it cannot be set per call.

## Aliases (must exist in config `model_aliases`)

| Alias | Slug | Tier |
|-------|------|------|
| `sonnet` | `anthropic/claude-sonnet-4.6` | default writer ($3/$15) |
| `opus` | `anthropic/claude-opus-4.8` | heavy ($5/$25) |
| `reviewer` | `openai/gpt-5.3-codex` | code review ($1.75/$14) |
| `research` | `google/gemini-3-pro-preview` | long-context research ($2/$12) |
| `copywriter` | `anthropic/claude-sonnet-4.6` | marketing copy |
| `strateg` | `anthropic/claude-opus-4.8` | marketing strategy |
| `flash` | `google/gemini-3.5-flash` | trivial/bulk |

## Triage — signal → lane (apply on every new request)

| Signal in the request | Lane | Action | reasoning_effort |
|-----------------------|------|--------|------------------|
| "быстро", small fact, draft, bulk rename | trivial | `/model flash` | low |
| ordinary code/edit, typical task | **default** | stay on `sonnet` | medium |
| "архитектура", tricky bug, >2 files, perf | hard-code | `/model opus` | high |
| "проверь / ревью / безопасно ли / найди баг" | review | `delegate_task(...)` → runs on `delegation.model` (= reviewer/Codex) | — |
| "конкуренты / рынок / собери / SEO", many sources | research | `/model research` (Gemini Pro) on the main model; or parallel `delegate_task(...)` IF `delegation.model` is set to research first | medium |
| "стратегия / позиционирование / воронка / аналитика" | mkt-strategy | `/model strateg` | high |
| "напиши пост / лендинг / письмо / оффер" | copywrite | `/model copywriter` | medium |
| "картинка / баннер / креатив" | visual | `image_generate` | — |
| "озвучь / голос" | voice | `text_to_speech` | — |
| critical + ambiguous ("важно, не ошибись") | consensus | `mixture_of_agents` | high |

Default when no signal matches: stay on `sonnet`, `reasoning_effort: medium`.

## Escalation / de-escalation (dynamic)

- **Start on the cheapest adequate lane.** Do not reach for `opus` until
  `sonnet` is actually stuck.
- **Escalate ↑** on explicit complexity, a failed attempt (tests red / bug
  persists), or user dissatisfaction ("не то, глубже"): `sonnet → opus` + `high`.
- **De-escalate ↓** for mechanical work (rename, format, bulk identical edits):
  `flash` or delegated subagents.
- **Auto-review trigger:** after writing NON-TRIVIAL code (>~30 lines, OR it
  touches auth/money/security, OR the user said "проверь"), automatically run
  `delegate_task(goal="Review this diff for bugs and security issues: ...")`
  BEFORE delivering. The child runs on `delegation.model` (= reviewer/Codex),
  so author ≠ reviewer as long as the main model isn't also Codex.
- **Parallelize:** research / "evaluate N options" → split into subagents via
  `delegate_task` (they run on `delegation.model`), never serialize on the main
  model. NOTE: all parallel children share the one `delegation.model`.

## Pipelines (whole flow on one request)

- **"Сделай маркетинг X"**: `/model strateg` (strategy) →
  parallel `research` subagents (competitors/keywords) →
  `/model copywriter` (texts) → `image_generate` (creative) →
  `text_to_speech` (voiceover).
- **"Реализуй фичу Y"**: `sonnet`/`opus` (code) →
  auto `reviewer` via delegate (review) → fix → report `/spend`.

## Cache discipline (non-negotiable)

- Switch the main model only BETWEEN tasks, never mid-context.
- Prefer `delegate_task` (child runs on `delegation.model`) over `/model` when a
  sub-step needs the delegation model for just one step — the child's context is
  isolated and the main prompt cache survives.
- Long session → `prompt_caching.cache_ttl: "1h"` makes repeated turns nearly free.
