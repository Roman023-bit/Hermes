# Telegram operational alerting — implementation plan

1. Add strict alert configuration under `alerts:` in the existing
   `config.yaml`; never introduce a behavioral `.env` flag.
2. Add the stdlib Bot API transport, private outbox, per-recipient progress,
   retry delivery, transition state and heartbeat generator.
3. Extend Hermes status records with `last_ok_at`.
4. Add a host-side Hermes production healthcheck.
5. Add `OnFailure` to Hermes backup/health services and install delivery,
   monitor and heartbeat systemd timers.
6. Add Knowledge Factory's stdlib `run_with_status.py`; wrap its three server
   units and two Mac LaunchAgents; make exit 75 successful for backup.
7. Install the three Mac alert LaunchAgents. Build all five Hermes operations
   jobs into `~/.local/share/hermes/operations-runtime` first; no LaunchAgent
   may execute the checkout under `~/Documents`, because a successful
   `launchctl bootstrap` does not prove macOS will permit the later file open.
8. Seed every new status through a real successful run before enabling the
   silence monitor.
9. Run live drills for enqueue/delivery, systemd `OnFailure`, failed→recovered
   transition and both heartbeats. Never inject failure into a production
   data task.
10. Verify permissions, no token in argv/journal/outbox, timer schedules,
    outbox empty after delivery and all original backup/KF tests.
11. Update operational documentation, commit both repositories and deploy
    the exact committed revisions.

Live execution added two acceptance checks that the original plan omitted:
run both scheduled Hermes backup jobs through `launchctl kickstart`, and
require the KF sync wrapper to publish a real `OK` after any raw-tree
exclusion change.
