#!/usr/bin/env bash
# Install the daily acquisition timer — one systemd template, one timer per
# county, so counties rotate across the week instead of hammering PropStream
# in a single burst.
#
#   sudo scripts/install-acquire-timer.sh galveston brazoria chambers
#   systemctl list-timers 'aria-acquire@*'
#   journalctl -u 'aria-acquire@*' -f
#
# Each firing: pull -> save list -> skip trace -> poll for contacts ->
# export -> ledger. It NEVER enrolls; the batch waits for a human push.

set -euo pipefail

REPO="${REPO:-/root/Aria-Swarm}"
PY="$REPO/.venv/bin/python"
STATE="${STATE:-tx}"
HOUR="${HOUR:-08}"
MAX_ROWS="${MAX_ROWS:-10000}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo"; exit 1; }
[[ -x "$PY" ]] || { echo "no venv python at $PY (set REPO=...)"; exit 1; }
[[ $# -ge 1 ]] || { echo "usage: $0 <county> [county...]"; exit 1; }

cat > /etc/systemd/system/aria-acquire@.service <<EOF
[Unit]
Description=ARIA acquisition — %i: pull, skip trace, export, ledger
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
EnvironmentFile=$REPO/.env
Environment=PLAYWRIGHT_BROWSERS_PATH=/root/.automaton/ms-playwright
ExecStart=$PY -m tools.acquisition.cli pipeline --county %i --state $STATE --max-rows $MAX_ROWS
TimeoutStartSec=5400
Nice=10
EOF

cat > /etc/systemd/system/aria-acquire@.timer <<EOF
[Unit]
Description=Daily ARIA county pull — %i

[Timer]
OnCalendar=*-*-* $HOUR:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload

# Stagger the counties: one per weekday, so a single day never runs two pulls
day=1
for county in "$@"; do
    dropin="/etc/systemd/system/aria-acquire@${county}.timer.d"
    mkdir -p "$dropin"
    cat > "$dropin/schedule.conf" <<EOF
[Timer]
OnCalendar=
OnCalendar=$(date -d "$day day" +%a) *-*-* $HOUR:00
EOF
    systemctl enable --now "aria-acquire@${county}.timer"
    echo "enabled aria-acquire@${county}.timer ($(date -d "$day day" +%A) ${HOUR}:00)"
    day=$((day + 1))
done

systemctl daemon-reload
echo
echo "scheduled:"
systemctl list-timers 'aria-acquire@*' --no-pager || true
echo
echo "Each morning after a run:"
echo "  $PY -m tools.acquisition.cli enroll --include-unscreened --limit 2000"
echo "  $PY -m tools.acquisition.cli enroll --include-unscreened --limit 2000 --push"
echo "  $PY -m tools.acquisition.cli review list"
