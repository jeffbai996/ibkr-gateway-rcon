# ibkr-gateway-rcon

Pause, resume, and manually restart Interactive Brokers gateway instances through Discord slash commands. The bot also runs the watchdog itself — no cron required — and is kept alive by a tiny deadman's switch that restarts the process if its heartbeat goes stale.

- **`gateway_ctl.py`** — pure-function backend. Config, skip-files, duration parsing, status aggregation, port probes, the watchdog tick, heartbeat I/O.
- **`discord_bot.py`** — the long-running process. Registers slash commands AND runs `watchdog_tick` every `WATCHDOG_INTERVAL_SEC` seconds (default 180). After each tick it writes a heartbeat file.
- **`deadman.py`** — one-shot utility. Reads the heartbeat file; if it's stale, issues `systemctl --user restart` against the bot service. Runs on a 5-minute systemd timer.
- **`gwctl.py`** — optional CLI for humans / legacy cron setups. `list-names`, `status-one`, `status-all`, `pause`, `resume`, `restart-one`, `tail-log`.
- **`watchdog.sh`** — optional shell-side watchdog for setups that don't want a long-running bot. Redundant with `discord_bot.py`'s in-process loop.
- **`webapp.py`** *(planned)* — Flask dashboard. Same primitives, HTML UI.

## Why

If your phone gets push notifications every time a gateway tries to re-auth, a watchdog restart loop turns into a DoS on your own pocket. This tool adds per-gateway pause controls with optional auto-resume timers, so you can silence a specific gateway for 30 minutes without killing the watchdog entirely.

## Design

**Skip-file protocol.** Each gateway has a `state/<name>.skip` file. If the file exists, the watchdog skips that gateway on its next tick. The file's contents are either empty (paused indefinitely) or a single ISO-8601 timestamp (auto-resume at that time).

This keeps state out of the watchdog process — it can be a dumb bash script. Pause/resume operations just touch/delete files. The backend (`gateway_ctl.py`), the Discord bot, the web UI, and an ad-hoc CLI are all independent front ends hitting the same filesystem primitives.

**Config-driven.** No gateway specs, paths, or broker details are hardcoded. Copy `config.example.yaml` to `config.yaml`, fill in your gateways, run. The `config.yaml` is gitignored.

## Setup

See **[docs/install.md](docs/install.md)** for the full walkthrough. Abbreviated:

```bash
git clone https://github.com/<you>/ibkr-gateway-rcon.git
cd ibkr-gateway-rcon
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml  # edit with your gateways
cp .env.example .env                # edit with your Discord token + IDs
pytest -q                           # verify

# install systemd user units
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ibkr-gateway-rcon-bot.service
systemctl --user enable --now ibkr-gateway-rcon-deadman.timer
```

## Commands

```
/gateway status            → which gateways are up, which are paused (and until when)
/gateway pause <name> [duration]
/gateway resume <name>
/gateway restart <name>
/gateway tail [n=20]       → last N lines of the watchdog log
```

Duration accepts `30m`, `2h`, `1d`, or ISO timestamp. Omit for indefinite pause.

## Layout

```
ibkr-gateway-rcon/
├── gateway_ctl.py          # backend (pure functions + small fs I/O)
├── discord_bot.py          # slash commands + in-process watchdog loop
├── deadman.py              # heartbeat checker — restarts the bot if stale
├── gwctl.py                # CLI wrapper over gateway_ctl (humans + legacy cron)
├── webapp.py               # Flask layer (planned)
├── watchdog.sh             # optional shell watchdog (redundant if bot is running)
├── config.example.yaml
├── .env.example
├── requirements.txt
├── systemd/
│   ├── ibkr-gateway-rcon-bot.service
│   ├── ibkr-gateway-rcon-deadman.service
│   └── ibkr-gateway-rcon-deadman.timer
├── tests/
│   ├── test_config.py
│   ├── test_duration_parse.py
│   ├── test_skip_files.py
│   ├── test_status.py
│   ├── test_watchdog_tick.py
│   └── test_heartbeat.py
└── docs/
    ├── install.md
    ├── cron.md
    └── architecture.md
```

## License

MIT
