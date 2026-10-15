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
#   app-processes.sh alive <pid>...      those of them that are still running,
#                                        one pid per line
set -uo pipefail

usage() {
  echo "usage: $(basename "$0") tree <app-path> | alive <pid>..." >&2
  exit 2
}

# The host is the bundle's own executable, so its command line begins with a
# path directly inside Contents/MacOS.  A process merely named after the app
# does not, and a test run under the bundle's interpreter -- whose first word is
# that interpreter's path, under Contents/Resources -- does not either.
host_pids() {
  ps -axo pid=,command= | awk -v prefix="$1/Contents/MacOS/" '
    {
      pid = $1
      command = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", command)
      if (index(command, prefix) == 1 && substr(command, length(prefix) + 1) !~ /\//) print pid
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
  alive)
    shift
    alive "$@"
    ;;
  *)
    usage
    ;;
esac
