"""Process-wide ledger for money-spending operations.

Hermes spends money in two places: paid tool APIs (Perplexity, Exa, Firecrawl,
Parallel, Tavily, image generation, premium TTS) and LLM calls. This module is
the single place those costs are recorded so the agent can show, per turn:

  * which tools ran and which LLM models were involved, and
  * how much the turn cost, the running session cost, and a persistent
    lifetime total ("how much I've spent on Hermes").

Scope model
-----------
* **turn**     reset at the start of each ``run_conversation()`` turn; drives
               the end-of-turn footer.
* **session**  accumulated across turns for the life of the process.
* **lifetime** persisted to ``~/.hermes/spend.json`` across runs.

Thread-safety
-------------
Tool calls within a turn may run in parallel, so all mutation is guarded by a
single re-entrant lock. One agent processes one turn at a time, so a single
process-global "current turn" is sufficient. Costs from delegated subagents in
the same process roll up into the current turn / lifetime — which is exactly
what we want for a true total spend.

Every public function is best-effort and never raises into the agent loop.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Cost statuses, mirroring agent.usage_pricing where possible.
#   estimated — computed from an approximate price table (most paid tools)
#   actual    — reported by the provider
#   included  — covered by a subscription, $0 marginal
#   unknown   — money was spent but we can't price it
STATUS_ESTIMATED = "estimated"
STATUS_ACTUAL = "actual"
STATUS_INCLUDED = "included"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class CostEntry:
    """A single money-spending event within a turn."""

    kind: str                       # "tool" | "llm"
    name: str                       # tool name (e.g. "web_search") or "llm"
    backend: Optional[str] = None   # e.g. "perplexity", "firecrawl", "fal"
    models: Tuple[str, ...] = ()     # LLM models involved (main or auxiliary)
    amount_usd: Optional[float] = None
    status: str = STATUS_ESTIMATED
    units: Optional[str] = None     # human label, e.g. "1 search", "3 images"
    note: Optional[str] = None

    def source_key(self) -> str:
        """Key used for the lifetime per-source breakdown.

        LLM spend is grouped by model; tool spend by backend (falling back to
        the tool name). This produces the mixed "deepseek / perplexity /
        firecrawl / image-gen" top-list the user asked for.
        """
        if self.kind == "llm" and self.models:
            return self.models[0]
        return self.backend or self.name


@dataclass
class TurnSummary:
    """Aggregated view of one turn, returned by :func:`end_turn`."""

    entries: List[CostEntry] = field(default_factory=list)
    turn_total_usd: float = 0.0
    has_unknown: bool = False
    has_estimated: bool = False
    tools: List[Tuple[str, Optional[str]]] = field(default_factory=list)  # (name, backend)
    models: List[str] = field(default_factory=list)
    session_total_usd: float = 0.0
    lifetime_total_usd: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.entries


# ── Module state ────────────────────────────────────────────────────────────

_lock = threading.RLock()
_turn_entries: List[CostEntry] = []
_session_total_usd: float = 0.0
_enabled_cache: Optional[bool] = None


def _coerce_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


def is_enabled() -> bool:
    """Return whether cost tracking is on (``cost_tracking.enabled``, default True).

    Cached after first read; config is not expected to change mid-process.
    """
    global _enabled_cache
    if _enabled_cache is not None:
        return _enabled_cache
    enabled = True
    try:
        from hermes_cli.config import load_config
        section = load_config().get("cost_tracking", {})
        if isinstance(section, dict) and "enabled" in section:
            enabled = bool(section.get("enabled"))
    except Exception:
        enabled = True
    _enabled_cache = enabled
    return enabled


def reset_enabled_cache() -> None:
    """Drop the cached enabled flag (used by tests / after config changes)."""
    global _enabled_cache
    _enabled_cache = None


# ── Recording ───────────────────────────────────────────────────────────────

def record(
    kind: str,
    name: str,
    *,
    backend: Optional[str] = None,
    models: Sequence[str] = (),
    amount_usd: Any = None,
    status: str = STATUS_ESTIMATED,
    units: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    """Record one money-spending event. Best-effort; never raises."""
    if not is_enabled():
        return
    try:
        entry = CostEntry(
            kind=kind,
            name=name,
            backend=backend,
            models=tuple(m for m in models if m),
            amount_usd=_coerce_amount(amount_usd),
            status=status,
            units=units,
            note=note,
        )
        with _lock:
            _turn_entries.append(entry)
        # Flush to the lifetime store immediately, per record. This keeps the
        # lifetime total accurate on every code path — including gateway/cron/
        # subagent turns that never call begin_turn()/end_turn() — without
        # wiring each caller. The turn footer (begin/end) only affects display.
        if entry.amount_usd:
            _flush_one_to_lifetime(entry.amount_usd, entry.source_key())
    except Exception:  # pragma: no cover - defensive
        logger.debug("cost_ledger.record failed", exc_info=True)


def record_tool(
    name: str,
    *,
    backend: Optional[str] = None,
    models: Sequence[str] = (),
    amount_usd: Any = None,
    status: str = STATUS_ESTIMATED,
    units: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    """Record a paid-tool invocation (search, extract, image gen, TTS, …)."""
    record(
        "tool", name, backend=backend, models=models, amount_usd=amount_usd,
        status=status, units=units, note=note,
    )


def record_llm(
    model: str,
    *,
    amount_usd: Any = None,
    status: str = STATUS_ESTIMATED,
    role: str = "main",
    provider: Optional[str] = None,
) -> None:
    """Record an LLM call so its model and cost show up in the turn footer."""
    record(
        "llm", "llm", backend=provider, models=(model,) if model else (),
        amount_usd=amount_usd, status=status, units=role,
    )


def record_llm_for_turn(model, provider, cost_result, *, role="main") -> None:
    """Record an LLM call from a usage_pricing ``CostResult`` (best-effort glue).

    Duck-types ``cost_result`` (``.amount_usd`` may be Decimal/float/None,
    ``.status`` a string) and forwards to :func:`record_llm`. Never raises —
    failures log at debug. Kept here so it is unit-testable without a live
    conversation turn.
    """
    try:
        amount = getattr(cost_result, "amount_usd", None)
        record_llm(
            model,
            amount_usd=float(amount) if amount is not None else None,
            status=getattr(cost_result, "status", STATUS_ESTIMATED),
            role=role,
            provider=provider,
        )
    except Exception:
        logger.debug("record_llm_for_turn failed", exc_info=True)


# ── Turn lifecycle ──────────────────────────────────────────────────────────

def begin_turn() -> None:
    """Clear the per-turn accumulator. Call at the start of each turn."""
    with _lock:
        _turn_entries.clear()


def end_turn() -> TurnSummary:
    """Finalize the current turn: roll its cost into the session + lifetime
    stores and return an aggregated :class:`TurnSummary` for rendering.
    """
    global _session_total_usd
    with _lock:
        entries = list(_turn_entries)
        _turn_entries.clear()

    summary = TurnSummary(entries=entries)
    seen_tools: set[Tuple[str, Optional[str]]] = set()
    seen_models: set[str] = set()

    for e in entries:
        if e.amount_usd is not None:
            summary.turn_total_usd += e.amount_usd
        if e.status == STATUS_UNKNOWN:
            summary.has_unknown = True
        elif e.status == STATUS_ESTIMATED:
            summary.has_estimated = True

        if e.kind == "tool":
            key = (e.name, e.backend)
            if key not in seen_tools:
                seen_tools.add(key)
                summary.tools.append(key)
        for m in e.models:
            if m not in seen_models:
                seen_models.add(m)
                summary.models.append(m)

    with _lock:
        _session_total_usd += summary.turn_total_usd
        summary.session_total_usd = _session_total_usd

    # Lifetime was already accrued per-record in record(); just read the total.
    summary.lifetime_total_usd = lifetime_total_usd()
    return summary


def session_total_usd() -> float:
    with _lock:
        return _session_total_usd


# ── Lifetime store (~/.hermes/spend.json) ───────────────────────────────────

_STORE_VERSION = 1
_store_lock = threading.RLock()


def _store_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "spend.json"


def load_store() -> Dict[str, Any]:
    """Load the lifetime spend store, returning a fresh skeleton on any error."""
    skeleton: Dict[str, Any] = {
        "version": _STORE_VERSION,
        "lifetime_usd": 0.0,
        "by_day": {},
        "by_source": {},
        "updated_at": None,
    }
    try:
        path = _store_path()
        if not path.exists():
            return skeleton
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return skeleton
        # Merge onto skeleton so missing keys are always present.
        for key in skeleton:
            if key in data:
                skeleton[key] = data[key]
        return skeleton
    except Exception:
        logger.debug("cost_ledger.load_store failed", exc_info=True)
        return skeleton


def _save_store(store: Dict[str, Any]) -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".spend_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(store, fh, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        logger.debug("cost_ledger._save_store failed", exc_info=True)


def _flush_one_to_lifetime(amount: float, source: str) -> None:
    """Add a single recorded amount to the persistent store (atomic, locked)."""
    if not amount or amount <= 0:
        return
    with _store_lock:
        store = load_store()
        store["lifetime_usd"] = float(store.get("lifetime_usd", 0.0) or 0.0) + amount
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        by_day = store.setdefault("by_day", {})
        by_day[day] = float(by_day.get(day, 0.0) or 0.0) + amount
        store_sources = store.setdefault("by_source", {})
        store_sources[source] = float(store_sources.get(source, 0.0) or 0.0) + amount
        store["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_store(store)


def lifetime_total_usd() -> float:
    return float(load_store().get("lifetime_usd", 0.0) or 0.0)


def today_total_usd() -> float:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return float((load_store().get("by_day") or {}).get(day, 0.0) or 0.0)


def top_sources(limit: int = 6) -> List[Tuple[str, float]]:
    sources = load_store().get("by_source") or {}
    return sorted(
        ((k, float(v or 0.0)) for k, v in sources.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )[:limit]


# ── Rendering ───────────────────────────────────────────────────────────────

def _fmt_usd(amount: float, *, estimated: bool = False) -> str:
    prefix = "~" if estimated else ""
    if amount and abs(amount) < 0.01:
        return f"{prefix}${amount:.4f}"
    return f"{prefix}${amount:.2f}"


def render_footer(summary: TurnSummary, *, show_lifetime: bool = True) -> str:
    """Render the compact end-of-turn footer. Returns "" when there's nothing
    worth showing (no paid tools and no priced LLM call).
    """
    if summary.is_empty:
        return ""

    lines: List[str] = ["─── итог хода ───"]

    if summary.tools:
        parts = []
        for name, backend in summary.tools:
            parts.append(f"{name}({backend})" if backend else name)
        lines.append("инструменты: " + ", ".join(parts))

    if summary.models:
        lines.append("модели: " + ", ".join(summary.models))

    est = summary.has_estimated or summary.has_unknown
    turn_s = _fmt_usd(summary.turn_total_usd, estimated=est)
    sess_s = _fmt_usd(summary.session_total_usd, estimated=est)
    cost_line = f"ход: {turn_s} · сессия: {sess_s}"
    if show_lifetime:
        cost_line += f" · всего: {_fmt_usd(summary.lifetime_total_usd)}"
    lines.append(cost_line)
    if summary.has_unknown:
        lines.append("(часть расходов не удалось оценить)")
    return "\n".join(lines)


def render_spend_report(*, markdown: bool = False) -> str:
    """Render the ``/spend`` report: lifetime total, today, and top sources.

    Reads the persistent store, so it is accurate in every front-end (CLI,
    gateway, ``hermes spend``) regardless of which process recorded the spend.
    """
    store = load_store()
    lifetime = float(store.get("lifetime_usd", 0.0) or 0.0)
    today = today_total_usd()
    session = session_total_usd()
    sources = top_sources(8)

    if lifetime <= 0 and not sources:
        return "Пока нет учтённых расходов. Они появятся после первого платного запроса."

    bullet = "- " if markdown else "  "
    lines: List[str] = []
    title = "## Расходы на Hermes" if markdown else "💰 Расходы на Hermes"
    lines.append(title)
    lines.append(f"{bullet}Всего за всё время: {_fmt_usd(lifetime)}")
    lines.append(f"{bullet}За сегодня:         {_fmt_usd(today)}")
    if session > 0:
        lines.append(f"{bullet}Эта сессия:         {_fmt_usd(session, estimated=True)}")
    if sources:
        top_str = " · ".join(f"{name} {_fmt_usd(amount)}" for name, amount in sources)
        lines.append(f"{bullet}Топ источников: {top_str}")
    lines.append("(оценочно; стоимость инструментов считается по приблизительному прайсу)")
    return "\n".join(lines)


# Test/maintenance helper: zero out in-process session state.
def _reset_for_tests() -> None:
    global _session_total_usd
    with _lock:
        _turn_entries.clear()
        _session_total_usd = 0.0
    reset_enabled_cache()
