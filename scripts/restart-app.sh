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
#   scripts/restart-app.sh --stop   stop on purpose and leave it stopped
#
# The stop is one policy, shared with the build script's installer: the pids the
# bundle is running are captured, killed together, and the replacement starts as
# soon as they are gone.  See ``app-processes.sh stop``.
set -uo pipefail

APP="/Applications/Young Router.app"
# The pids this stop replaces are that host and its descendants, resolved from
# the bundle before the stop below because the host's children are reparented
# the moment it exits and the tree cannot be re-derived afterwards.  Resolving
# it by the app's name instead also captures a process that merely mentions the
# app -- another session's test run under the bundle's own interpreter, a probe
# shell, a log viewer -- and a captured pid that will never exit spends the
# whole stop budget below with the app down.
APP_PROCESSES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/app-processes.sh"
if [[ ! -x "$APP_PROCESSES" ]]; then
  echo "Young Router: $APP_PROCESSES is missing; not touching a running app" >&2
  exit 1
fi
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
    [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]] || open -a "$APP" >/dev/null 2>&1 || true
  ) >/dev/null 2>&1 &
  disown 2>/dev/null || true
}
trap relaunch EXIT INT TERM

if [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]]; then
  echo "Young Router: stopping (restart armed)"
  STOPPING_PIDS="$("$APP_PROCESSES" tree "$APP" | tr '\n' ' ')"
  RUNTIME_ROOT="${LITELLM_RUNTIME_ROOT:-$HOME/.young-router/.litellm-runtime}"
  recorded_port="$(cat "$RUNTIME_ROOT/litellm.port" 2>/dev/null || true)"
  [[ "$recorded_port" =~ ^[0-9]{1,5}$ ]] || recorded_port=12390
  port_taken() {
    [[ -n "$(lsof -nP -iTCP:"$recorded_port" -sTCP:LISTEN -t 2>/dev/null)" ]]
  }
  # The stop is one shot, and it is the same call the build script's installer
  # makes: every pid captured above is killed together, and the replacement
  # starts as soon as they are gone.  What that gives up is a turn that is still
  # being written -- its upstream dies with the worker, so the client reconnects
  # and finishes its answer -- and the app's own orderly teardown, which is the
  # point.  Measured here: the host's quit path and the Core's shutdown need ten
  # to thirty seconds under load, while the loopback listener the replacement
  # needs is released in the same instant the processes holding it die, so
  # waiting for that teardown was the whole of a restart's stop.
  survivors="$("$APP_PROCESSES" stop $STOPPING_PIDS)" || true
  if [[ -n "$survivors" ]]; then
    # SIGKILL cannot be escalated, so there is no ladder left to climb: these
    # are processes the kernel has not ended yet.  They are named rather than
    # hidden, because one of them can be what makes the new proxy step forward.
    for pid in $survivors; do
      echo "Young Router: process $pid survived being stopped" >&2
    done
  fi
  # One check, not a wait: the new proxy binds the recorded port, and clients
  # are pointed at that port by hand, so a listener that somehow outlived the
  # pids above would send it forward to the next one.  A held port here is
  # reported rather than waited out; a second is enough for a listener that was
  # already closing to finish.
  for _ in $(seq 1 10); do
    port_taken || break
    sleep 0.1
  done
  if port_taken; then
    echo "Young Router: port $recorded_port is still held; the new proxy will step forward" >&2
  fi
  still_running="$("$APP_PROCESSES" alive $STOPPING_PIDS | tr '\n' ' ')"
  if [[ -n "$still_running" ]]; then
    echo "Young Router: replacing with $(wc -w <<<"$still_running" | tr -d ' ') process(es) still running" >&2
  fi
else
  echo "Young Router: not running"
fi

if [[ "${1:-}" == "--stop" ]]; then
  # A deliberate stop disarms the relaunch, so this is the only path that may
  # leave the app down.  It is the same stop as above: one process tree, one
  # signal, no grace budget for a caller that has already decided.
  ARMED=0
  echo "Young Router: stopped on request"
  exit 0
fi

open -a "$APP" >/dev/null 2>&1 || true
ARMED=0
echo "Young Router: starting"

# A cold start reaches a healthy proxy in about four seconds, so this polls for
# it rather than sleeping a fixed interval before the first check: the previous
# form paid three seconds per attempt even when the proxy was already up.
START_TIMEOUT_SECONDS="${YOUNG_ROUTER_START_TIMEOUT_SECONDS:-70}"
deadline=$(( SECONDS + START_TIMEOUT_SECONDS ))
while (( SECONDS < deadline )); do
  if [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:12390/health/liveliness 2>/dev/null || echo 000)"
    if [[ "$code" == "200" ]]; then
      echo "Young Router: running (proxy health check passed)"
      exit 0
    fi
  fi
  sleep 0.2
done

echo "Young Router: started but the proxy did not answer in time" >&2
exit 1
