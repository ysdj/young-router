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
    [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]] || open -a "$APP" >/dev/null 2>&1 || true
  ) >/dev/null 2>&1 &
  disown 2>/dev/null || true
}
trap relaunch EXIT INT TERM

if [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]]; then
  echo "Young Router: stopping (restart armed)"
  STOPPING_PIDS="$("$APP_PROCESSES" tree "$APP" | tr '\n' ' ')"
  STOPPING_HOSTS="$("$APP_PROCESSES" hosts "$APP" | tr '\n' ' ')"
  STOPPING_CORE="$("$APP_PROCESSES" core "$APP" | tr '\n' ' ')"
  # What the replacement has to take over is three things and nothing else: the
  # host, which owns the window and the Core beneath it; the Core, which owns
  # the state root; and the recorded loopback port, because a new proxy that
  # cannot bind it steps forward to the next one while every client -- each one
  # pointed at the configured port by hand -- is left talking to a port nothing
  # listens on.  The proxy's own shutdown spine -- its launcher, the forkserver,
  # the resource tracker, a worker still finishing a turn it accepted -- holds
  # none of the three.  Measured on this app: the port is released about a
  # second into the stop, that spine lives on for another thirty, and the
  # launcher writes no state file.  Waiting for it was most of a restart.
  #
  # The Core is captured for the graceful path, which waits for the pids that
  # own the window and the state root instead of for the whole tree.
  if [[ -n "$STOPPING_CORE" ]]; then
    GATING_PIDS="$STOPPING_HOSTS $STOPPING_CORE"
  else
    GATING_PIDS="$STOPPING_PIDS"
  fi
  RUNTIME_ROOT="${LITELLM_RUNTIME_ROOT:-$HOME/.young-router/.litellm-runtime}"
  recorded_port="$(cat "$RUNTIME_ROOT/litellm.port" 2>/dev/null || true)"
  [[ "$recorded_port" =~ ^[0-9]{1,5}$ ]] || recorded_port=12390
  port_taken() {
    [[ -n "$(lsof -nP -iTCP:"$recorded_port" -sTCP:LISTEN -t 2>/dev/null)" ]]
  }
  # The stop is one shot: every pid captured above goes at once.  What that
  # gives up is a turn that is still being written -- its upstream dies with the
  # worker, so the client reconnects instead of being handed a completed answer
  # -- and the app's own orderly teardown, which is the point.  Measured here:
  # the host's quit path and the Core's shutdown need ten to thirty seconds
  # under load, and the only thing the replacement actually has to wait for is
  # the loopback listener those processes hold.  Killing them together releases
  # it in the same instant, so the stop costs the poll below instead of the
  # drain above.
  #
  # YOUNG_ROUTER_RESTART_GRACEFUL=1 keeps the other policy: ask the app to quit
  # and let it unwind its own tree, for the case where a turn must not be cut.
  # AppKit only runs ``applicationShouldTerminate`` -- the one path that reaches
  # the Core's ``host/shutdown`` route -- on a real quit request, and the request
  # is answered asynchronously, so the host is still alive for a moment after
  # ``osascript`` returns and gets only that long before the signal is used as
  # the backstop.
  if [[ "${YOUNG_ROUTER_RESTART_GRACEFUL:-$([[ "${1:-}" == "--stop" ]] && echo 1 || echo 0)}" == "1" ]]; then
    osascript -e 'tell application id "young.router.app" to quit' >/dev/null 2>&1 || true
    host_alive() {
      [[ -n "$("$APP_PROCESSES" alive $STOPPING_HOSTS)" ]]
    }
    for _ in $(seq 1 60); do
      host_alive || break
      sleep 0.1
    done
    if host_alive; then
      for pid in $("$APP_PROCESSES" alive $STOPPING_HOSTS); do
        kill -TERM "$pid" 2>/dev/null || true
      done
    fi
  else
    for pid in $STOPPING_PIDS; do
      kill -KILL "$pid" 2>/dev/null || true
    done
  fi
  # The replacement may start as soon as those pids are gone: they are what
  # holds the listener, and a killed process holds nothing else that outlives
  # it.  A zombie is not one of them, which is why liveness is asked of the
  # captured pids rather than of a name.
  STOP_POLLS=100
  stopping_any() {
    [[ -n "$("$APP_PROCESSES" alive $STOPPING_PIDS)" ]]
  }
  for _ in $(seq 1 "$STOP_POLLS"); do
    stopping_any || break
    sleep 0.1
  done
  if stopping_any; then
    # Something survived the signal: a process the kernel kept alive, or the
    # graceful path's host that never answered its quit.  Signalled through the
    # pids this stop captured, so a process that is not this app's is never
    # touched.
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
  # leave the app down -- and it is also the one stop that is not in a hurry, so
  # it asks the app to quit unless the caller chose otherwise.
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
