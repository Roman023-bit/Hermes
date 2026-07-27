# Telegram operational alerting — design

Status: implemented for Hermes and Knowledge Factory on Aeza and macOS.

## Goal

Report failure, silence and recovery of the backup/sync/health paths without
depending on the Hermes agent loop, gateway, container, or free-form log
parsing.

The delivery provider is Telegram. A dedicated
`TELEGRAM_ALERT_BOT_TOKEN` is preferred. Until one is created through
BotFather, production may explicitly allow fallback to the existing
`TELEGRAM_BOT_TOKEN`; this leaves token revocation as a documented shared
failure mode.

## Monitored components

Aeza:

- Hermes essential backup — maximum silence 30 hours;
- Hermes full archive — 30 hours;
- Hermes production container/gateway healthcheck — 30 minutes;
- Knowledge Factory backup — 30 hours;
- Knowledge Factory healthcheck — 30 minutes;
- Knowledge Factory restore drill — 35 days.

Mac:

- Hermes off-site pull — 30 hours;
- Hermes freshness validation — 30 hours;
- Hermes restore drill — 8 days;
- Knowledge Factory off-site pull — 30 hours;
- Knowledge Factory sync to Aeza — 2 hours (allows for sleep/wake).

`SKIPPED` is not an immediate incident. Status records preserve
`last_ok_at`, so repeated lock skips eventually become silence instead of
remaining fresh forever.

## Event flow

```text
task / OnFailure / silence monitor
             │
             ▼
 private atomic JSON outbox
             │
       retry every 2 min
             │
             ▼
      Telegram Bot API
```

Producers never perform network delivery. An event remains in the outbox
until Telegram returns both HTTP 200 and JSON `ok: true`. Per-recipient
progress is persisted after every send. Delivery is at-least-once: a process
crash after Telegram accepts a request can produce a duplicate, but cannot
silently discard the alert.

The token stays in process memory. It is read from dotenv syntax without
executing the file and is never placed in argv, a journal field, an event, or
an exception string.

## State transitions

- healthy → failed/stale: one `FAILED`;
- still failed: one `REMINDER` per configured interval (six hours in
  production);
- failed → healthy: one `RECOVERED`;
- weekly: one heartbeat from Aeza and one from the Mac.

The ten-minute healthcheck therefore cannot flood Telegram. Event IDs are
deterministic for a transition/invocation, and the outbox deduplicates them.

## Structured status

Every monitored job publishes owner-only JSON containing:

```json
{
  "name": "component",
  "outcome": "OK|FAILED|SKIPPED",
  "reason": "bounded one-line reason",
  "finished_at": "UTC",
  "last_ok_at": "UTC or empty"
}
```

Hermes extends its existing status contract. Knowledge Factory uses a
stdlib-only `run_with_status.py` wrapper around the unchanged commands.
Neither alerting nor summaries parse journals. Journals remain diagnostic
detail for an operator after an alert.

## systemd and launchd

Six Aeza services use `OnFailure=hermes-alert@%n.service`. The handler reads
only allowlisted `systemctl show` fields (`Result`, `ExecMainStatus`,
`InvocationID`) and queues an event. Knowledge Factory backup declares
`SuccessExitStatus=75`, matching its documented lock skip.

The Mac runs independent delivery and monitor LaunchAgents plus a weekly
heartbeat. Existing Hermes and Knowledge Factory LaunchAgents keep their
schedules; the KF jobs gain status wrappers.

All five Hermes operations LaunchAgents execute an installed, private runtime
under `~/.local/share/hermes/operations-runtime`. They do not execute code
from the checkout in `~/Documents`: macOS can load such a plist successfully
and then deny the first real script open with exit 126. The installer builds
and validates the replacement before unloading any working agent, then
restores all five labels.

## Live acceptance

The deployed Aeza monitor reports all six components healthy; the deployed
Mac monitor reports all five healthy. Silent `TEST` messages were accepted by
Telegram from both hosts and removed from their outboxes. A real systemd
`OnFailure` invocation queued one bounded event without reading a journal.
Both Hermes Mac backup agents were subsequently executed through launchd,
not just loaded, and exited successfully from the installed runtime.

Knowledge Factory sync exposed one additional live-tree edge:
`runtime/logs` and `runtime/legacy-logs` mutate while rsync runs. They are now
excluded symmetrically from the raw manifest and transfer. The failed run
published `FAILED`; the repeated full sync published `OK`. During final
integration tests OrbStack later became genuinely unavailable: the deployed
monitor delivered one `FAILED`, then one `RECOVERED` after a successful real
sync. The production transition and deduplication path was therefore exercised
without editing a status file.
