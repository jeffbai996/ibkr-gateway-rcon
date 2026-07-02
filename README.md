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

**Control**

```
/gateway status                       which gateways are up, which are paused
/gateway pause [name] [duration]      defaults to all — duration is 30m/2h/1d/ISO or omit
/gateway resume [name]                defaults to all
/gateway restart [name]               defaults to all — async, non-blocking
/gateway stop [name]                  defaults to all — only auto-pauses on success
/gateway tail [n=20]                  last N lines of the watchdog log
/gateway health                       process uptime, restart count, last heartbeat
```

**Read-only views (require MCP reachability)**

```
/gateway brief [scope]                portfolio summary: NLV, P&L, top positions, today's trades; CAD-only with FX
/gateway pnl                          per-account daily / unrealized / realized
/gateway positions                    top positions w/ cost basis, market value, unrealized P&L
/gateway trades                       today's executions by account
/gateway margin                       cushion, excess liquidity, buying power, leverage, utilization
/gateway quote <symbols>              live quotes — space-separated tickers (e.g. mu avgo nvda)
```

**Risk & intelligence (require MCP reachability)**

```
/gateway drawdown                     NAV vs peak: drawdown %, recovery needed
/gateway var                          1-day Value-at-Risk (95%/99%) + component breakdown
/gateway correlation                  pairwise correlation matrix — hidden concentration
/gateway sector                       sector exposure: weights + HHI concentration
/gateway beta [benchmark]             portfolio beta vs benchmark (default SPY)
/gateway geopolitical                 geopolitical risk mapped to held positions
/gateway thesis <news>                check a news item against your thesis pillars
/gateway rebalance <targets>          rebalance plan — targets as SYM:PCT,SYM:PCT
/gateway compare <symbols>            relative performance across symbols
```

Duration in `/gateway pause` accepts `30m`, `2h`, `1d`, or an ISO-8601 timestamp. Omit for indefinite. Most control commands accept `name` *or* an `all` sentinel — omitting `name` defaults to all gateways.

Output is mobile-friendly: code-block formatted with consistent column widths, `+1` decimal precision on NLV / day / liq / bp.

## Multi-channel gating

The bot can be restricted to specific Discord channels via an allowlist (`ALLOWED_CHANNEL_IDS` in `.env`). Slash commands invoked anywhere else return a polite refusal without leaking what they would have done. Guild ID is auto-discovered on startup; you can pin a specific guild in `.env` if you have multiple.

After guild sync the bot clears any duplicate global registrations so a single channel only sees one set of commands.

## Restart semantics

`/gateway restart` returns immediately and runs the restart asynchronously (`smart_restart_async`) so Discord doesn't time out on cold IBKR boots (which can run 60-240s). Progress is logged; `/gateway tail` shows the live stream.

`/gateway stop` only auto-pauses the gateway when the stop actually succeeded — a failed stop doesn't silently flip the gateway to paused state.

`/gateway status` reports MCP reachability honestly: if an MCP subscription is stale, the status row shows it stale rather than reporting last-known-good as current.

## Quotes / data hygiene

`/gateway quote` treats non-numeric price/change values as missing data rather than rendering them as `0` or `NaN`. Stale data in any view is labeled explicitly.

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
├── tests/                  # unit + integration coverage on config, durations, skip files, status, watchdog tick, heartbeat
└── docs/
    ├── install.md
    ├── cron.md
    └── architecture.md
```

## License

MIT
