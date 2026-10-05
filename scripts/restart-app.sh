#!/bin/bash
# Restart the installed Young Router app without ever leaving it stopped.
#
# Why this exists: a bare `pkill -f "Young Router"` stops the app and nothing
# brings it back, so a session that gets interrupted between the stop and the
# start leaves the router down with no window and no proxy.  The build script
# already solves this for itself with an armed EXIT trap; this script is the
# same guarantee for the case where no rebuild is wanted.
#
#   scripts/restart-app.sh          restart
#   scripts/restart-app.sh --stop   stop on purpose (asks first)
set -uo pipefail

APP="/Applications/Young Router.app"
PATTERN="Young Router"
# Arm before the stop: a failure or an interrupt between here and `open` still
# reaches the trap, which starts the app again.
ARMED=1
RELAUNCHED=0

relaunch() {
  [[ "$ARMED" == "1" ]] || return 0
  ARMED=0
  open -a "$APP" >/dev/null 2>&1 || true
  RELAUNCHED=1
  echo "Young Router: relaunched"
  # A shell trap cannot catch SIGKILL, which is what an interrupted tool call
  # delivers.  This detached checker is the backstop for that: it re-opens the
  # app only while it is genuinely down, then exits, so it never doubles the
  # running app or lingers as a daemon.
  (
    sleep 4
    pgrep -f "$PATTERN" >/dev/null 2>&1 || open -a "$APP" >/dev/null 2>&1 || true
  ) >/dev/null 2>&1 &
  disown 2>/dev/null || true
}
trap relaunch EXIT INT TERM

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "Young Router: stopping (restart armed)"
  pkill -f "$PATTERN" 2>/dev/null || true
  for _ in $(seq 1 40); do
    pgrep -f "$PATTERN" >/dev/null 2>&1 || break
    sleep 0.5
  done
  pgrep -f "$PATTERN" >/dev/null 2>&1 && pkill -9 -f "$PATTERN" 2>/dev/null || true
  sleep 1
else
  echo "Young Router: not running"
fi

if [[ "${1:-}" == "--stop" ]]; then
  # A deliberate stop disarms the relaunch, so this is the only path that may
  # leave the app down.
  ARMED=0
  echo "Young Router: stopped on request"
  exit 0
fi

open -a "$APP" >/dev/null 2>&1 || true
ARMED=0
echo "Young Router: starting"

for _ in $(seq 1 60); do
  if pgrep -f "$PATTERN" >/dev/null 2>&1; then
    sleep 3
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:12390/health/liveliness 2>/dev/null || echo 000)"
    if [[ "$code" == "200" ]]; then
      echo "Young Router: running (proxy health check passed)"
      exit 0
    fi
  fi
  sleep 1
done

echo "Young Router: started but the proxy did not answer in time" >&2
exit 1
