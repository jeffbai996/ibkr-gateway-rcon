# Architecture

Three front ends, one shared backend, filesystem as state.

```
   Discord slash commands    Flask web page    Humans + CLI     systemd timer
   (inside the bot)              │                 │                │
     + watchdog loop              │                 │                │
           │                      │                 │                │
           └────┬─────────────────┴─────────────────┘                │
                │                                                    │
        gateway_ctl.py  +  gwctl.py  +  discord_bot.py  ←─── deadman.py
                │
   ┌────────────┼────────────┬──────────────┐
   │            │            │              │
skip-files   port probe   watchdog log   bot.heartbeat
 (YAML-free)  (ss/netstat)  (append-only) (ISO timestamp)
```

## The bot is the watchdog

The Discord bot (`discord_bot.py`) runs its own `tasks.loop` every `WATCHDOG_INTERVAL_SEC` seconds (default 180). Each tick does the same thing the old cron watchdog did: probe every gateway port, check skip-files, fire restart commands for any gateway that's down and not paused. After a successful tick the bot writes a timestamp to `state/bot.heartbeat`.

Benefits of owning the loop in-process:

- **One process, one PID.** If the bot is up, the watchdog is up. No "cron keeps firing but the bot is dead" weirdness.
- **Rich integration.** The bot can send a proactive message when a gateway has restarted N times in M minutes. That's harder from a standalone bash script.
- **Easier testing.** `watchdog_tick` is a pure function — feed it a fake port probe and assert on the returned action list.

## The deadman makes it safe

The single-process design has a clear failure mode: if the bot dies, the watchdog dies with it. `deadman.py` closes that gap. It runs on a 5-minute systemd timer, independent of the bot. On each tick it reads `bot.heartbeat`; if the timestamp is older than 10 minutes (or the file is missing), it runs `systemctl --user restart ibkr-gateway-rcon-bot.service`.

This is a small, dependency-light script that uses only `gateway_ctl`'s pure heartbeat helpers. If the bot's Python environment is wedged (OOM, import failure, event-loop deadlock), the deadman is unaffected — its entire execution is "read one text file, maybe run one shell command, exit."

The bot service is configured with `Restart=always` in its systemd unit, so most bot crashes self-heal without needing the deadman at all. The deadman is for the harder class of failure: "bot process is alive but not doing work" (deadlock, exception-swallowed loop, Discord API stuck).

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
