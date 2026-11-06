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
# here: 10 to 16 s for an edit, of which the restart alone is ~10 s.
#
#   scripts/dev-loop-macos.sh            sync whatever changed, restart
#   scripts/dev-loop-macos.sh --js       rebundle and install the UI only
#   scripts/dev-loop-macos.sh --python   sync the Python tree only
#   scripts/dev-loop-macos.sh --metro    point the installed app at Metro, so a
#                                        UI edit costs a reload instead of a
#                                        bundle; keeps Metro in this terminal
#   scripts/dev-loop-macos.sh --reload   make the running app load the bundle
#                                        again, without restarting anything
#   scripts/dev-loop-macos.sh --proxy    run an isolated proxy on a scratch
#                                        port, leaving the app alone
#   scripts/dev-loop-macos.sh --check    report which half is stale, act not
#
# `--metro` plus `--reload` is the JS loop.  `RCTHost` registers itself as a
# reload listener in every configuration, and a reload re-reads the bundle URL
# and rebuilds the JS instance in place, so a UI edit costs one bundle fetch
# (~1 s warm) instead of a bundle build plus a restart (~20 s).  What it does
# not do is keep React state or an open native window's contents: a reload
# resets the React tree, which is the same thing a restart gives today, minus
# Core and the proxy going down with it.
#
# Neither mode turns HMR on: the installed host is Release, so `RCT_DEV` is 0,
# `setupHMRClientWithBundleURL` is the empty stub, and nothing opens an HMR
# socket.  Nor is there a red box: a bundle that fails to compile reaches
# `RCTFatal` and aborts.  That is why `--metro` fetches the bundle before it
# points the app at it, and why `--reload` refuses while Metro is not answering
# -- and why `AppDelegate.mm` drops the override when a bundle load fails, so a
# broken bundle costs one launch instead of a launch loop.
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
# Neither mode is the deploy: `build-and-install-macos.sh` re-checks the
# upstream releases, re-signs, and installs, so it runs when a deploy is asked
# for (or when this loop refused a native source).  A change the loop carried
# is already live in the installed app, and that is where an edit ends.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RN_ROOT="$ROOT/rn"
APP="${YOUNG_ROUTER_INSTALL_APP:-/Applications/Young Router.app}"
CORE="$APP/Contents/Resources/Core"
RESOURCES="$APP/Contents/Resources"
RESTART_APP="$ROOT/scripts/restart-app.sh"
APP_PROCESSES="$ROOT/scripts/app-processes.sh"
# `AppDelegate.mm` answers this link by reloading the JS instance in place.
RELOAD_URL="young-router://reload"
MODE="auto"

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

while (($# > 0)); do
  case "$1" in
    --js) MODE="js" ;;
    --python) MODE="python" ;;
    --metro) MODE="metro" ;;
    --reload) MODE="reload" ;;
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

# --- Metro dev server ---------------------------------------------------------
#
# This mode points the installed app at Metro instead of the packaged bundle,
# then keeps Metro running in this terminal.  Two facts shape it:
#
#   * the installed host is Release, so `RCT_DEV` is 0 and nothing opens an HMR
#     socket -- an edit is picked up by the next bundle load, not live;
#   * a Release host has no red box, so a bundle that fails to compile reaches
#     `RCTFatal` and aborts.  The bundle is therefore fetched here first, and
#     the override is written only when Metro compiled it.
#
# The override lives in the app's own defaults, so a failed fetch would also
# poison the next launch; `AppDelegate.mm` clears it when a bundle load fails.
METRO_PID=""
if [[ "$MODE" == "metro" ]]; then
  [[ -d "$CORE/young_router" ]] || {
    echo "dev-loop-macos: no installed bundle at $APP" >&2
    echo "Run scripts/build-and-install-macos.sh once to install one, then iterate here." >&2
    exit 1
  }
  command -v node >/dev/null 2>&1 || {
    echo "dev-loop-macos: node is required to run Metro" >&2
    exit 3
  }
  command -v curl >/dev/null 2>&1 || {
    echo "dev-loop-macos: curl is required to probe Metro" >&2
    exit 3
  }
  METRO_PORT="${YOUNG_ROUTER_METRO_PORT:-8081}"
  # `dev=false`, not the usual `dev=true`: this bundle runs in a Release host,
  # and `__DEV__` is what enables the renderer's view-config check that a
  # `topLevelType` appears in both `bubblingEventTypes` and `directEventTypes`.
  # The generated `LiteLLMAppKitPicker` and `LiteLLMAppKitSegmentedControl`
  # configs declare `topChange` as a direct event while the platform base config
  # declares it as bubbling, so a dev bundle aborts on first render with
  # "Event cannot be both direct and bubbling: topChange" and the window shows
  # nothing.  The packaged bundle is built without `__DEV__` and never runs that
  # check, so serving the same shape keeps the loop faithful to what ships --
  # a bug that only reproduces in dev would be a bug the loop could not show.
  # `minify=false` still keeps the bundle readable.
  METRO_URL="http://127.0.0.1:${METRO_PORT}/index.bundle?platform=macos&dev=false&minify=false"
  METRO_LOG="${TMPDIR:-/tmp}/young-router-metro-${METRO_PORT}.log"
  # `node_modules/.bin/react-native` is the CLI's shell shim, not a JS entry:
  # running it under `node` is a syntax error, so it is executed directly.
  (
    cd "$RN_ROOT/apps/macos"
    exec "$RN_ROOT/node_modules/.bin/react-native" start --port "$METRO_PORT"
  ) >"$METRO_LOG" 2>&1 &
  METRO_PID=$!
  # Metro answers nothing until it has resolved the port, so wait for the status
  # endpoint rather than sleeping a fixed guess.  `ps -p` is the liveness probe:
  # this script must not signal anything (see the stop policy above), and a dead
  # Metro would otherwise be waited out to the full loop.
  metro_ready=0
  for _ in $(seq 1 240); do
    if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:${METRO_PORT}/status"; then
      metro_ready=1
      break
    fi
    ps -p "$METRO_PID" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if [[ "$metro_ready" != "1" ]]; then
    echo "dev-loop-macos: Metro did not start; see $METRO_LOG" >&2
    tail -20 "$METRO_LOG" >&2
    exit 1
  fi
  # The preflight is the whole safety story: a bundle Metro cannot compile is
  # one the Release host would abort on, and it would abort after the override
  # was already written.
  status="$(curl -s -o "$METRO_LOG.bundle" -w '%{http_code}' --max-time 300 "$METRO_URL" || echo 000)"
  if [[ "$status" != "200" ]]; then
    echo "dev-loop-macos: Metro could not compile the bundle (HTTP $status); the app was left alone." >&2
    head -20 "$METRO_LOG.bundle" >&2
    exit 1
  fi
  echo "dev-loop-macos: Metro compiled the bundle; pointing the app at $METRO_URL"
  defaults write young.router.app MetroServerURL "$METRO_URL"
  # The one restart this mode makes: it is not a sync, it is what makes the
  # override visible to a host that already read it at launch.
  if ! "$RESTART_APP" >/dev/null 2>&1; then
    echo "dev-loop-macos: the app did not restart cleanly" >&2
    exit 1
  fi
  echo "dev-loop-macos: app is loading from Metro; log at $METRO_LOG"
  echo "  After an edit, from another terminal: scripts/dev-loop-macos.sh --reload"
  echo "  Back to the packaged bundle:"
  echo "    defaults delete young.router.app MetroServerURL && scripts/restart-app.sh"
  # Staying attached keeps Metro alive with this shell: Ctrl-C ends both, and
  # the next launch of the app finds a server that no longer answers.
  wait "$METRO_PID"
  exit 0
fi

# --- the in-place JS reload ---------------------------------------------------
#
# The cheapest loop there is: the app re-reads its bundle URL and rebuilds the
# JS instance, while Core, the proxy and every worker keep running.  It answers
# only when the bundle it would load is known to compile, because a Release
# host has no red box and answers a failed load with `RCTFatal`.
if [[ "$MODE" == "reload" ]]; then
  [[ -n "$("$APP_PROCESSES" hosts "$APP")" ]] || {
    echo "dev-loop-macos: the app is not running; start it once with scripts/restart-app.sh" >&2
    exit 1
  }
  override="$(defaults read young.router.app MetroServerURL 2>/dev/null || true)"
  if [[ -n "$override" ]]; then
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "$override" || echo 000)"
    if [[ "$status" != "200" ]]; then
      echo "dev-loop-macos: the dev server at $override answered HTTP $status, so a reload would abort the app." >&2
      echo "  Fix the bundle, or return to the packaged one:" >&2
      echo "    defaults delete young.router.app MetroServerURL && scripts/restart-app.sh" >&2
      exit 1
    fi
  fi
  open "$RELOAD_URL" >/dev/null 2>&1 || {
    echo "dev-loop-macos: could not send the reload link" >&2
    exit 1
  }
  echo "dev-loop-macos: reload requested${override:+ (bundle: $override)}"
  exit 0
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
# The browser page is Core's own copy of the shared UI, so a page edit has to
# reach `Core/web` the same way the rest of the shared tree reaches the bundle.
web_changed=0
# A checkout that never built the page has nothing to compare, and an install
# that never received one has nothing to update: without this guard a missing
# `dist` reads as "changed" on every run and restarts the app for no edit.
if [[ -d "$RN_ROOT/apps/web/dist" ]] && ! diff -rq "$RN_ROOT/apps/web/dist" "$CORE/web" >/dev/null 2>&1; then
  web_changed=1
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
  echo "dev-loop-macos: web page    $([[ "$web_changed" == 0 ]] && echo current || echo stale)"
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

sync_web() {
  [[ "$web_changed" == 1 ]] || return 0
  [[ -d "$RN_ROOT/apps/web/dist" ]] || return 0
  rsync -a --delete "$RN_ROOT/apps/web/dist/" "$CORE/web/"
  echo "dev-loop-macos: web page synced"
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
  metro)
    # The bundle is served from Metro, so there is no bundle to write.  What
    # this mode needs from below is the one restart that makes the app load it.
    ;;
  auto)
    [[ "$js_changed$python_changed$web_changed" != "000" ]] || { echo "dev-loop-macos: nothing changed; nothing to do"; exit 0; }
    sync_python
    sync_web
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

echo "dev-loop-macos: running with the change"
