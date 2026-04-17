# Install

This walks through setting up the Discord bot (with its in-process watchdog) and the deadman's switch as systemd user services.

## 1. Configure

```bash
cd ~/repos/ibkr-gateway-rcon
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml` — real gateway ports, paths, and restart commands.

Edit `.env` — Discord bot token, guild ID, and optionally a channel ID to restrict commands.

Run the tests to confirm everything is wired up:

```bash
pytest -q
```

## 2. Install the bot as a systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ibkr-gateway-rcon-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ibkr-gateway-rcon-bot.service
```

Check the bot came up cleanly:

```bash
systemctl --user status ibkr-gateway-rcon-bot.service
tail -n 50 bot.log
```

In Discord, run `/gateway status` in your chosen channel — you should get a table of gateway states. If so, the bot is registered and the watchdog loop is ticking.

## 3. Install the deadman's switch

```bash
cp systemd/ibkr-gateway-rcon-deadman.service ~/.config/systemd/user/
cp systemd/ibkr-gateway-rcon-deadman.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ibkr-gateway-rcon-deadman.timer
```

The timer runs the deadman every 5 minutes. It reads `state/bot.heartbeat` — if the file is missing or older than 10 minutes, it issues `systemctl --user restart ibkr-gateway-rcon-bot.service`.

Verify it works:

```bash
# force a stale heartbeat by deleting it
rm state/bot.heartbeat
# trigger the deadman manually (don't wait for the timer)
systemctl --user start ibkr-gateway-rcon-deadman.service
# check logs
journalctl --user -u ibkr-gateway-rcon-deadman.service -n 20
```

You should see a STALE report followed by a bot restart. The bot should come back up and start refreshing its heartbeat again.

## 4. Stop the old cron watchdog

If you had a prior watchdog cron line (e.g., `*/3 * * * * /path/to/gateway-watchdog.sh`), comment it out — both watchdogs running at once is fine (just redundant), but wastes cycles and double-logs.

```bash
crontab -e
# put a # in front of the watchdog line, save
```

## 5. systemd-user persistence

By default, a user's systemd services die when the user logs out. To keep them running regardless:

```bash
sudo loginctl enable-linger $USER
```

This is required on headless servers where you're not always SSH'd in.

## Troubleshooting

### Bot is up but watchdog never fires

The bot logs `watchdog loop starting (interval=180s)` on boot if the loop registered successfully. If you don't see that line in `bot.log`, either the bot crashed during init, or `WATCHDOG_INTERVAL_SEC` is misconfigured. Check `systemctl --user status ibkr-gateway-rcon-bot.service` for stack traces.

### Deadman keeps restarting the bot

If the bot is unable to refresh its heartbeat (e.g., the state dir isn't writable, or `watchdog_tick` raises on every call), the deadman will loop forever. Check:

- `bot.log` for exceptions
- Permissions on `state/` — the bot's user must be able to write there
- `state/bot.heartbeat` ages — if it updates then stops, check the port-probe config (netstat vs ss vs wsl-cmd-netstat)

### Deadman fires on startup

Expected for the first run — heartbeat doesn't exist yet. The `OnBootSec=2min` delay gives the bot time to write one before the first deadman check.
