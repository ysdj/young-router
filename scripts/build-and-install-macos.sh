#!/usr/bin/env bash
#
# The one macOS build script: it builds the bundle and then installs and
# restarts the app that runs it.
#
# It is also the build-only entry point. Calling it with
# ``YOUNG_ROUTER_MACOS_OUTPUT`` names the bundle the caller wants and gets
# exactly that: built, verified, left at that path, with nothing installed and
# no running app touched. CI and scripts/package-release.sh use it that way.
# Without that variable the same verified bundle replaces
# /Applications/Young Router.app and the installed app is restarted.
#
# One file serves both because the install step consumes exactly the bundle
# this build produced; a second script would either repeat the build or
# re-validate an artifact it did not make.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RN_ROOT="$ROOT/rn"
PROJECT_ROOT="$ROOT"
APP_ROOT="$RN_ROOT/apps/macos"
DEVELOPER_DIR="${DEVELOPER_DIR:-}"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
RUNTIME_SOURCE="${YOUNG_ROUTER_CORE_RUNTIME_SOURCE:-${LITELLM_RELEASE_RUNTIME_SOURCE:-}}"
ARCH="$(uname -m)"
# A caller that names an output owns the artifact: build only.
BUILD_ONLY=0
if [[ -n "${YOUNG_ROUTER_MACOS_OUTPUT:-}" ]]; then
  BUILD_ONLY=1
else
  # Installing means the build product is a staging bundle, not the
  # DerivedData app: moving that one into /Applications would evict it from
  # the incremental build tree. A staging root on the install volume is what
  # also lets the verified bundle be renamed into place instead of copied.
  STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/young-router-install.XXXXXX")"
  YOUNG_ROUTER_MACOS_OUTPUT="$STAGE_ROOT/Young Router.app"
  export YOUNG_ROUTER_MACOS_OUTPUT
fi
DESTINATION="${YOUNG_ROUTER_INSTALL_APP:-/Applications/Young Router.app}"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
RUNTIME_WORK="$(mktemp -d "${TMPDIR:-/tmp}/young-router-rn-runtime.XXXXXX")"
# Third-party staging lives here instead of in the per-build temporary tree
# above, because a build stages the same four releases over and over: each
# script resolves its upstream release on every run, and re-installing a
# release it has already staged is the single largest remaining cost of a local
# build.  A tree staged here records the release it came from, and a later
# build reuses it only when its own lookup resolves that exact release (see
# ``reused_staged_release`` in scripts/update_common.py).  A changed release,
# a partial tree, or no cache at all stages from scratch, so deleting this
# directory is always safe.
STAGING_CACHE="${YOUNG_ROUTER_STAGING_CACHE:-$RN_ROOT/.staging-cache}"
STAGE_ROOT="${STAGE_ROOT:-}"
INSTALL_STAGE=""
PREVIOUS_APP=""
FAILED_APP=""
INSTALL_COMPLETE=0
RESTART_ARMED=0
OLD_PIDS=""
NEW_PIDS=""
START_TIMEOUT_SECONDS="${YOUNG_ROUTER_START_TIMEOUT_SECONDS:-70}"
STOP_TIMEOUT_SECONDS="${YOUNG_ROUTER_STOP_TIMEOUT_SECONDS:-20}"
STOP_GRACE_POLLS=20
# How long the replacement host may wait for the running app to finish the
# turns it already accepted before the stale tree is forced down.  It matches
# the Core's own proxy-drain budget (same variable, same default) so the two
# layers agree on when a slow drain has become a wedged one.
STOP_DRAIN_SECONDS="${YOUNG_ROUTER_PROXY_DRAIN_SECONDS:-300}"
REQUIRED_HEALTH_CHECKS=3
LAUNCH_RETRY_SECONDS=1
LAST_STARTUP_STATE=""

cleanup() {
  [[ -z "$RUNTIME_WORK" ]] || rm -rf "$RUNTIME_WORK"
  if [[ "$INSTALL_COMPLETE" != "1" && -n "$PREVIOUS_APP" && -e "$PREVIOUS_APP" && ! -e "$DESTINATION" ]]; then
    mv "$PREVIOUS_APP" "$DESTINATION" || true
  fi
  [[ -z "$INSTALL_STAGE" || ! -d "$INSTALL_STAGE" ]] || rm -rf "$INSTALL_STAGE"
  [[ -z "$FAILED_APP" || ! -d "$FAILED_APP" ]] || rm -rf "$FAILED_APP"
  [[ -z "$STAGE_ROOT" || ! -d "$STAGE_ROOT" ]] || rm -rf "$STAGE_ROOT"
  if [[ "$RESTART_ARMED" == "1" && ( -e "$DESTINATION" || -L "$DESTINATION" ) ]]; then
    [[ -n "$(installed_pids)" ]] || open -g "$DESTINATION" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
cd "$RN_ROOT"

copy_tree() {
  local source="$1"
  local destination="$2"
  mkdir -p "$destination"
  # On APFS, -c uses clonefile and falls back to a regular copy across volumes.
  cp -ac "$source/." "$destination/"
}

copy_bundle() {
  local source="$1"
  local destination="$2"
  mkdir -p "$(dirname "$destination")"
  # Preserve the bundle's nested code signatures and extended attributes when
  # copying a verified build into place. `cp -a` can drop those signatures even
  # when the source bundle itself verifies.
  ditto --rsrc --extattr --acl "$source" "$destination"
}

refresh_installed_app_icon() {
  # The installed path, bundle identifier, and build number intentionally stay
  # stable across local builds. Notify Finder of the replacement and force
  # LaunchServices to reload AppIcon.icns instead of retaining its placeholder.
  touch "$DESTINATION"
  "$LSREGISTER" -f "$DESTINATION"
}

bundle_roots() {
  ps -axo pid=,command= \
    | awk '
        {
          pid = $1
          line = $0
          sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", line)
          if (line ~ /\/Young ?Router[^\/]*\.app\/Contents\/MacOS\/YoungRouter$/) print pid
        }
      '
}

process_tree() {
  local pid child
  for pid in "$@"; do
    [[ -n "$pid" ]] || continue
    printf '%s\n' "$pid"
    while read -r child; do
      [[ -n "$child" ]] || continue
      process_tree "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
  done
  return 0
}

bundle_processes() {
  local root
  while read -r root; do
    [[ -n "$root" ]] && process_tree "$root"
  done < <(bundle_roots) | sort -n -u
  return 0
}

installed_pids() {
  local pid command
  while read -r pid; do
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    [[ "$command" == */Young*Router*.app/Contents/MacOS/YoungRouter ]] && printf '%s\n' "$pid"
  done < <(bundle_processes)
  return 0
}

stop_installed_app() {
  local deadline bundle_pids="$1" grace_polls pid state roots descendants
  [[ -n "$bundle_pids" ]] || return 0

  OLD_PIDS="$bundle_pids"
  # Stop the host first and let it unwind, instead of signalling every
  # descendant at once.
  #
  # Core owns an ordered teardown: it drains the proxy, then releases the
  # upstream workers the proxy streams through.  Signalling the whole tree
  # together inverted that order -- the workers died in the same instant as the
  # proxy they feed, so a turn that was mid-answer lost its upstream before the
  # proxy could finish it and the user had to resend.  The kill ladder below
  # also used to pre-empt that drain after one second.
  roots="$(bundle_roots)"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<<"$roots"

  # The host's own end is immediate; the work it leaves behind is the drain.
  # Wait for it, bounded by the same budget the Core reads, so an ordinary
  # restart never cuts a turn that is still being written.
  grace_polls=$(( STOP_DRAIN_SECONDS * 20 ))
  while pids_are_alive "$bundle_pids" && (( grace_polls > 0 )); do
    sleep 0.05
    grace_polls=$((grace_polls - 1))
  done

  # Anything the host did not unwind -- a descendant it never owned, or one a
  # wedged process is holding -- still has to go before the replacement can
  # bind the same listener.
  descendants="$(bundle_processes)"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<<"$descendants"

  grace_polls=$STOP_GRACE_POLLS
  while pids_are_alive "$bundle_pids" && (( grace_polls > 0 )); do
    sleep 0.05
    grace_polls=$((grace_polls - 1))
  done
  if pids_are_alive "$bundle_pids"; then
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
      [[ -n "$state" && "$state" != Z* ]] && kill -KILL "$pid" 2>/dev/null || true
    done <<<"$bundle_pids"
  fi

  deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
  while pids_are_alive "$bundle_pids"; do
    if (( SECONDS >= deadline )); then
      echo "Young Router did not stop its captured process tree within ${STOP_TIMEOUT_SECONDS}s." >&2
      return 1
    fi
    sleep 0.05
  done
}

pids_are_alive() {
  local pid state
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    [[ -n "$state" && "$state" != Z* ]] && return 0
  done <<<"$1"
  return 1
}

pid_is_listed() {
  local expected="$1"
  local candidate
  while read -r candidate; do
    [[ "$candidate" == "$expected" ]] && return 0
  done <<<"$2"
  return 1
}

core_pid_for_app() {
  local app_pid="$1"
  local child command
  while read -r child; do
    [[ -n "$child" ]] || continue
    command=$(ps -p "$child" -o command= 2>/dev/null || true)
    if [[ "$command" == "$DESTINATION/Contents/Resources/Core/runtime/"* ]] \
      && [[ "$command" == *" -m young_router.core "* ]] \
      && [[ "$command" == *" --parent-pid $app_pid"* ]]; then
      printf '%s\n' "$child"
      return 0
    fi
  done < <(pgrep -P "$app_pid" 2>/dev/null || true)
  return 1
}

proxy_port_for_app() {
  local app_pid="$1"
  local core_pid process_pid command port
  core_pid="$(core_pid_for_app "$app_pid")" || return 1
  while read -r process_pid; do
    [[ -n "$process_pid" ]] || continue
    command=$(ps -p "$process_pid" -o command= 2>/dev/null || true)
    if [[ "$command" != *"run_server()"* \
      && "$command" != *" -m young_router.proxy.macos_proxy "* ]]; then
      continue
    fi
    port=$(awk '{ for (field = 1; field < NF; field += 1) if ($field == "--port") { print $(field + 1); exit } }' <<<"$command")
    if [[ "$port" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$port"
      return 0
    fi
  done < <(process_tree "$core_pid")
  return 1
}

app_is_ready() {
  [[ "$(startup_state_for_app "$1")" == "running" ]]
}

startup_state_for_app() {
  local app_pid="$1"
  local port
  if ! ps -p "$app_pid" -o stat= >/dev/null 2>&1; then
    printf '%s\n' "stopped"
    return 0
  fi
  if ! core_pid_for_app "$app_pid" >/dev/null; then
    printf '%s\n' "starting (Core)"
    return 0
  fi
  if ! port="$(proxy_port_for_app "$app_pid")"; then
    printf '%s\n' "starting (proxy)"
    return 0
  fi
  if curl --fail --silent --show-error --max-time 1 \
    "http://127.0.0.1:$port/health/liveliness" >/dev/null 2>&1; then
    printf '%s\n' "running"
  else
    printf '%s\n' "unhealthy (proxy health check)"
  fi
}

report_startup_state() {
  local state="$1"
  [[ "$state" == "$LAST_STARTUP_STATE" ]] && return 0
  LAST_STARTUP_STATE="$state"
  printf 'Young Router: %s\n' "$state"
}

wait_for_started_app() {
  local rejected_pids="$1"
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  local app_pids app_pid candidate_pid ready_pid stable_pid="" stable_checks=0 next_launch_at=0 state
  while :; do
    app_pids="$(installed_pids)"
    candidate_pid=""
    ready_pid=""
    while read -r app_pid; do
      [[ -n "$app_pid" ]] || continue
      pid_is_listed "$app_pid" "$rejected_pids" && continue
      candidate_pid="$app_pid"
      state="$(startup_state_for_app "$app_pid")"
      report_startup_state "$state"
      if [[ "$state" == "running" ]]; then
        ready_pid="$app_pid"
        break
      fi
    done <<<"$app_pids"
    [[ -n "$candidate_pid" ]] || report_startup_state "stopped (waiting to launch)"
    if [[ -n "$ready_pid" ]]; then
      if [[ "$ready_pid" == "$stable_pid" ]]; then
        stable_checks=$((stable_checks + 1))
      else
        stable_pid="$ready_pid"
        stable_checks=1
      fi
      if (( stable_checks >= REQUIRED_HEALTH_CHECKS )); then
        NEW_PIDS="$app_pids"
        return 0
      fi
    else
      stable_pid=""
      stable_checks=0
    fi
    (( SECONDS >= deadline )) && break
    # The app's single-instance guard can briefly still see the process we
    # just stopped through LaunchServices even after its PID exits. A normal
    # `open` may therefore activate the old registration and return without
    # starting the replacement. Retry only while no replacement process
    # exists; never request a forced new instance.
    if [[ -z "$candidate_pid" && $SECONDS -ge $next_launch_at ]]; then
      open -g "$DESTINATION" >/dev/null 2>&1 || true
      next_launch_at=$((SECONDS + LAUNCH_RETRY_SECONDS))
    fi
    sleep 0.05
  done
  report_startup_state "failed (startup timeout)"
  return 1
}

start_installed_app() {
  local rejected_pids="$1"
  # An in-place bundle replacement can leave LaunchServices briefly pointed at
  # the old instance. Start only after all old bundle processes have exited,
  # then prove the new process remains healthy before discarding the rollback
  # bundle.
  wait_for_started_app "$rejected_pids" || {
    echo "The new Young Router app did not start from $DESTINATION." >&2
    return 1
  }
  return 0
}

restore_previous_app() {
  local restore_failed=0
  if [[ -n "$PREVIOUS_APP" && ( -e "$PREVIOUS_APP" || -L "$PREVIOUS_APP" ) ]]; then
    if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
      FAILED_APP="$(dirname "$DESTINATION")/.YoungRouter.failed.$$.app"
      rm -rf "$FAILED_APP"
      mv "$DESTINATION" "$FAILED_APP" || restore_failed=1
    fi
    if (( restore_failed == 0 )); then
      mv "$PREVIOUS_APP" "$DESTINATION" || restore_failed=1
    fi
  fi
  return "$restore_failed"
}

if [[ -z "$DEVELOPER_DIR" && -x /Applications/Xcode-beta.app/Contents/Developer/usr/bin/xcodebuild ]]; then
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
fi
if [[ -n "$DEVELOPER_DIR" ]]; then
  [[ -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]] || {
    echo "Invalid DEVELOPER_DIR: $DEVELOPER_DIR" >&2
    exit 3
  }
  export DEVELOPER_DIR
elif ! xcodebuild -version >/dev/null 2>&1; then
  echo "A complete Xcode installation is required to build the React Native macOS host." >&2
  exit 3
fi

command -v pnpm >/dev/null 2>&1 || {
  echo "pnpm is required to build the React Native macOS host." >&2
  exit 1
}

# Every entry point reaches this script, and not all of them install the
# workspace themselves: scripts/package-release.sh calls it directly. Install
# here so a caller only has to have the checkout, and let pnpm itself decide
# when the store already satisfies the lockfile.
pnpm install --frozen-lockfile

if [[ "${YOUNG_ROUTER_LITELLM_VERSION_UPDATED:-}" != "1" ]]; then
  "$PROJECT_ROOT/scripts/update-litellm.sh"
  export YOUNG_ROUTER_LITELLM_VERSION_UPDATED=1
fi

# The installed app already carries the exact portable Python runtime a repeat
# local build needs. Reuse it only when no caller supplied another runtime, its
# pinned LiteLLM version matches this checkout, and the portable launchers are
# present. The macOS host, project Python sources, smoke tests, signing,
# replacement, and readiness checks still run on every build.
if [[ -z "$RUNTIME_SOURCE" ]]; then
  INSTALLED_RUNTIME="$DESTINATION/Contents/Resources/Core/runtime"
  if [[ -x "$INSTALLED_RUNTIME/python/bin/python3.12" \
    && -x "$INSTALLED_RUNTIME/bin/python" \
    && -x "$INSTALLED_RUNTIME/bin/litellm" \
    && -f "$INSTALLED_RUNTIME/LITELLM_VERSION" \
    && "$(tr -d '[:space:]' < "$INSTALLED_RUNTIME/LITELLM_VERSION")" == "$(tr -d '[:space:]' < "$ROOT/LITELLM_VERSION")" ]]; then
    export YOUNG_ROUTER_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"
    RUNTIME_SOURCE="$INSTALLED_RUNTIME"
    printf 'Reusing installed Core runtime: %s\n' "$INSTALLED_RUNTIME"
  fi
fi

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "$NODE_MAJOR" -lt 22 ]]; then
  echo "Node.js 22 or later is required to build the React Native 0.85 macOS host." >&2
  exit 1
fi

PI_WEB_ACCESS_UPDATE_COMMAND=()
if [[ -n "${LITELLM_PI_WEB_ACCESS_PYTHON:-}" ]]; then
  command -v "$LITELLM_PI_WEB_ACCESS_PYTHON" >/dev/null 2>&1 || {
    echo "Configured LITELLM_PI_WEB_ACCESS_PYTHON was not found." >&2
    exit 1
  }
  PI_WEB_ACCESS_UPDATE_COMMAND=("$LITELLM_PI_WEB_ACCESS_PYTHON")
elif [[ -n "$UV_BIN" && -x "$UV_BIN" ]]; then
  PI_WEB_ACCESS_UPDATE_COMMAND=("$UV_BIN" run --no-project --python 3.12)
elif command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  PI_WEB_ACCESS_UPDATE_COMMAND=(python3)
else
  echo "Python 3.11+ (or uv) is required to stage pi-web-access for the macOS Core runtime." >&2
  exit 1
fi
PI_WEB_ACCESS_PACKAGE_WORK="$STAGING_CACHE/pi-web-access"
PI_WEB_ACCESS_NODE_WORK="$RUNTIME_WORK/node"
mkdir -p "$STAGING_CACHE"
# The Node runtime this app ships is a 108 MB download and about half the
# staging wall clock, and a repeat local build already has an identical copy:
# the installed bundle carries the very runtime the package requires.  Reusing
# it keeps the mandatory upstream re-checks below unchanged — only the Node
# binary avoids a second trip over the network.  A caller that supplied another
# runtime, or a build with no installed app, still downloads it.
NODE_SOURCE_ARGS=()
if [[ -n "$RUNTIME_SOURCE" && -x "$RUNTIME_SOURCE/bin/node" ]]; then
  NODE_SOURCE_ARGS=(--node-source "$RUNTIME_SOURCE/bin/node")
  printf 'Reusing installed Node runtime: %s\n' "$RUNTIME_SOURCE/bin/node"
fi
STAGING_PIDS=()
# `set -u` treats an empty array expansion as unbound on the bash 3.2 that
# macOS ships, so the argument is added only when it exists.
PI_WEB_ACCESS_NODE_ARGS=("$PROJECT_ROOT/scripts/update_pi_web_access.py"
  --output "$PI_WEB_ACCESS_PACKAGE_WORK"
  --node-output "$PI_WEB_ACCESS_NODE_WORK")
if [[ ${#NODE_SOURCE_ARGS[@]} -gt 0 ]]; then
  PI_WEB_ACCESS_NODE_ARGS+=("${NODE_SOURCE_ARGS[@]}")
fi
"${PI_WEB_ACCESS_UPDATE_COMMAND[@]}" "${PI_WEB_ACCESS_NODE_ARGS[@]}" &
STAGING_PIDS+=("$!")
# The authenticity deep test runs the latest upstream Veridrop, so every
# artifact build re-checks its default branch instead of packaging a stale
# copy of the scan program.
# WorkBuddy access is driven through the published third-party package, so
# every artifact build re-resolves its latest release instead of shipping a
# stale protocol copy.
# The vision fallback routes through the staged upstream package rather than a
# copy of its chain, so it joins the same re-check: every artifact build
# resolves the current release instead of shipping a mirror of whichever
# version was reviewed last.
#
# These integrations reach four unrelated upstreams and none of them reads
# another's output, so they resolve together instead of one after another. Run
# serially the package installs cost about a minute of nearly all network wait
# that a second process would spend idle; together they cost as long as the
# slowest one. Every script still runs on every build, so the mandatory
# re-check against upstream is unchanged — only its wall clock is.
#
# Nothing here is read until the Core is assembled below — the Xcode project
# does not reference the Python Core or a staged adapter at all — so the whole
# group overlaps the native build instead of preceding it, and the wait moves
# down to the assembly that actually consumes it.
WORKBUDDY_CONNECT_WORK="$STAGING_CACHE/workbuddy-connect"
VERIDROP_WORK="$STAGING_CACHE/veridrop"
DSH_VISION_ROUTER_WORK="$STAGING_CACHE/dsh-vision-router"
# Each job is started in this shell, not inside a command substitution: a
# substitution runs in a subshell, so a job it launches is not a child of the
# shell that has to wait for it and its output would corrupt the captured pid.
"${PI_WEB_ACCESS_UPDATE_COMMAND[@]}" "$PROJECT_ROOT/scripts/update_workbuddy_connect.py" \
  --output "$WORKBUDDY_CONNECT_WORK" &
STAGING_PIDS+=("$!")
"${PI_WEB_ACCESS_UPDATE_COMMAND[@]}" "$PROJECT_ROOT/scripts/update_veridrop.py" \
  --output "$VERIDROP_WORK" &
STAGING_PIDS+=("$!")
"${PI_WEB_ACCESS_UPDATE_COMMAND[@]}" "$PROJECT_ROOT/scripts/update_dsh_vision_router.py" \
  --output "$DSH_VISION_ROUTER_WORK" &
STAGING_PIDS+=("$!")

export RCT_USE_RN_DEP=0
export RCT_USE_PREBUILT_RNCORE=0
export RCT_BUILD_HERMES_FROM_SOURCE=true
export RCT_HERMES_V1_ENABLED=1
node scripts/bootstrap-rnmacos-085.mjs
node scripts/verify-rnmacos-085.mjs --check-build-env

if [[ ! -d "$APP_ROOT/macos" ]]; then
  echo "React Native macOS host project is missing at rn/apps/macos/macos." >&2
  exit 2
fi

command -v pod >/dev/null 2>&1 || {
  echo "CocoaPods is required to build the React Native macOS host." >&2
  exit 3
}
for tool in rsync codesign strip file; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Missing required macOS bundle tool: $tool" >&2
    exit 3
  }
done
SWIFTC_BIN="$(xcrun --sdk macosx --find swiftc 2>/dev/null || true)"
[[ -n "$SWIFTC_BIN" && -x "$SWIFTC_BIN" ]] || {
  echo "Xcode swiftc is required to build the bundled Vision OCR helper." >&2
  exit 3
}
MACOS_SDK="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)"
[[ -n "$MACOS_SDK" && -d "$MACOS_SDK" ]] || {
  echo "Xcode macOS SDK is required to build the bundled Vision OCR helper." >&2
  exit 3
}
if [[ -z "$RUNTIME_SOURCE" && ( -z "$UV_BIN" || ! -x "$UV_BIN" ) ]]; then
  echo "uv is required to build the self-contained macOS Core runtime." >&2
  exit 3
fi
[[ -x /usr/libexec/PlistBuddy ]] || {
  echo "Missing required macOS bundle tool: /usr/libexec/PlistBuddy" >&2
  exit 3
}
# These checks only read the shared/macOS JS/TS workspace while CocoaPods
# prepares the native workspace. Windows codegen remains checked by its build
# and the cross-platform CI job.
if [[ -f node_modules/typescript/bin/tsc ]]; then
  pnpm run test &
  RN_TESTS_PID=$!
  pnpm run contract-check &
  RN_CONTRACT_PID=$!
  pnpm run code-editor:build &
  RN_EDITOR_PID=$!
  pnpm run typecheck:no-editor &
  RN_TYPECHECK_PID=$!
else
  pnpm run check:macos &
  STATIC_CHECKS_PID=$!
fi
wait_static_checks() {
  local failed=0 pid
  if [[ -n "${STATIC_CHECKS_PID:-}" ]]; then
    wait "$STATIC_CHECKS_PID" || failed=1
  else
    for pid in "$RN_TESTS_PID" "$RN_CONTRACT_PID" "$RN_EDITOR_PID" "$RN_TYPECHECK_PID"; do
      wait "$pid" || failed=1
    done
  fi
  return "$failed"
}
if [[ "${YOUNG_ROUTER_REFRESH_PODS:-0}" == "1" \
  || -n "${CI:-}" \
  || ! -d "$APP_ROOT/macos/Pods" \
  || ! -d "$APP_ROOT/macos/YoungRouter.xcworkspace" \
  || ! -f "$APP_ROOT/macos/Podfile.lock" ]]; then
  if ! pod install --project-directory="$APP_ROOT/macos"; then
    wait_static_checks || true
    exit 1
  fi
else
  printf '%s\n' "Reusing CocoaPods workspace (set YOUNG_ROUTER_REFRESH_PODS=1 after native dependency or codegen changes)."
fi
wait_static_checks

XCODE_DESTINATION=()
case "$ARCH" in
  arm64|x86_64) XCODE_DESTINATION=( -destination "platform=macOS,arch=$ARCH" ) ;;
  *) echo "Unsupported macOS build architecture: $ARCH" >&2; exit 3 ;;
esac
APP="$(xcodebuild -workspace "$APP_ROOT/macos/YoungRouter.xcworkspace" -scheme YoungRouter-macOS -configuration Release "${XCODE_DESTINATION[@]}" -showBuildSettings 2>/dev/null | awk -F ' = ' '/TARGET_BUILD_DIR = / { target = $2 } /FULL_PRODUCT_NAME = / { product = $2 } END { if (target && product) print target "/" product }')"
if [[ -z "$APP" ]]; then
  echo "Could not resolve the React Native macOS build product path." >&2
  exit 4
fi

RNMACOS_CLI="$RN_ROOT/vendor/react-native-macos-0.85/packages/react-native/cli.js"
(
  cd "$APP_ROOT"
  node "$RNMACOS_CLI" build-macos --project-path macos --mode Release
)

if [[ -z "$APP" || ! -d "$APP" ]]; then
  echo "React Native macOS build did not produce YoungRouter.app." >&2
  exit 4
fi

# The staging jobs have been running alongside the native build; this is where
# their output is first read, so this is where a failed one is reported. It is
# named as itself instead of surfacing later as an unrelated missing-file check.
STAGING_FAILED=0
for staging_pid in "${STAGING_PIDS[@]}"; do
  wait "$staging_pid" || STAGING_FAILED=1
done
if [[ "$STAGING_FAILED" == "1" ]]; then
  echo "Staging a third-party integration from its latest upstream release failed; no artifact was produced." >&2
  exit 1
fi

CORE="$APP/Contents/Resources/Core"
rm -rf "$CORE"
mkdir -p "$CORE"
cp "$PROJECT_ROOT/sitecustomize.py" "$CORE/sitecustomize.py"
rsync -a \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  "$PROJECT_ROOT/young_router/" "$CORE/young_router/"
copy_tree "$PI_WEB_ACCESS_PACKAGE_WORK" "$CORE/young_router/adapters/pi-web-access"
copy_tree "$VERIDROP_WORK" "$CORE/young_router/adapters/veridrop"
copy_tree "$WORKBUDDY_CONNECT_WORK" "$CORE/young_router/adapters/workbuddy-connect"
copy_tree "$DSH_VISION_ROUTER_WORK" "$CORE/young_router/adapters/dsh-vision-router"

if [[ -n "$RUNTIME_SOURCE" ]]; then
  if [[ ! -d "$RUNTIME_SOURCE/python" \
    || ! -d "$RUNTIME_SOURCE/site-packages" \
    || ! -x "$RUNTIME_SOURCE/bin/python" \
    || ! -x "$RUNTIME_SOURCE/bin/litellm" \
    || ! -x "$RUNTIME_SOURCE/python/bin/python3.12" ]]; then
    echo "YOUNG_ROUTER_CORE_RUNTIME_SOURCE must use the portable release-runtime layout, not a virtualenv." >&2
    exit 5
  fi
  copy_tree "$RUNTIME_SOURCE" "$CORE/runtime"
else
  case "$ARCH" in
    arm64) UV_RUNTIME="cpython-3.12-macos-aarch64-none" ;;
    x86_64) UV_RUNTIME="cpython-3.12-macos-x86_64-none" ;;
    *)
      echo "Unsupported macOS Core runtime architecture: $ARCH" >&2
      exit 5
      ;;
  esac
  mkdir -p "$RUNTIME_WORK/python-installs" "$CORE/runtime/bin" "$CORE/runtime/site-packages"
  UV_PYTHON_INSTALL_DIR="$RUNTIME_WORK/python-installs" \
    "$UV_BIN" python install "$UV_RUNTIME" --no-bin >/dev/null
  PYTHON_SOURCE="$(printf '%s\n' "$RUNTIME_WORK"/python-installs/cpython-3.12.*-macos-*64-none | head -n 1)"
  if [[ ! -x "$PYTHON_SOURCE/bin/python3.12" ]]; then
    echo "uv did not install the expected standalone macOS Python 3.12 runtime." >&2
    exit 5
  fi
  mv "$PYTHON_SOURCE" "$CORE/runtime/python"
  LITELLM_VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/LITELLM_VERSION")"
  "$UV_BIN" pip install \
    --python "$CORE/runtime/python/bin/python3.12" \
    --target "$CORE/runtime/site-packages" \
    "litellm[proxy]==$LITELLM_VERSION" \
    "fastapi==0.140.3" \
    Pillow \
    PyYAML >/dev/null
  cp "$PROJECT_ROOT/scripts/runtime/python-wrapper.sh" "$CORE/runtime/bin/python"
  cp "$PROJECT_ROOT/scripts/runtime/litellm-wrapper.sh" "$CORE/runtime/bin/litellm"
  cp "$PROJECT_ROOT/LITELLM_VERSION" "$CORE/runtime/LITELLM_VERSION"
  chmod 0755 "$CORE/runtime/bin/python" "$CORE/runtime/bin/litellm"
fi
cp "$PROJECT_ROOT/LITELLM_VERSION" "$CORE/runtime/LITELLM_VERSION"
mkdir -p "$CORE/runtime/bin"
copy_tree "$PI_WEB_ACCESS_NODE_WORK" "$CORE/runtime/bin"

# The staged scan program is imported into the Core's own interpreter, so what
# it imports and the bundled runtime does not already carry is added to that
# runtime here - and only that: a dependency the runtime already has is never
# upgraded under the release's own pins.
"$CORE/runtime/python/bin/python3.12" "$PROJECT_ROOT/scripts/update_veridrop.py" \
  --output "$VERIDROP_WORK" \
  --install-deps "$CORE/runtime/site-packages" \
  --python "$CORE/runtime/python/bin/python3.12" || {
  echo "Could not complete the bundled Veridrop dependencies." >&2
  exit 5
}
# The dependency pass operates on the staged tree and may replace its package
# payload while resolving missing wheels; restore the upstream license beside
# the copied package before the bundle checks inspect the assembled Core.
cp -p "$VERIDROP_WORK/LICENSE" "$CORE/young_router/adapters/veridrop/LICENSE"

# Published JavaScript packages include declaration files, source maps, tests,
# docs and demo media for npm consumers. The shipped workers execute only the
# package runtime; prune the copied Core before smoke tests and signing.
"$CORE/runtime/python/bin/python3.12" "$PROJECT_ROOT/scripts/prune-macos-core.py" "$CORE"

VISION_HELPER_SOURCE="$APP_ROOT/src/native/macos/VisionOCR.swift"
VISION_HELPER="$CORE/bin/vision_ocr"
mkdir -p "$CORE/bin"
"$SWIFTC_BIN" \
  -sdk "$MACOS_SDK" \
  -target "$ARCH-apple-macosx14.0" \
  -file-prefix-map "$PROJECT_ROOT=." \
  -debug-prefix-map "$PROJECT_ROOT=." \
  "$VISION_HELPER_SOURCE" \
  -o "$VISION_HELPER" \
  -framework Vision \
  -framework ImageIO \
  -framework CoreGraphics \
  -framework Foundation
[[ -x "$VISION_HELPER" ]] || {
  echo "The bundled Vision OCR helper is missing or not executable." >&2
  exit 5
}
if "$VISION_HELPER" >/dev/null 2>&1; then
  echo "The bundled Vision OCR helper accepted an invalid empty invocation." >&2
  exit 5
else
  VISION_HELPER_STATUS=$?
  [[ "$VISION_HELPER_STATUS" -eq 64 ]] || {
    echo "The bundled Vision OCR helper could not launch." >&2
    exit 5
  }
fi

[[ -f "$CORE/young_router/core/__main__.py" ]] || {
  echo "Bundled Core launcher is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/adapters/pi-web-access/index.ts" ]] || {
  echo "The bundled pi-web-access package is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/adapters/veridrop/src/relay_detector/cli.py" ]] || {
  echo "The bundled Veridrop scan program is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/adapters/veridrop/LICENSE" ]] || {
  echo "The bundled Veridrop license text is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/adapters/workbuddy_stream.mjs" ]] || {
  echo "The macOS build output does not contain young_router/adapters/workbuddy_stream.mjs." >&2
  exit 1
}
[[ -f "$CORE/young_router/adapters/workbuddy-connect/lib/index.js" ]] || {
  echo "The bundled dsh-workbuddy-connect package is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/adapters/dsh-vision-router/lib/core-primitives.js" ]] || {
  echo "The bundled dsh-vision-router package is missing." >&2
  exit 5
}
[[ -f "$CORE/young_router/config/api.py" ]] || {
  echo "Bundled Core dependencies are incomplete." >&2
  exit 5
}
[[ -x "$CORE/runtime/bin/python" ]] || {
  echo "A self-contained Core runtime is required. Set YOUNG_ROUTER_CORE_RUNTIME_SOURCE." >&2
  exit 5
}
[[ -x "$CORE/runtime/bin/litellm" ]] || {
  echo "The bundled Core runtime does not contain the LiteLLM executable." >&2
  exit 5
}
[[ -x "$CORE/runtime/bin/node" ]] || {
  echo "The bundled Node.js runtime is missing." >&2
  exit 5
}
PI_WEB_ACCESS_SMOKE_CONFIG="$RUNTIME_WORK/pi-web-access-smoke-config"
if ! printf '' | "$CORE/runtime/bin/node" \
  "$CORE/young_router/adapters/pi_web_access_worker.mjs" \
  --entry "$CORE/young_router/adapters/pi-web-access/index.ts" \
  --config-dir "$PI_WEB_ACCESS_SMOKE_CONFIG"; then
  echo "The bundled pi-web-access worker could not load its staged SDK." >&2
  exit 5
fi
# The vision fallback is entered only after a model has already rejected the
# image, so a chain that cannot be built would cost the answer rather than the
# request. The bundle therefore asks the staged package for its chain once: a
# worker that cannot import it is a build failure, not a runtime degrade.
VISION_SMOKE_OUTPUT="$(printf '%s\n' '{"config":{"backend":"auto","freeFallback":true}}' \
  | "$CORE/runtime/bin/node" "$CORE/young_router/adapters/dsh_vision_worker.mjs" 2>/dev/null || true)"
if ! printf '%s' "$VISION_SMOKE_OUTPUT" | grep -q '"source":"upstream"' \
  || ! printf '%s' "$VISION_SMOKE_OUTPUT" | grep -q '"model":'; then
  echo "The bundled dsh-vision-router worker could not load its staged package." >&2
  exit 5
fi
# WorkBuddy ships the desktop credential format and both model catalogs, so a
# release that renames one of its published exports would break the provider at
# request time rather than at build time. The bundle proves the staged package
# still offers the surface the worker imports.
if ! "$CORE/runtime/bin/node" --input-type=module -e "
import(process.argv[1]).then((m) => {
  const required = ['WorkBuddyCredentialStore', 'WorkBuddyUpstreamClient', 'WorkBuddyCatalog', 'createWorkBuddyShim', 'WORKBUDDY_VARIANTS']
  const missing = required.filter((name) => m[name] === undefined)
  if (missing.length > 0) { process.stderr.write('missing: ' + missing.join(', ') + '\n'); process.exit(1) }
}).catch((e) => { process.stderr.write(String((e && e.message) || e) + '\n'); process.exit(1) })
" "$CORE/young_router/adapters/workbuddy-connect/lib/index.js"; then
  echo "The bundled dsh-workbuddy-connect package no longer exports the surface the worker uses." >&2
  exit 5
fi
# The staged program is imported into the Core's own interpreter, so the bundle
# proves the entry points the adapter drives - and the dependency set the
# runtime was completed with - before the app is installed: a module that cannot
# be imported here would otherwise surface as a failed deep test long after the
# build.  The check names the same objects the adapter imports, so a release
# that renames one fails here rather than at probe time.
if ! VERIDROP_SMOKE_OUTPUT="$(PYTHONDONTWRITEBYTECODE=1 "$CORE/runtime/bin/python" -c "
import sys
sys.path.insert(0, '$CORE/young_router/adapters/veridrop/src')
from relay_detector.models import DetectionReport, ExecutionConfig, Mode, Protocol, mask_api_key
from relay_detector.scorer import compute_total, effective_verdict, fatal_run_error, summary_text
import relay_detector.protocols.anthropic
import relay_detector.protocols.openai
import relay_detector.protocols.gemini
import relay_detector.protocols.anthropic as a, relay_detector.protocols.openai as o, relay_detector.protocols.gemini as g
for proto in (a, o, g):
    for name in ('build_detectors', 'build_runner', 'make_client'):
        assert hasattr(proto, name), (proto.__name__, name)
assert ExecutionConfig.for_mode(Mode.QUICK, max_concurrent=3) is not None
print('young-router-veridrop-import-ok')
" 2>&1)"; then
  echo "The bundled Veridrop program could not be imported: $VERIDROP_SMOKE_OUTPUT" >&2
  exit 5
fi
case "$VERIDROP_SMOKE_OUTPUT" in
  *young-router-veridrop-import-ok*) ;;
  *)
    echo "The bundled Veridrop import smoke test printed no confirmation." >&2
    exit 5
    ;;
esac
[[ "$(tr -d '[:space:]' < "$CORE/runtime/LITELLM_VERSION")" == "$(tr -d '[:space:]' < "$PROJECT_ROOT/LITELLM_VERSION")" ]] || {
  echo "The bundled Core runtime does not contain the pinned LiteLLM release lock." >&2
  exit 5
}

# Native wheels and the bundled language runtimes carry local symbol tables
# that are not needed at runtime. Strip them before the final app signature.
while IFS= read -r -d '' binary; do
  case "$(file -b "$binary")" in
    Mach-O*)
      strip -x -S "$binary" >/dev/null 2>&1
      # strip rewrites the Mach-O pages and invalidates the wheel's embedded
      # signature.  Sign the rewritten extension itself; signing only the
      # outer app does not make macOS accept these pages when a worker imports
      # the module later.
      codesign --force --sign - "$binary" >/dev/null
      ;;
  esac
done < <(
  find "$CORE/runtime/site-packages" -type f \( -name '*.so' -o -name '*.dylib' \) -print0
  printf '%s\0' "$CORE/runtime/bin/node" "$CORE/runtime/python/bin/python3.12" "$CORE/runtime/python/lib/libpython3.12.dylib"
)

# A full-tree compileall pass adds roughly 100 MB of caches, most of which
# this app never imports. Remove stale caches before the real startup smoke
# imports populate only the modules on the active path.
find "$CORE/young_router" "$CORE/runtime/site-packages" \
  \( -type d -name __pycache__ -prune -exec rm -rf {} + \) \
  -o \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \)

# Import the exact proxy startup path as a packaging smoke test. This also
# verifies the callback fallback in the same environment used by the child.
# The package already carries LiteLLM's fallback cost map, so this must not
# wait on a remote catalog refresh during a local build.
export LITELLM_LOCAL_MODEL_COST_MAP=true
PYTHONDONTWRITEBYTECODE=0 \
PYTHONPATH="$CORE:$CORE/runtime/site-packages" \
YOUNG_ROUTER_PROXY_PROCESS=1 \
LITELLM_TEMPLATE_ROOT="$CORE" \
  "$CORE/runtime/python/bin/python3.12" -c '
from litellm import run_server
from litellm.proxy.proxy_server import app
from litellm.proxy.types_utils.utils import get_instance_fn
from gunicorn.app.base import BaseApplication
from uvicorn.workers import UvicornWorker

callback = get_instance_fn(
    "young_router.callbacks.image_generation_routing_hook",
    config_file_path="runtime/config.yaml",
)
assert app is not None
assert callback.__class__.__name__ == "YoungRouterHook"
assert run_server is not None and BaseApplication is not None and UvicornWorker is not None
'
[[ -f "$CORE/runtime/site-packages/litellm/__pycache__/__init__.cpython-312.pyc" ]] || {
  echo "The bundled Core startup bytecode could not be generated." >&2
  exit 5
}

PYTHONDONTWRITEBYTECODE=0 PYTHONPATH="$CORE" "$CORE/runtime/bin/python" -c 'import litellm.proxy.proxy_server, young_router.core, young_router.core.__main__, young_router.proxy.macos_proxy, young_router.core.codex_config, young_router.config, young_router.core.configuration_package, young_router.core.external_provider_import, young_router.webdav.core'
PYTHONDONTWRITEBYTECODE=1 "$CORE/runtime/bin/litellm" --help >/dev/null
# Prove the bundle still works from somewhere other than where it was built:
# that is what the installed app does, and an absolute path baked into a
# wrapper or a .pth would only show up here.  The relocation is a rename within
# the build product's own volume, so it costs nothing and, unlike copying a
# gigabyte aside, leaves the bytecode just generated above in place: a copied
# tree re-imports cold and pays the interpreter's full start-up again.
PORTABLE_SMOKE="$(dirname "$CORE")/.young-router-portable-smoke"
relocate_core() {
  mv "$CORE" "$PORTABLE_SMOKE" 2>/dev/null \
    || { copy_tree "$CORE" "$PORTABLE_SMOKE"; rm -rf "$CORE"; }
}
restore_core() {
  [[ -d "$PORTABLE_SMOKE" ]] || return 0
  mv "$PORTABLE_SMOKE" "$CORE" 2>/dev/null \
    || { copy_tree "$PORTABLE_SMOKE" "$CORE"; rm -rf "$PORTABLE_SMOKE"; }
}
# Restore however this ends, so a failed import cannot leave the built app
# without its Core.
trap 'restore_core; cleanup' EXIT
relocate_core
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PORTABLE_SMOKE" "$PORTABLE_SMOKE/runtime/bin/python" -c 'import litellm.proxy.proxy_server, young_router.core'
PYTHONDONTWRITEBYTECODE=1 YOUNG_ROUTER_PROXY_PROCESS=1 PYTHONPATH="$PORTABLE_SMOKE" \
  "$PORTABLE_SMOKE/runtime/bin/python" -c \
  'from litellm.proxy.types_utils.utils import get_instance_fn; callback = get_instance_fn("young_router.callbacks.image_generation_routing_hook", config_file_path="runtime/config.yaml"); assert callback.__class__.__name__ == "YoungRouterHook"'
PYTHONDONTWRITEBYTECODE=1 "$PORTABLE_SMOKE/runtime/bin/litellm" --help >/dev/null
restore_core
trap cleanup EXIT

# The smoke imports above deliberately exercise bytecode generation, but
# __pycache__ entries are not part of AppKit's signed resource contract.
# Remove them after validation so strict codesign verification cannot see
# unsealed cache files in the finished app bundle.
find "$CORE" \
  \( -type d -name __pycache__ -prune -exec rm -rf {} + \) \
  -o \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \)

VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
BUILD_NUMBER="$(tr -d '[:space:]' < "$PROJECT_ROOT/BUILD_NUMBER")"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist" >/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$APP/Contents/Info.plist" >/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $BUILD_NUMBER" "$APP/Contents/Info.plist" >/dev/null
if [[ -n "${YOUNG_ROUTER_MACOS_BUNDLE_IDENTIFIER:-}" ]]; then
  echo "YOUNG_ROUTER_MACOS_BUNDLE_IDENTIFIER is unsupported: every Young Router build must use the production instance identity." >&2
  exit 6
fi
if [[ -n "${YOUNG_ROUTER_MACOS_DISPLAY_NAME:-}" \
  || -n "${YOUNG_ROUTER_MACOS_ROUTE_SCHEME:-}" \
  || -n "${YOUNG_ROUTER_MACOS_PREVIEW_PROFILE_ROOT:-}" \
  || -n "${YOUNG_ROUTER_MACOS_PREVIEW_PORT:-}" ]]; then
  PREVIEW_BUNDLE_IDENTIFIER="young.router.app"
  PREVIEW_DISPLAY_NAME="${YOUNG_ROUTER_MACOS_DISPLAY_NAME:-Young Router Preview}"
  PREVIEW_ROUTE_SCHEME="${YOUNG_ROUTER_MACOS_ROUTE_SCHEME:-young-router-preview}"
  PREVIEW_PROFILE_ROOT="${YOUNG_ROUTER_MACOS_PREVIEW_PROFILE_ROOT:-}"
  PREVIEW_PORT="${YOUNG_ROUTER_MACOS_PREVIEW_PORT:-}"
  [[ "$PREVIEW_BUNDLE_IDENTIFIER" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    && [[ "$PREVIEW_DISPLAY_NAME" != *$'\n'* ]] \
    && [[ "$PREVIEW_ROUTE_SCHEME" =~ ^[A-Za-z][A-Za-z0-9.-]*$ ]] || {
      echo "Invalid macOS preview bundle metadata." >&2
      exit 6
    }
  if [[ -n "$PREVIEW_PROFILE_ROOT" ]]; then
    [[ "$PREVIEW_PROFILE_ROOT" = /* ]] \
      && [[ "$PREVIEW_PROFILE_ROOT" != *$'\n'* ]] \
      && [[ "$PREVIEW_PORT" =~ ^[0-9]+$ ]] \
      && (( PREVIEW_PORT >= 1 && PREVIEW_PORT <= 65535 )) || {
        echo "A preview profile root requires an absolute path and valid port." >&2
        exit 6
      }
  fi
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $PREVIEW_BUNDLE_IDENTIFIER" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $PREVIEW_DISPLAY_NAME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName $PREVIEW_DISPLAY_NAME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :YoungRouterRouteScheme $PREVIEW_ROUTE_SCHEME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLName $PREVIEW_BUNDLE_IDENTIFIER.routes" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLSchemes:0 $PREVIEW_ROUTE_SCHEME" "$APP/Contents/Info.plist"
  if [[ -n "$PREVIEW_PROFILE_ROOT" ]]; then
    /usr/libexec/PlistBuddy -c "Delete :YoungRouterPreviewProfileRoot" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :YoungRouterPreviewProfileRoot string $PREVIEW_PROFILE_ROOT" "$APP/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Delete :YoungRouterPreviewPort" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :YoungRouterPreviewPort string $PREVIEW_PORT" "$APP/Contents/Info.plist"
  fi
fi
codesign --force --deep --sign - "$APP" >/dev/null
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ -n "${YOUNG_ROUTER_MACOS_OUTPUT:-}" ]]; then
  OUTPUT="$YOUNG_ROUTER_MACOS_OUTPUT"
  [[ "$OUTPUT" = /* ]] || OUTPUT="$RN_ROOT/$OUTPUT"
  [[ "$OUTPUT" == *.app ]] || {
    echo "YOUNG_ROUTER_MACOS_OUTPUT must name an .app bundle." >&2
    exit 6
  }
  mkdir -p "$(dirname "$OUTPUT")"
  OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd -P)/$(basename "$OUTPUT")"
  case "$OUTPUT" in
    /|"$HOME"|"$PROJECT_ROOT"|"$RN_ROOT")
      echo "Refusing unsafe YOUNG_ROUTER_MACOS_OUTPUT: $OUTPUT" >&2
      exit 6
      ;;
  esac
  # A caller named this path, so the verified bundle has to end up there.
  # The product and the destination are normally on one volume, where a rename
  # moves a gigabyte in constant time and preserves every nested signature and
  # extended attribute exactly as a copy does - nothing about the files
  # changes.  The copy is the fallback for a destination on another volume.
  OUT_DIR="$(dirname "$OUTPUT")"
  STAGED_OUTPUT="$OUT_DIR/.YoungRouter.$$.app"
  rm -rf "$STAGED_OUTPUT"
  if [[ "$(stat -f %d "$APP" 2>/dev/null || echo a)" == "$(stat -f %d "$OUT_DIR" 2>/dev/null || echo b)" ]] \
    && mv "$APP" "$STAGED_OUTPUT" 2>/dev/null; then
    :
  else
    copy_tree "$APP" "$STAGED_OUTPUT"
  fi
  rm -rf "$OUTPUT"
  mv "$STAGED_OUTPUT" "$OUTPUT"
  APP="$OUTPUT"
fi

# Xcode can reuse an incrementally built bundle while preserving the app
# directory's old modification time. Finder keys its displayed icon cache to
# that bundle metadata, so mark every finished artifact as newly built even
# when the version and output path stay unchanged.
touch "$APP"

# ---- the install half ----
#
# Everything below runs only when this build owns the installed app. A
# build-only caller named an artifact path and has already received it.
if [[ "$BUILD_ONLY" == "1" ]]; then
  printf '%s\n' "$APP"
  exit 0
fi

STAGED_APP="$APP"

case "$(uname -s)" in
  Darwin) ;;
  *)
    echo "Young Router installation is supported only on macOS." >&2
    exit 1
    ;;
esac

[[ -x "$LSREGISTER" ]] || {
  echo "LaunchServices registration tool is missing: $LSREGISTER" >&2
  exit 1
}

[[ "$DESTINATION" = /* && "$DESTINATION" == *.app ]] || {
  echo "YOUNG_ROUTER_INSTALL_APP must be an absolute .app path." >&2
  exit 1
}
DESTINATION="$(cd "$(dirname "$DESTINATION")" && pwd -P)/$(basename "$DESTINATION")"
[[ "$DESTINATION" == "/Applications/Young Router.app" ]] || {
  echo "Refusing an installation destination other than /Applications/Young Router.app." >&2
  exit 1
}

# The bytes here are the ones just verified in the staging path, moved rather
# than rewritten, so this pass confirms the moved bundle kept its signatures
# instead of re-deriving them from an unchanged tree.
#
# The staging directory and the install destination are normally on the same
# volume, and a same-volume rename moves the verified bundle in constant time
# where a byte copy of about a gigabyte costs several seconds of the build.
# Renaming preserves every nested signature and extended attribute exactly as
# `ditto` does, because nothing about the files themselves changes; the copy
# remains the fallback for a cross-volume destination, where rename is
# impossible.
stage_verified_bundle() {
  local destination_device source_device
  source_device="$(stat -f %d "$STAGED_APP" 2>/dev/null || echo "")"
  destination_device="$(stat -f %d "$(dirname "$INSTALL_STAGE")" 2>/dev/null || echo "")"
  if [[ -n "$source_device" && "$source_device" == "$destination_device" ]] \
    && mv "$STAGED_APP" "$INSTALL_STAGE" 2>/dev/null; then
    return 0
  fi
  copy_bundle "$STAGED_APP" "$INSTALL_STAGE"
}

INSTALL_STAGE="$(dirname "$DESTINATION")/.YoungRouter.install.$$.app"
PREVIOUS_APP="$(dirname "$DESTINATION")/.YoungRouter.previous.$$.app"
rm -rf "$INSTALL_STAGE" "$PREVIOUS_APP"
stage_verified_bundle
codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"

# Keep the installed service available while the verified replacement takes
# its place. Running processes retain the old executable after this rename;
# stopping them only after the new bundle is in place shortens the listener
# outage to the stop/start interval.
OLD_PIDS="$(bundle_processes)"
printf '%s\n' "Young Router: replacing bundle and stopping previous process"
# Arm the EXIT relaunch before moving the live bundle, so an interrupted swap
# cannot leave the installed path without an app to launch.
RESTART_ARMED=1
if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
  mv "$DESTINATION" "$PREVIOUS_APP"
fi
if ! mv "$INSTALL_STAGE" "$DESTINATION"; then
  [[ ! -e "$PREVIOUS_APP" && ! -L "$PREVIOUS_APP" ]] || mv "$PREVIOUS_APP" "$DESTINATION"
  exit 1
fi
INSTALL_STAGE=""
if ! refresh_installed_app_icon; then
  echo "The new Young Router app icon could not be refreshed; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous Young Router bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi
if ! stop_installed_app "$OLD_PIDS"; then
  echo "The old Young Router app did not stop; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous Young Router bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi
# A Core may have forked its proxy just after the pre-swap snapshot. Capture
# any remaining process still executing the retired bundle before launching
# the replacement, so no old listener can survive into the new lifecycle.
REMAINING_OLD_PIDS="$(bundle_processes)"
if [[ -n "$REMAINING_OLD_PIDS" ]] && ! stop_installed_app "$REMAINING_OLD_PIDS"; then
  echo "The old Young Router proxy did not stop; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous Young Router bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi
printf '%s\n' "Young Router: starting replacement and checking Core/proxy health"
if ! start_installed_app "$OLD_PIDS"; then
  echo "The new Young Router app failed readiness checks; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous Young Router bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi

RESTART_ARMED=0
rm -rf "$PREVIOUS_APP"
PREVIOUS_APP=""
INSTALL_COMPLETE=1

printf '%s\n' "Young Router: running ($DESTINATION)"
