#!/usr/bin/env bash
#
# Carry a change into the running app without paying for a build.
#
# `build-and-install-macos.sh` is the deploy: it resolves four upstream
# releases, re-verifies the pinned LiteLLM release, re-runs the Core smokes,
# rebuilds the native host when a native file moved, re-signs the bundle, and
# swaps `/Applications/Young Router.app`.  A repeat deploy that changed nothing
# at all still costs about 104 s, and almost none of it is about the change
# under test.
#
# What a change needs is for the running processes to load it.  Both halves of
# this app read their sources from the installed bundle when they start -- Core
# and the proxy import `young_router/`, the host loads `main.jsbundle` -- so
# writing the changed tree into that bundle and restarting is enough.  Measured
# here: 10 to 16 s for an edit.
#
#   scripts/dev-loop-macos.sh            sync whatever changed, restart
#   scripts/dev-loop-macos.sh --js       rebundle and install the UI only
#   scripts/dev-loop-macos.sh --python   sync the Python tree only
#   scripts/dev-loop-macos.sh --metro    set up Metro dev server for sub-second
#                                        Fast Refresh (one-time, keeps running)
#   scripts/dev-loop-macos.sh --proxy    run an isolated proxy on a scratch
#                                        port, leaving the app alone
#   scripts/dev-loop-macos.sh --check    report which half is stale, act not
#
# `--metro` is the fastest JS iteration: it sets the installed app to load from
# Metro's dev server, starts Metro, and keeps it running. JS edits land in under
# one second with Fast Refresh and state preserved. Run it once, leave Metro
# running, and edit. Stop Metro (Ctrl-C) or clear the override
# (`defaults delete young.router.app MetroServerURL && restart-app.sh`) to go
# back to the bundled JS.
#
# `--proxy` is the data-plane loop: it starts a second proxy from the checkout's
# `young_router/` against a scratch runtime root and a port of its own, so a
# streaming or routing change is exercised with `curl` in a few seconds without
# touching the app, its window, or its clients.
#
# Not signing is deliberate: the installed bundle is an adhoc build carrying
# `com.apple.provenance` from the machine that built it, and Gatekeeper does not
# re-assess that on launch.  Verified here: a modified tree fails
# `codesign --verify --strict` and still starts and serves.  Every deploy
# re-signs the whole bundle, so the shipped artifact is unaffected.
#
# A change to a native source cannot be carried this way -- it compiles into the
# host executable -- so this refuses it by name instead of installing a
# JS/Python half that no window could show.  `tests/test_rn_release.py` pins
# that refusal and the other boundaries stated here.
#
# Neither mode replaces the deploy: run `build-and-install-macos.sh` once the
# change is settled, and read its own output for what it shipped.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RN_ROOT="$ROOT/rn"
APP="${YOUNG_ROUTER_INSTALL_APP:-/Applications/Young Router.app}"
CORE="$APP/Contents/Resources/Core"
RESOURCES="$APP/Contents/Resources"
RESTART_APP="$ROOT/scripts/restart-app.sh"
MODE="auto"

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

while (($# > 0)); do
  case "$1" in
    --js) MODE="js" ;;
    --python) MODE="python" ;;
    --metro) MODE="metro" ;;
    --proxy) MODE="proxy" ;;
    --check) MODE="check" ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "dev-loop-macos: unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
  shift
done

# The staged adapters under `adapters/*/` are published packages that only a
# build produces; this checkout holds none of them.  One list states that for
# both the staleness comparison and the sync, because two lists that must agree
# would drift into a tree that reads as permanently stale -- or, with
# `--delete`, into deleting working adapters mid-sync.
CORE_SYNC_EXCLUDES=(
  --exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo'
  --exclude 'veridrop' --exclude 'connect-workbuddy'
  --exclude 'dsh-vision-router' --exclude 'our-free-model'
)

# --- Metro dev server for sub-second Fast Refresh ------------------------------
#
# This mode sets the installed app to load from Metro instead of the packaged
# bundle, starts Metro in this terminal, and keeps it running. JS edits land in
# under one second with Fast Refresh and state preserved. Run it once, leave
# Metro running, and edit. Stop Metro (Ctrl-C) or clear the override to go back.
if [[ "$MODE" == "metro" ]]; then
  command -v node >/dev/null 2>&1 || {
    echo "dev-loop-macos: node is required to run Metro" >&2
    exit 3
  }
  METRO_PORT="${YOUNG_ROUTER_METRO_PORT:-8081}"
  METRO_URL="http://localhost:${METRO_PORT}/index.bundle?platform=macos&dev=true&minify=false"

  echo "dev-loop-macos: setting MetroServerURL to $METRO_URL"
  defaults write young.router.app MetroServerURL "$METRO_URL"

  # Restart the app so it picks up the override and connects to Metro
  if ! "$RESTART_APP" >/dev/null 2>&1; then
    echo "dev-loop-macos: the app did not restart cleanly" >&2
    exit 1
  fi

  echo "dev-loop-macos: Metro will serve on port $METRO_PORT"
  echo "  - JS edits land in under 1 second with Fast Refresh"
  echo "  - Stop Metro (Ctrl-C) or clear the override to go back:"
  echo "      defaults delete young.router.app MetroServerURL && scripts/restart-app.sh"
  echo ""

  # Start Metro and keep it in the foreground
  cd "$RN_ROOT/apps/macos"
  exec npx react-native start --port "$METRO_PORT"
fi

# --- the isolated proxy loop ------------------------------------------------
#
# This mode needs the bundled runtime and nothing else from the install, so it
# is handled before any bundle-completeness or tool check.
if [[ "$MODE" == "proxy" ]]; then
  CORE_ROOT="${YOUNG_ROUTER_CORE_ROOT:-$CORE}"
  [[ -d "$CORE_ROOT/runtime/site-packages" ]] || {
    echo "dev-loop-macos: no bundled Core runtime at $CORE_ROOT" >&2
    echo "Install the app once, or set YOUNG_ROUTER_CORE_ROOT to a Core directory." >&2
    exit 1
  }
  RUN_ROOT="${YOUNG_ROUTER_DEV_ROOT:-${TMPDIR:-/tmp}/young-router-dev-loop}"
  PORT="${YOUNG_ROUTER_DEV_PORT:-12490}"
  WORKERS="${YOUNG_ROUTER_DEV_WORKERS:-16}"
  mkdir -p "$RUN_ROOT/runtime"
  # A scratch root starts from the published example, never from the user's own
  # configuration: the point is a route a test can point at, and a temporary
  # directory is not where provider keys belong.
  if [[ ! -f "$RUN_ROOT/runtime/config.yaml" ]]; then
    cp "$ROOT/config.example.yaml" "$RUN_ROOT/runtime/config.yaml"
    echo "dev-loop-macos: staged a scratch document from config.example.yaml"
    echo "  edit $RUN_ROOT/runtime/config.yaml to add the route under test"
  fi
  # Core's own environment contract, so the proxy writes its state under the
  # scratch root instead of `~/.young-router`.  The bundled launcher prepends
  # its own site-packages to PYTHONPATH, and no `young_router` is installed
  # there, so the checkout's copy is the one imported as long as it is on the
  # path at all -- which is what makes an edit here take effect.
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
  export YOUNG_ROUTER_HOME="$RUN_ROOT"
  export LITELLM_RUNTIME_ROOT="$RUN_ROOT"
  export LITELLM_RUNTIME_DIR="$RUN_ROOT/runtime"
  export LITELLM_CONFIG_FILE="$RUN_ROOT/runtime/config.yaml"
  export LITELLM_RUNTIME_CONFIG="$RUN_ROOT/runtime/config.yaml"
  export LITELLM_RECENT_REQUESTS_LOG="$RUN_ROOT/recent-requests.jsonl"
  export LITELLM_NATIVE_PID_FILE="$RUN_ROOT/runtime/litellm.pid"
  export YOUNG_ROUTER_RUNTIME_SETTINGS_FILE="$RUN_ROOT/runtime-settings.env"
  export YOUNG_ROUTER_ROUTE_RECOVERY_STATE_FILE="$RUN_ROOT/runtime/route-recovery-state.json"
  export YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FILE="$RUN_ROOT/runtime/deployment-cooldowns.json"
  export YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY_FILE="$RUN_ROOT/runtime/session-deployment-affinity.json"
  export LITELLM_LOCAL_MODEL_COST_MAP=true
  exec "$CORE_ROOT/runtime/bin/python" -m young_router.proxy.macos_proxy \
    --config "$RUN_ROOT/runtime/config.yaml" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --workers "$WORKERS"
fi

if [[ ! -d "$CORE/young_router" ]]; then
  echo "dev-loop-macos: no installed bundle at $APP" >&2
  echo "Run scripts/build-and-install-macos.sh once to install one, then iterate here." >&2
  exit 1
fi
for tool in rsync diff find cmp; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "dev-loop-macos: $tool is required" >&2
    exit 3
  }
done

# --- what the change touches ------------------------------------------------
#
# A native source compiles into the host executable, so being newer than that
# executable is the same evidence a stale build gives.  Only this host's
# sources gate a macOS sync: a Windows edit is a build for whoever ships
# Windows, but it cannot change what a macOS window shows, and letting it block
# this loop would make unrelated work look like a reason to rebuild.
native_files=()
host_executable="$APP/Contents/MacOS/YoungRouter"
for native_root in "$RN_ROOT/apps/macos/macos" "$RN_ROOT/apps/macos/src/native"; do
  [[ -d "$native_root" ]] || continue
  while IFS= read -r changed; do
    [[ -n "$changed" ]] || continue
    native_files+=("${changed#"$ROOT"/}")
  done < <(find "$native_root" -newer "$host_executable" -type f \
    \( -name '*.m' -o -name '*.mm' -o -name '*.swift' -o -name '*.h' -o -name '*.cpp' \
       -o -name '*.pbxproj' -o -name 'Podfile' -o -name '*.xcconfig' -o -name '*.plist' \
       -o -name '*.entitlements' \) \
    -not -path '*/Pods/*' -not -path '*/build/*' -not -path '*/node_modules/*' 2>/dev/null | head -50)
done

# --- what is stale ----------------------------------------------------------
#
# Both comparisons are against what is installed, not against a timestamp file:
# the question is only ever "would a restart change what runs".  Metro reads
# nothing outside these roots to produce the bundle.
js_changed=0
python_changed=0
bundle="$RESOURCES/main.jsbundle"
if [[ -f "$bundle" ]]; then
  [[ -z "$(find "$RN_ROOT/packages" "$RN_ROOT/apps/macos/src" "$RN_ROOT/apps/macos/index.js" \
    -newer "$bundle" -type f \
    \( -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' -o -name '*.mjs' -o -name '*.json' \) \
    -not -path '*/node_modules/*' -not -path '*/build/*' 2>/dev/null | head -1)" ]] || js_changed=1
else
  js_changed=1
fi
if ! diff -rq "${CORE_SYNC_EXCLUDES[@]}" "$ROOT/young_router" "$CORE/young_router" >/dev/null 2>&1; then
  python_changed=1
fi
if [[ -f "$CORE/sitecustomize.py" ]] && ! cmp -s "$ROOT/sitecustomize.py" "$CORE/sitecustomize.py"; then
  python_changed=1
fi

if ((${#native_files[@]} > 0)); then
  echo "dev-loop-macos: native sources are newer than the installed host, so only a build carries them:"
  printf '  %s\n' "${native_files[@]}"
  echo "  -> scripts/build-and-install-macos.sh"
  if [[ "$MODE" != "check" ]]; then
    echo "dev-loop-macos: refusing to sync the rest of a change whose native half needs a build." >&2
    exit 1
  fi
fi

if [[ "$MODE" == "check" ]]; then
  echo "dev-loop-macos: app         $APP"
  echo "dev-loop-macos: js bundle   $([[ "$js_changed" == 0 ]] && echo current || echo stale)"
  echo "dev-loop-macos: python      $([[ "$python_changed" == 0 ]] && echo current || echo stale)"
  exit 0
fi

# --- sync -------------------------------------------------------------------

sync_python() {
  [[ "$python_changed" == 1 ]] || return 0
  rsync -a --delete "${CORE_SYNC_EXCLUDES[@]}" \
    "$ROOT/young_router/" "$CORE/young_router/"
  cp "$ROOT/sitecustomize.py" "$CORE/sitecustomize.py"
  echo "dev-loop-macos: python tree synced"
}

sync_js() {
  [[ "$js_changed" == 1 ]] || return 0
  command -v node >/dev/null 2>&1 || {
    echo "dev-loop-macos: node is required to rebundle the UI" >&2
    exit 3
  }
  # The host's own build phase, called the same way.  It stamps the bundle with
  # a hash of the sources Metro reads, so a second run with no edit in between
  # costs a hash instead of a bundle.
  (
    cd "$RN_ROOT/apps/macos"
    node ../../scripts/bundle-macos.mjs bundle \
      --entry-file index.js \
      --platform macos \
      --dev false \
      --bundle-output "$bundle" \
      --assets-dest "$RESOURCES" \
      --minify false
  ) >/dev/null
  echo "dev-loop-macos: js bundle rebuilt"
}

case "$MODE" in
  js)
    [[ "$js_changed" == 1 ]] || { echo "dev-loop-macos: js bundle is current; nothing to do"; exit 0; }
    sync_js
    ;;
  python)
    [[ "$python_changed" == 1 ]] || { echo "dev-loop-macos: python tree is current; nothing to do"; exit 0; }
    sync_python
    ;;
  auto)
    [[ "$js_changed$python_changed" != "00" ]] || { echo "dev-loop-macos: nothing changed; nothing to do"; exit 0; }
    sync_python
    sync_js
    ;;
esac

# One restart, after every half is in place: restarting between them would run a
# window against a Core from a different revision of the change.  This delegates
# instead of signalling a process, because the stop policy is shared with the
# deploy and stated in `app-processes.sh`.
if ! "$RESTART_APP" >/dev/null 2>&1; then
  echo "dev-loop-macos: the app did not restart cleanly" >&2
  exit 1
fi
echo "dev-loop-macos: running with the change -- run scripts/build-and-install-macos.sh when it is settled"
