# ibkr-gateway-rcon

Pause, resume, and manually restart Interactive Brokers gateway instances running behind a watchdog script. Ships with:

- **`gateway_ctl.py`** — pure-function backend. Knows how to read/write skip-files, check listening ports, read the watchdog log, issue restart commands.
- **`watchdog.sh`** — a drop-in replacement for a vanilla cron watchdog. Before restarting a gateway it checks for a `<name>.skip` file; if present and unexpired, it backs off silently.
- **`discord_bot.py`** — slash-command interface. `/gateway status`, `/gateway pause`, `/gateway resume`, `/gateway restart`.
- **`webapp.py`** *(planned)* — Flask dashboard. Same primitives, HTML UI.

## Why

If your phone gets push notifications every time a gateway tries to re-auth, a watchdog restart loop turns into a DoS on your own pocket. This tool adds per-gateway pause controls with optional auto-resume timers, so you can silence a specific gateway for 30 minutes without killing the watchdog entirely.

## Design

**Skip-file protocol.** Each gateway has a `state/<name>.skip` file. If the file exists, the watchdog skips that gateway on its next tick. The file's contents are either empty (paused indefinitely) or a single ISO-8601 timestamp (auto-resume at that time).

This keeps state out of the watchdog process — it can be a dumb bash script. Pause/resume operations just touch/delete files. The backend (`gateway_ctl.py`), the Discord bot, the web UI, and an ad-hoc CLI are all independent front ends hitting the same filesystem primitives.

**Config-driven.** No gateway specs, paths, or broker details are hardcoded. Copy `config.example.yaml` to `config.yaml`, fill in your gateways, run. The `config.yaml` is gitignored.

## Setup

```bash
git clone https://github.com/<you>/ibkr-gateway-rcon.git
cd ibkr-gateway-rcon
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml with your gateway names, ports, restart commands
pytest  # verify install
```

Then wire `watchdog.sh` into cron (example in `docs/cron.md`).

For the Discord bot, copy `.env.example` to `.env`, fill in your bot token + guild ID + channel ID, run `python discord_bot.py`.

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
├── discord_bot.py          # slash-command wrapper
├── webapp.py               # Flask layer (planned)
├── watchdog.sh             # cron-driven status checker & restarter
├── config.example.yaml
├── .env.example
├── requirements.txt
├── tests/
│   ├── test_skip_files.py
│   ├── test_status.py
│   ├── test_duration_parse.py
│   └── test_watchdog_sh.py
└── docs/
    ├── cron.md
    └── architecture.md
```

## License

MIT
