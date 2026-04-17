# Running the watchdog from cron

Edit your user crontab (`crontab -e`) and add one line per frequency you want:

```
*/3 * * * * /abs/path/to/ibkr-gateway-rcon/watchdog.sh /abs/path/to/ibkr-gateway-rcon/config.yaml
```

## Why `*/3`?

Interactive Brokers' gateways take ~45–90 seconds to come up on a cold restart. Checking every 3 minutes is a reasonable balance between "fast recovery from a crash" and "don't trigger redundant restart cascades." Choose whatever works for your setup — the watchdog is idempotent so a shorter interval won't do damage, just extra log noise.

## Pausing via cron vs pausing via skip-file

Before this repo, the only way to silence the watchdog was to comment out its cron entry — a nuclear option that paused everything. The skip-file protocol is per-gateway and supports auto-resume, so you can say "pause `secondary` for 30 minutes" without touching cron or affecting `primary`.

The cron entry itself should stay enabled in normal operation. Operators interact with the *skip-files*, not the cron line.

## Verifying

After wiring the cron entry, watch the log file tail:

```
tail -f /abs/path/to/ibkr-gateway-rcon/watchdog.log
```

You should see entries only when a port probe actually fails. If ports are healthy and no skip-files exist, the log stays silent.
