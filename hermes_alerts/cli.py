"""Command-line surface used by systemd and launchd."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AlertConfigError, load_settings
from .monitor import heartbeat, monitor, record_failure
from .secrets import SecretError, read_token
from .storage import (
    AlertStateError,
    enqueue,
    private_dir,
    read_event,
    state_lock,
    write_event,
)
from .telegram import DeliveryError, render_message, send_message

_SAFE_UNIT = re.compile(r"\A[a-zA-Z0-9_.@:-]+\\.service\Z")


def _load(args):
    return load_settings(args.config, args.profile)


def _deliver(args) -> int:
    settings = _load(args)
    token = read_token(
        args.env_file,
        allow_primary_fallback=settings.allow_primary_token_fallback,
    )
    outbox = private_dir(settings.state_root / "outbox")
    bad = private_dir(settings.state_root / "bad")
    delivered = 0
    with state_lock(settings.state_root):
        for path in sorted(outbox.glob("*.json"))[: args.limit]:
            try:
                event = read_event(path)
            except AlertStateError:
                shutil.move(str(path), bad / path.name)
                continue
            pending = [str(item) for item in event["pending_chat_ids"]]
            for chat_id in pending:
                try:
                    send_message(token, chat_id, render_message(event))
                except DeliveryError as error:
                    event["attempts"] = int(event.get("attempts", 0)) + 1
                    event["last_error"] = str(error)
                    write_event(path, event)
                    print(f"alert_delivery_FAILED {error}", file=sys.stderr)
                    return 1
                event["pending_chat_ids"].remove(chat_id)
                event.pop("last_error", None)
                write_event(path, event)
            path.unlink()
            delivered += 1
    print(f"alert_delivery_OK events={delivered}")
    return 0


def _monitor(args) -> int:
    summary = monitor(_load(args))
    failed = [name for name, value in summary.items() if value != "OK"]
    print(f"alert_monitor_OK components={len(summary)} active_failures={len(failed)}")
    return 0


def _heartbeat(args) -> int:
    summary = heartbeat(_load(args))
    failed = [name for name, value in summary.items() if value != "OK"]
    print(f"alert_heartbeat_OK components={len(summary)} degraded={len(failed)}")
    return 0


def _enqueue(args) -> int:
    settings = _load(args)
    target = enqueue(
        settings,
        args.component,
        args.kind,
        args.reason,
        dedupe_key=args.dedupe_key,
    )
    print(f"alert_enqueue_OK event={target.stem}")
    return 0


def _systemctl_value(unit: str, field: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", unit, f"--property={field}", "--value"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return "unknown"
    return " ".join(result.stdout.split())[:160] or "unknown"


def _record_systemd(args) -> int:
    if not _SAFE_UNIT.match(args.unit):
        raise AlertStateError(f"unsafe unit name: {args.unit!r}")
    settings = _load(args)
    result = _systemctl_value(args.unit, "Result")
    status = _systemctl_value(args.unit, "ExecMainStatus")
    invocation = _systemctl_value(args.unit, "InvocationID")
    component = settings.component_for_unit(args.unit) or f"systemd:{args.unit}"
    record_failure(
        settings,
        component,
        f"unit={args.unit} result={result} exit={status}",
        dedupe_key=f"{settings.profile}:systemd:{args.unit}:{invocation}",
    )
    print(f"alert_systemd_failure_QUEUED unit={args.unit}")
    return 0


def _summary(args) -> int:
    settings = _load(args)
    summary = monitor(settings)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    deliver = sub.add_parser("deliver")
    deliver.add_argument("--env-file", type=Path, required=True)
    deliver.add_argument("--limit", type=int, default=50)
    deliver.set_defaults(handler=_deliver)

    monitor_parser = sub.add_parser("monitor")
    monitor_parser.set_defaults(handler=_monitor)

    heartbeat_parser = sub.add_parser("heartbeat")
    heartbeat_parser.set_defaults(handler=_heartbeat)

    enqueue_parser = sub.add_parser("enqueue")
    enqueue_parser.add_argument("--component", required=True)
    enqueue_parser.add_argument(
        "--kind",
        required=True,
        choices=["FAILED", "REMINDER", "RECOVERED", "HEARTBEAT", "TEST"],
    )
    enqueue_parser.add_argument("--reason", required=True)
    enqueue_parser.add_argument("--dedupe-key", required=True)
    enqueue_parser.set_defaults(handler=_enqueue)

    systemd_parser = sub.add_parser("record-systemd-failure")
    systemd_parser.add_argument("--unit", required=True)
    systemd_parser.set_defaults(handler=_record_systemd)

    summary_parser = sub.add_parser("summary")
    summary_parser.set_defaults(handler=_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (AlertConfigError, AlertStateError, SecretError, OSError) as error:
        print(f"alert_{args.command}_FAILED {error}", file=sys.stderr)
        return 1
