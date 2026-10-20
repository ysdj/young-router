#!/bin/bash
# Which processes an installed Young Router bundle is running.
#
# ``restart-app.sh`` has to name the pids it replaces *before* it asks the host
# to quit: the host's children are reparented the moment it exits, so the tree
# cannot be re-derived afterwards.  That capture is by bundle path and
# parentage, never by the app's name.  A name match also catches a process whose
# command line merely mentions the app -- another session's test run under the
# bundle's own interpreter, a probe shell, a log viewer -- and a captured pid
# that will never exit sits out the whole stop budget with the app down, while
# the backstop that follows signals processes that are not this app's.
#
#   app-processes.sh tree  <app-path>    the bundle's host process and every
#                                        descendant of it, one pid per line
#   app-processes.sh hosts <app-path>    the bundle's host processes alone
#   app-processes.sh stop  <pid>...      signal them together and wait for them
#                                        to be gone; survivors are printed and
#                                        the exit status is 1
#   app-processes.sh orphans <app-path>  the bundle's worker processes left
#                                        without a Core after a failed install
#   app-processes.sh alive <pid>...      those of them that are still running,
#                                        one pid per line
set -uo pipefail

usage() {
  echo "usage: $(basename "$0") tree <app-path> | hosts <app-path> | stop <pid>... | alive <pid>..." >&2
  exit 2
}

# The host is the bundle's own executable, so its command line begins with a
# path directly inside Contents/MacOS.  A process merely named after the app
# does not, and a test run under the bundle's interpreter -- whose first word is
# that interpreter's path, under Contents/Resources -- does not either.
#
# "Directly inside" is read from the first word after that prefix, not from the
# whole line: the app path itself may contain a space, and a host started with
# arguments has more words after it, so only the name right after Contents/MacOS
# can be tested for a further slash.
host_pids() {
  ps -axo pid=,command= | awk -v prefix="$1/Contents/MacOS/" '
    {
      pid = $1
      command = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", command)
      if (index(command, prefix) != 1) next
      split(substr(command, length(prefix) + 1), word, /[[:space:]]+/)
      if (word[1] ~ /\//) next
      print pid
    }
  '
}

descendants() {
  local pid child
  for pid in "$@"; do
    [[ -n "$pid" ]] || continue
    while read -r child; do
      [[ -n "$child" ]] || continue
      printf '%s\n' "$child"
      descendants "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
  done
}

orphaned_workers() {
  local app_path="$1" pid parent command
  local worker="$app_path/Contents/Resources/Core/young_router/adapters/our_free_model_worker.mjs"
  # A failed install can retire an entire app bundle while its Core is still
  # starting its managed worker. The worker is then reparented before the next
  # install can capture it through the host tree. A live Core from any bundle
  # owns its own worker; every other process running this bundle's exact worker
  # script is a survivor of that bundle's retired process tree.
  for pid in $(pgrep -f "our_free_model_worker\.mjs" 2>/dev/null || true); do
    parent="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')"
    command="$(ps -p "$parent" -o command= 2>/dev/null || true)"
    if [[ -n "$parent" && "$command" != *"young_router.core"* ]] \
      && [[ "$(ps -p "$pid" -o command= 2>/dev/null || true)" == *"$worker"* ]]; then
      printf '%s\n' "$pid"
    fi
  done
}

# Replacing a bundle's processes is one policy for every entry point, so it
# lives here beside the rules that decide which processes those are: the
# captured pids are signalled together, in one shot, and the caller waits only
# for them to be gone.  Asking the host to quit first and letting it unwind its
# own tree -- what this replace used to wait on, with a grace budget -- only
# postponed the replacement: measured on this app, the host's quit path and the
# Core's shutdown need ten to thirty seconds under load, while the loopback
# listener the replacement needs is released in the same instant the processes
# holding it die.  A caller with survivors to report gets them on stdout and a
# non-zero status; SIGKILL cannot be escalated further, so there is no ladder.
#
# No caller waits for a proxy drain here.  The four-hour drain belongs to a
# replacement the Core performs on purpose and the short bound to the host's own
# quit path; ``young_router/lifecycle.py`` states which stop uses which, and a
# grace budget in this path is the wait being reintroduced.
stop() {
  local pid tries
  [[ $# -ge 1 ]] || usage
  for pid in "$@"; do
    [[ -n "$pid" ]] || continue
    kill -KILL "$pid" 2>/dev/null || true
  done
  tries="${APP_PROCESSES_STOP_POLLS:-100}"
  for _ in $(seq 1 "$tries"); do
    [[ -n "$(alive "$@")" ]] || return 0
    sleep 0.05
  done
  alive "$@"
  return 1
}

stop_installed_app_when_ready() {
  local destination="$1" app_pids app_pid deadline proxy_ready
  deadline=$((SECONDS + 40))
  while :; do
    app_pids="$("$0" hosts "$destination")"
    [[ -n "$app_pids" ]] && break
    (( SECONDS < deadline )) || return 0
    sleep 0.05
  done
  # The host's Core launches the proxy asynchronously. Its normal quit path
  # replaces Core, which would leave a host running after this stop returned.
  deadline=$((SECONDS + 40))
  while :; do
    proxy_ready=0
    while read -r app_pid; do
      [[ -n "$app_pid" ]] || continue
      if ps -p "$app_pid" -o command= 2>/dev/null | grep -q -- ' -m young_router.core --endpoint-file '; then
        app_pids="$("$0" tree "$destination")"
        if ps -p $app_pids -o command= 2>/dev/null | grep -q -- ' -m young_router.proxy.macos_proxy '; then
          proxy_ready=1
        fi
        break
      fi
    done <<<"$app_pids"
    (( proxy_ready == 0 )) || break
    (( SECONDS < deadline )) || break
    sleep 0.05
  done
  app_pids="$("$0" hosts "$destination")"
  [[ -z "$app_pids" ]] || stop $app_pids
}

tree() {
  local roots
  [[ -n "${1:-}" ]] || usage
  roots="$(host_pids "$1" | tr '\n' ' ')"
  for pid in $roots; do
    printf '%s\n' "$pid"
  done
  descendants $roots
}

# A pid the kernel still lists after its exit is a zombie: its parent is gone,
# nothing here will ever reap it, and counting it as running is the other way
# the stop budget gets spent against a process that has already ended.
alive() {
  local pid state
  for pid in "$@"; do
    [[ -n "$pid" ]] || continue
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    [[ -n "$state" && "$state" != Z* ]] || continue
    printf '%s\n' "$pid"
  done
}

[[ $# -ge 1 ]] || usage
case "$1" in
  tree)
    shift
    tree "$@"
    ;;
  hosts)
    shift
    [[ -n "${1:-}" ]] || usage
    host_pids "$1"
    ;;
  stop)
    shift
    stop "$@"
    ;;
  stop-when-ready)
    shift
    [[ -n "${1:-}" ]] || usage
    stop_installed_app_when_ready "$1"
    ;;
  orphans)
    shift
    [[ -n "${1:-}" ]] || usage
    orphaned_workers "$1"
    ;;
  alive)
    shift
    alive "$@"
    ;;
  *)
    usage
    ;;
esac
