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
# The bundle's own host process, not every process whose command line happens to
# contain the app name.  A blunt match also catches the Core, the managed
# proxy's sixteen workers, and the credential-holding WorkBuddy worker, and
# signalling that whole tree together inverts the ordered teardown the host
# performs: the workers die in the same instant as the proxy they feed, so a
# turn that was mid-answer loses its upstream.  The stop below therefore
# addresses the host and lets it unwind its own tree, exactly as the build
# script's installer does.
HOST_PATTERN='/Contents/MacOS/YoungRouter$'
# The pids this stop replaces are that host and its descendants, resolved from
# the bundle before the quit below because the host's children are reparented
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
    pgrep -f "$HOST_PATTERN" >/dev/null 2>&1 || open -a "$APP" >/dev/null 2>&1 || true
  ) >/dev/null 2>&1 &
  disown 2>/dev/null || true
}
trap relaunch EXIT INT TERM

if pgrep -f "$HOST_PATTERN" >/dev/null 2>&1; then
  echo "Young Router: stopping (restart armed)"
  # The pids that exist *now* are the ones being replaced.  Waiting on a name
 # pattern instead would also match a host that something else starts while
  # this stop is in progress, so the loop would sit out its whole budget
  # against an app that is already running and healthy.
  STOPPING_PIDS="$("$APP_PROCESSES" tree "$APP" | tr '\n' ' ')"
  # Ask the app to quit rather than signalling it.  AppKit only runs
  # ``applicationShouldTerminate`` -- the one path that reaches the Core's
  # ``host/shutdown`` route -- on a real quit request; a SIGTERM skips it, so
  # the Core falls back to its parent watchdog and stops the proxy on the
  # full drain, which is what kept a restart waiting for the longest turn it
  # had accepted.  Both clients reconnect, so this costs one reconnect and
  # nothing is cut without the client being told.
  #
  # The request is answered asynchronously: AppKit returns ``NSTerminateLater``
  # and does the Core's stop on a background queue, so the host is still alive
  # for a moment after ``osascript`` returns.  Signalling on the next line
  # killed the host before it could run that path, which is why the marker was
  # never written and the restart fell back to the drain this exists to avoid.
  # The request is therefore given the time the host itself allows for it
  # before the signal is used as the backstop.
  osascript -e 'tell application id "young.router.app" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do
    pgrep -f "$HOST_PATTERN" >/dev/null 2>&1 || break
    sleep 0.1
  done
  pgrep -f "$HOST_PATTERN" >/dev/null 2>&1 && pkill -TERM -f "$HOST_PATTERN" 2>/dev/null || true
  # The host's teardown is a few seconds; poll at a tenth of a second so an
  # ordinary restart starts again the moment it is genuinely done instead of
  # sitting out a fixed budget, and bound it by the same drain limit the Core
  # reads rather than by a guess.  Liveness is asked of the captured pids
  # themselves, and a zombie -- a pid the kernel still lists after its exit,
  # which nothing here will ever reap -- is not one of them.
  STOP_POLLS=600
  stopping_any() {
    [[ -n "$("$APP_PROCESSES" alive $STOPPING_PIDS)" ]]
  }
  for _ in $(seq 1 "$STOP_POLLS"); do
    stopping_any || break
    sleep 0.1
  done
  if stopping_any; then
    # Anything the host did not unwind -- a descendant it never owned, or a
    # wedged process holding the loopback port the replacement needs -- still
    # has to go before the new instance can bind.  Signalled through the pids
    # this stop captured, so a process that is not this app's is never touched.
    for pid in $("$APP_PROCESSES" alive $STOPPING_PIDS); do
      kill -TERM "$pid" 2>/dev/null || true
    done
    for _ in $(seq 1 20); do
      stopping_any || break
      sleep 0.1
    done
    if stopping_any; then
      for pid in $("$APP_PROCESSES" alive $STOPPING_PIDS); do
        kill -9 "$pid" 2>/dev/null || true
      done
    fi
  fi
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

# A cold start reaches a healthy proxy in about four seconds, so this polls for
# it rather than sleeping a fixed interval before the first check: the previous
# form paid three seconds per attempt even when the proxy was already up.
START_TIMEOUT_SECONDS="${YOUNG_ROUTER_START_TIMEOUT_SECONDS:-70}"
deadline=$(( SECONDS + START_TIMEOUT_SECONDS ))
while (( SECONDS < deadline )); do
  if pgrep -f "$HOST_PATTERN" >/dev/null 2>&1; then
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
