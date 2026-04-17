# Architecture

Three front ends, one shared backend, filesystem as state.

```
   Discord slash commands    Flask web page    Humans + cron
           │                       │                │
           └────┬──────────────────┴────────────────┘
                │
        gateway_ctl.py  +  gwctl.py  (thin CLI over gateway_ctl)
                │
   ┌────────────┼────────────┐
   │            │            │
skip-files   port probe   watchdog log
 (YAML-free)  (ss/netstat)  (append-only text)
```

## Why filesystem as state?

- **Zero daemons.** There's no long-running backend process to go down. Any of the front ends can appear or disappear without coordination.
- **Bash-friendly.** `watchdog.sh` is shell. A skip-file with a single ISO timestamp is trivially readable without a Python interpreter.
- **Durable by default.** State survives bot restarts, server reboots, laptop sleeps. No in-memory-only decisions.

## Why pure functions where possible?

`gateway_ctl.py` keeps config parsing, duration math, and status aggregation as pure functions. I/O (the subprocess calls for port probes and restart commands) lives at the edges. This makes the tests fast, deterministic, and free of mocking gymnastics — each test passes in a `port_listening=lambda port: True` fake instead of monkey-patching subprocess.

## The skip-file contract

A skip-file under `state/<name>.skip` means "don't let the watchdog restart this gateway." Format:

- Empty file → paused indefinitely, human must `resume` to un-pause.
- Single line with an ISO-8601 UTC timestamp → paused until that instant. `is_skipped()` auto-cleans the file once the deadline passes, so stale files don't accumulate.

Three rules make this safe:

1. `watchdog.sh` *only reads* skip-files; operators (via Discord, CLI, web UI) *only write* them. No shared writer races.
2. The skip is per-gateway, not global. Pausing `secondary` never affects `primary`.
3. Auto-resume is timestamp-based, not duration-based. Even if the server reboots mid-pause, resume fires exactly when the wall clock crosses the deadline.

## Why a small CLI in front of gateway_ctl?

`gwctl.py` is a minimal subprocess-friendly surface. `watchdog.sh` calls `gwctl status-one` instead of parsing YAML itself. Humans can run `gwctl status-all /path/to/config.yaml` from a terminal in an emergency without needing Discord or a browser.

## What the Discord bot actually does

The bot:

1. Loads `config.yaml` once at startup.
2. Registers four guild-scoped slash commands (`status`, `pause`, `resume`, `restart`, `tail`).
3. On each invocation, calls directly into `gateway_ctl`. No database, no cache, no queue.
4. Optionally gates the commands to a single channel ID so the controls don't leak into random text channels in the guild.

Restart commands run synchronously (via `asyncio.to_thread`) with a 60-second timeout so a hung gateway doesn't block the event loop.

## What we deliberately don't do

- **No cross-host orchestration.** This is a local watchdog, not SaltStack. One host, one cron line, one skip-directory.
- **No authentication beyond "be in the guild."** Discord handles identity; the channel gate handles location. Adding RBAC is out of scope — if you need it, the usual pattern is slash-command role checks via `app_commands.checks`.
- **No Lottie/fancy UI.** The Flask layer (planned) will be a plain HTML page with server-rendered buttons. No SPA.
