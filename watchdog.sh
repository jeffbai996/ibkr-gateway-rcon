#!/bin/bash
# Gateway watchdog — checks whether each configured gateway is listening on its
# port. If not, and no active skip-file exists for it, fires the restart command.
#
# Designed to be invoked by cron every few minutes. The skip-file logic is
# intentionally implemented here in shell (not Python) so the watchdog never
# hangs waiting for a Python interpreter. Python is used only by operators
# reading/writing skip-files through gateway_ctl.py.
#
# USAGE:
#   watchdog.sh /abs/path/to/config.yaml
#
# Expects a tiny helper on PATH (bundled in this repo):
#   gwctl status-one <config> <gateway-name>
# which prints "up|down skipped|active"
#
# That avoids parsing YAML in bash.

set -euo pipefail

CONFIG="${1:-}"
if [[ -z "$CONFIG" ]]; then
    echo "usage: $0 /abs/path/to/config.yaml" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GWCTL="$HERE/gwctl"
if [[ ! -x "$GWCTL" ]]; then
    GWCTL="python3 $HERE/gwctl.py"
fi

LOG="$(cd "$(dirname "$CONFIG")" && pwd)/$(python3 -c "import yaml,sys; print(yaml.safe_load(open('$CONFIG'))['log_file'])")"

timestamp() { date -u '+%Y-%m-%d %H:%M:%S'; }

# Gateway list comes from gwctl so YAML parsing stays in one place.
GATEWAYS=$($GWCTL list-names "$CONFIG")

for name in $GATEWAYS; do
    read -r status skipped <<<"$($GWCTL status-one "$CONFIG" "$name")"

    if [[ "$skipped" == "skipped" ]]; then
        # Paused by operator. Silently move on.
        continue
    fi

    if [[ "$status" == "up" ]]; then
        continue
    fi

    echo "$(timestamp) — port probe failed for $name, restarting $name gateway" >> "$LOG"
    $GWCTL restart-one "$CONFIG" "$name" >> "$LOG" 2>&1 || true
    echo "$(timestamp) — $name restart command issued" >> "$LOG"
done
