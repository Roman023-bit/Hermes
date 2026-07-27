"""Transitions, reminders, recovery and weekly heartbeat generation."""

from __future__ import annotations

from datetime import datetime

from .config import AlertSettings
from .storage import (
    enqueue,
    evaluate,
    format_time,
    load_monitor_state,
    state_lock,
    utc_now,
    write_monitor_state,
)


def monitor(settings: AlertSettings, *, now: datetime | None = None) -> dict[str, str]:
    now = now or utc_now()
    summary: dict[str, str] = {}
    with state_lock(settings.state_root):
        state = load_monitor_state(settings.state_root)
        component_state = state["components"]
        for component in settings.components:
            healthy, reason = evaluate(component, now=now)
            summary[component.name] = "OK" if healthy else reason
            previous = component_state.get(component.name, {})
            active = previous.get("active") is True
            if not healthy:
                last_alert = previous.get("last_alert_at")
                reminder_due = not last_alert
                if last_alert:
                    try:
                        last = datetime.strptime(
                            last_alert, "%Y-%m-%dT%H:%M:%SZ"
                        ).replace(tzinfo=now.tzinfo)
                        reminder_due = (
                            now - last
                        ).total_seconds() >= settings.reminder_seconds
                    except (TypeError, ValueError):
                        reminder_due = True
                if not active or reminder_due:
                    kind = "REMINDER" if active else "FAILED"
                    observed = format_time(now)
                    enqueue(
                        settings,
                        component.name,
                        kind,
                        reason,
                        dedupe_key=(
                            f"{settings.profile}:{component.name}:{kind}:{observed}"
                        ),
                    )
                    previous["last_alert_at"] = observed
                previous["active"] = True
                previous["reason"] = reason
            else:
                if active:
                    observed = format_time(now)
                    enqueue(
                        settings,
                        component.name,
                        "RECOVERED",
                        reason,
                        dedupe_key=(
                            f"{settings.profile}:{component.name}:RECOVERED:{observed}"
                        ),
                    )
                previous = {
                    "active": False,
                    "reason": reason,
                    "last_ok_at": format_time(now),
                }
            component_state[component.name] = previous
        state["updated_at"] = format_time(now)
        write_monitor_state(settings.state_root, state)
    return summary


def record_failure(
    settings: AlertSettings,
    component_name: str,
    reason: str,
    *,
    dedupe_key: str,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    with state_lock(settings.state_root):
        state = load_monitor_state(settings.state_root)
        current = state["components"].get(component_name, {})
        if current.get("active") is not True:
            enqueue(
                settings,
                component_name,
                "FAILED",
                reason,
                dedupe_key=dedupe_key,
            )
        current.update({
            "active": True,
            "reason": reason,
            "last_alert_at": format_time(now),
        })
        state["components"][component_name] = current
        state["updated_at"] = format_time(now)
        write_monitor_state(settings.state_root, state)


def heartbeat(
    settings: AlertSettings, *, now: datetime | None = None
) -> dict[str, str]:
    now = now or utc_now()
    results = {
        component.name: evaluate(component, now=now)
        for component in settings.components
    }
    failed = [name for name, (healthy, _) in results.items() if not healthy]
    if failed:
        reason = f"DEGRADED {len(failed)}/{len(results)}: {', '.join(failed)}"
    else:
        reason = f"all {len(results)} monitored components healthy"
    year, week, _ = now.isocalendar()
    enqueue(
        settings,
        "alerting",
        "HEARTBEAT",
        reason,
        dedupe_key=f"{settings.profile}:heartbeat:{year}-W{week:02d}",
    )
    return {name: ("OK" if value[0] else value[1]) for name, value in results.items()}
