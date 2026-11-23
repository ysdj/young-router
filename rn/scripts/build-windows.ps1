param(
  # A machine that has never built React Native Windows has no patched
  # Microsoft.ReactNative.dll to swap in, and a source build of it is half an
  # hour with no valid PRI.  This lets that machine produce a working artifact
  # with the upstream wheel mapping instead of refusing to build at all; the
  # run says so, loudly, and nothing about the mapping is claimed.
  [switch]$AllowUpstreamWheel,
  # The other way to answer the same gap: install the prepared build of that DLL
  # from a directory an export wrote (or an HTTP URL carrying the same files).
  # The digest and the RNW release are checked before it is used, so a copy from
  # a different release is refused instead of linked.
  [string]$FetchPrebuilt = $env:YOUNG_ROUTER_RNW_PREBUILT
)
$ErrorActionPreference = "Stop"

$RnRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = (Resolve-Path (Join-Path $RnRoot "..")).Path
$AppRoot = Join-Path $RnRoot "apps\windows"
$WindowsArch = if ([string]::IsNullOrWhiteSpace($env:YOUNG_ROUTER_WINDOWS_ARCH)) {
  "x64"
} else {
  $env:YOUNG_ROUTER_WINDOWS_ARCH.Trim().ToUpperInvariant()
}
if ($WindowsArch -notin @("X64", "ARM64")) {
  throw "YOUNG_ROUTER_WINDOWS_ARCH must be x64 or ARM64."
}
# ``$WindowsArch`` is only for comparisons.  Every path, every MSBuild platform
# and the RNW CLI use the way the architecture is actually spelled: React Native
# Windows puts its own output under ``target\$(Platform)\$(Configuration)``
# (Directory.Build.props), MSBuild uses the solution configuration's platform
# verbatim, and that spelling is ``x64`` -- so an uppercased ``X64`` looked for
# ``windows\X64\Release`` and ``target\X64``, which a build never creates.
$WindowsArchName = if ($WindowsArch -eq "ARM64") { "ARM64" } else { "x64" }
# LiteLLM's required proxy wheel is only published for Windows x64. Core is
# a separate process, so even the native ARM64 host runs the bundled x64
# Python on Windows 11 without an extra installation.
$PythonTarget = "cpython-3.12-windows-x86_64-none"
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  $Core = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
} else {
  $Core = Join-Path $env:RUNNER_TEMP ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
}
# Third-party staging lives in a persistent cache rather than a fresh GUID
# directory, for the same reason the macOS build does it: a build stages the
# same four releases over and over, each script resolves its upstream release
# on every run, and re-installing a release it has already staged is the
# largest remaining cost.  A tree is reused only when its own lookup resolves
# that exact release (see ``reused_staged_release`` in
# scripts/update_common.py), so deleting this directory is always safe.  The
# Node and Bun runtime is staged here for the same reason: it is a release too,
# and staging it into a fresh GUID directory is what made every build download
# both again and left their own version markers nothing to compare against.
$StagingCache = if ($env:YOUNG_ROUTER_STAGING_CACHE) {
  $env:YOUNG_ROUTER_STAGING_CACHE
} else {
  Join-Path $RnRoot ".staging-cache"
}
New-Item -ItemType Directory -Force -Path $StagingCache | Out-Null
$NodeRuntime = Join-Path $StagingCache "node-runtime"
$ModelPrintWork = Join-Path $StagingCache "modelprint"
$LlmFingerprintWork = Join-Path $StagingCache "llm-fingerprint-detector"
$ConnectWorkBuddyWork = Join-Path $StagingCache "connect-workbuddy"
$ConnectQoderWork = Join-Path $StagingCache "connect-qoder"
$OurFreeModelWork = Join-Path $StagingCache "our-free-model"
$DshVisionRouterWork = Join-Path $StagingCache "dsh-vision-router"
$ContextAttachmentManagerWork = Join-Path $StagingCache "context-attachment-manager"

function Copy-CoreSource {
  param([string]$Source, [string]$Destination)
  if (-not (Test-Path $Source)) { throw "Bundled Core source is incomplete." }
  if ((Get-Item -LiteralPath $Source).PSIsContainer) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $CopyThreads = if ($env:YOUNG_ROUTER_LOW_RESOURCE_BUILD -notin @("0", "false", "no")) { 4 } else { 16 }
    & robocopy $Source $Destination /E /MT:$CopyThreads /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Could not copy bundled Core source: $Source" }
  } else {
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
  }
}

try {
  Set-Location $RnRoot
  if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "pnpm is required to build the React Native Windows host."
  }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to build the self-contained Windows Core runtime."
  }
  & uv run --no-project --python 3.12 (Join-Path $ProjectRoot "scripts\update_litellm.py") --sync-venv
  if ($LASTEXITCODE -ne 0) { throw "Could not update LiteLLM to the latest stable release." }
  $NodeRuntimeUpdater = Join-Path $ProjectRoot "scripts\update_node_runtime.py"
  & uv run --no-project --python 3.12 $NodeRuntimeUpdater `
    --output $NodeRuntime
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Node.js runtime." }
  # Bun is the one runtime the staged Our Free Model package cannot be without:
  # its official OpenCode lane runs every request through a Bun child and
  # refuses the roster outright when it finds no such executable.  A host that
  # installed Bun itself is not enough -- the app is launched outside a login
  # shell -- so the build stages it beside Node, where the adapter resolves it.
  # It stages into the Node tree because both land in Core/runtime/bin.
  $BunRuntimeUpdater = Join-Path $ProjectRoot "scripts\update_bun_runtime.py"
  & uv run --no-project --python 3.12 $BunRuntimeUpdater `
    --output $NodeRuntime
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Bun runtime." }
  # The deep test's tables are versioned with the values they produced, so a
  # release built from a stale pair reports differences that are not there.  The
  # check runs first and reports whether upstream moved; staging then always
  # runs, so a build never packages whatever happened to be in the cache.
  # Exit code 4 means upstream was unreachable, and a build must not treat that
  # as "we are current".
  $ModelPrintUpdater = Join-Path $ProjectRoot "scripts\update_modelprint.py"
  & uv run --no-project --python 3.12 $ModelPrintUpdater --output $ModelPrintWork --check
  if ($LASTEXITCODE -eq 4) { throw "Could not reach the modelprint upstream; the deep test's tables cannot be verified." }
  & uv run --no-project --python 3.12 $ModelPrintUpdater --output $ModelPrintWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled modelprint tables." }
  # The precision probe is a TypeScript library, so it is staged and built with
  # its own toolchain.  Same gate: ask upstream whether it moved first.
  $LlmFingerprintUpdater = Join-Path $ProjectRoot "scripts\update_llm_fingerprint.py"
  & uv run --no-project --python 3.12 $LlmFingerprintUpdater --output $LlmFingerprintWork --check
  if ($LASTEXITCODE -eq 4) { throw "Could not reach the precision probe's upstream; it cannot be verified." }
  & uv run --no-project --python 3.12 $LlmFingerprintUpdater --output $LlmFingerprintWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled precision probe." }
  # WorkBuddy access is driven through the published third-party package, so
  # every artifact build re-resolves its latest release.
  $ConnectWorkBuddyUpdater = Join-Path $ProjectRoot "scripts\update_connect_workbuddy.py"
  & uv run --no-project --python 3.12 $ConnectWorkBuddyUpdater --output $ConnectWorkBuddyWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-connect-workbuddy package." }
  # Qoder access is driven through the published third-party package, so every
  # artifact build re-resolves its latest release too.
  $ConnectQoderUpdater = Join-Path $ProjectRoot "scripts\update_connect_qoder.py"
  & uv run --no-project --python 3.12 $ConnectQoderUpdater --output $ConnectQoderWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-connect-qoder package." }
  # The Our Free Model lane stages the upstream package's sources.
  $OurFreeModelUpdater = Join-Path $ProjectRoot "scripts\update_our_free_model.py"
  & uv run --no-project --python 3.12 $OurFreeModelUpdater --output $OurFreeModelWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Our Free Model package." }
  # The vision fallback routes through the staged upstream package rather than a
  # copy of its chain, so every artifact build re-resolves its latest release.
  $DshVisionRouterUpdater = Join-Path $ProjectRoot "scripts\update_dsh_vision_router.py"
  & uv run --no-project --python 3.12 $DshVisionRouterUpdater --output $DshVisionRouterWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-vision-router package." }
  # The image-context pass copies Context Attachment Manager's placeholder wording
  # and reads its selection file, so every build re-checks that release.
  $ContextAttachmentManagerUpdater = Join-Path $ProjectRoot "scripts\update_context_attachment_manager.py"
  & uv run --no-project --python 3.12 $ContextAttachmentManagerUpdater --output $ContextAttachmentManagerWork
  if ($LASTEXITCODE -ne 0) { throw "Could not verify the Context Attachment Manager release the image-context pass mirrors." }
  if (-not (Test-Path (Join-Path $AppRoot "windows"))) {
    throw "React Native Windows host project is missing at rn/apps/windows/windows."
  }

  # The wheel fix changes RNW source, so the matching incremental source-build
  # output must already exist: this script swaps it in after the fast NuGet
  # build, and a checkout without it would otherwise ship the upstream wheel
  # with nothing in the build output to say so.
  $ReactNativeWheelPatch = Join-Path $RnRoot "scripts\patch-react-native-windows.mjs"
  $ReactNativeTargetDll = Join-Path $RnRoot "node_modules\react-native-windows\target\$WindowsArchName\Release\Microsoft.ReactNative\Microsoft.ReactNative.dll"
  $UseUpstreamWheel = [bool]($AllowUpstreamWheel -or $env:YOUNG_ROUTER_ALLOW_UPSTREAM_WHEEL -notin @($null, "", "0", "false", "no"))
  if ($UseUpstreamWheel) {
    Write-Warning "Building with upstream React Native Windows: its wheel mapping eases every event and does not follow the system lines-per-notch setting. Pass no override on a machine that has the prepared build."
  } else {
    if ($FetchPrebuilt) {
      if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw "node is required to install the prepared React Native Windows build."
      }
      $WheelInstaller = Join-Path $RnRoot "scripts\react-native-windows-wheel.mjs"
      if (-not (Test-Path -LiteralPath $WheelInstaller -PathType Leaf)) {
        throw "The prepared React Native Windows installer is missing: $WheelInstaller."
      }
      & node $WheelInstaller install --root $ProjectRoot --arch $WindowsArchName --from $FetchPrebuilt
      if ($LASTEXITCODE -ne 0) { throw "Could not install the prepared React Native Windows build from $FetchPrebuilt." }
    }
    if (-not (Test-Path -LiteralPath $ReactNativeTargetDll -PathType Leaf)) {
      throw "RNW source-build output is missing: $ReactNativeTargetDll. Copy the prepared Microsoft.ReactNative.dll there (or pass -FetchPrebuilt <dir|url>, or YOUNG_ROUTER_RNW_PREBUILT), or build this machine with -AllowUpstreamWheel to ship the upstream wheel mapping instead."
    }
    if (-not (Test-Path -LiteralPath $ReactNativeWheelPatch -PathType Leaf)) {
      throw "The prepared RNW wheel patch is missing: $ReactNativeWheelPatch."
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
      throw "node is required to patch the React Native Windows source tree."
    }
    & node $ReactNativeWheelPatch --root $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not apply the prepared React Native Windows wheel patch." }
  }

  pnpm run build
  if ($LASTEXITCODE -ne 0) { throw "React Native Windows JavaScript build checks failed." }

  Remove-Item -LiteralPath $Core -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $Core | Out-Null
  Copy-CoreSource (Join-Path $ProjectRoot "sitecustomize.py") (Join-Path $Core "sitecustomize.py")
  Copy-CoreSource (Join-Path $ProjectRoot "young_router") (Join-Path $Core "young_router")
  Copy-CoreSource $ModelPrintWork (Join-Path $Core "young_router\adapters\modelprint")
  Copy-CoreSource $LlmFingerprintWork (Join-Path $Core "young_router\adapters\llm-fingerprint-detector")
  Copy-CoreSource $ConnectWorkBuddyWork (Join-Path $Core "young_router\adapters\connect-workbuddy")
  Copy-CoreSource $ConnectQoderWork (Join-Path $Core "young_router\adapters\connect-qoder")
  Copy-CoreSource $OurFreeModelWork (Join-Path $Core "young_router\adapters\our-free-model")
  Copy-CoreSource $DshVisionRouterWork (Join-Path $Core "young_router\adapters\dsh-vision-router")
  Get-ChildItem -LiteralPath $Core -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $Core -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force

  $RuntimeBin = Join-Path $Core "runtime\bin"
  $PythonInstalls = Join-Path $Core ".python-installs"
  New-Item -ItemType Directory -Path $RuntimeBin, $PythonInstalls | Out-Null
  $RuntimeSeed = $env:YOUNG_ROUTER_RUNTIME_SEED
  if (-not $RuntimeSeed) {
    # The runtime this tree staged last time is this build's seed.  Its LiteLLM
    # version is checked below, and that check is what decides whether anything
    # has to be installed: re-resolving the whole proxy into a fresh temporary
    # runtime on every build is minutes of work for a release that has not
    # moved.  A runtime without an interpreter is ignored by the same check.
    $PreviousRuntime = Join-Path $AppRoot "windows\$WindowsArchName\Release\Core\runtime"
    if (Test-Path (Join-Path $PreviousRuntime "bin\python.exe")) {
      $RuntimeSeed = $PreviousRuntime
    }
  }
  $RuntimeSeedUsable = $false
  if ($RuntimeSeed -and (Test-Path (Join-Path $RuntimeSeed "bin\python.exe"))) {
    $SeedPlatform = & (Join-Path $RuntimeSeed "bin\python.exe") -c "import sysconfig; print(sysconfig.get_platform())"
    $RuntimeSeedUsable = $LASTEXITCODE -eq 0 -and $SeedPlatform -eq "win-amd64"
  }
  if ($RuntimeSeedUsable) {
    Copy-Item -Path (Join-Path $RuntimeSeed "bin\*") -Destination $RuntimeBin -Recurse -Force
  } else {
    uv python install $PythonTarget --install-dir $PythonInstalls --no-bin
    $PythonSource = Get-ChildItem -LiteralPath $PythonInstalls -Directory |
      Where-Object { $_.Name -like "cpython-3.12*-windows-*" } |
      Select-Object -First 1
    if ($null -eq $PythonSource -or -not (Test-Path (Join-Path $PythonSource.FullName "python.exe"))) {
      throw "uv did not install the bundled Windows x64 Python 3.12 runtime."
    }
    Copy-Item -Path (Join-Path $PythonSource.FullName "*") -Destination $RuntimeBin -Recurse -Force
  }
  Remove-Item -LiteralPath $PythonInstalls -Recurse -Force -ErrorAction SilentlyContinue

  $RuntimePython = Join-Path $RuntimeBin "python.exe"
  $LiteLLMVersion = (Get-Content -Raw (Join-Path $ProjectRoot "LITELLM_VERSION")).Trim()
  # ``uv`` marks the standalone interpreter it installs as externally managed
  # (``EXTERNALLY-MANAGED`` in its ``lib/python3.12``), so installing *into* it
  # is refused:
  #
  #   The interpreter at ... is externally managed
  #
  # ``--target`` writes the packages into that interpreter's own site-packages
  # instead of asking it to accept an install.
  $RuntimeSitePackages = & $RuntimePython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
  if ($LASTEXITCODE -ne 0 -or -not $RuntimeSitePackages) {
    throw "Could not resolve the bundled Windows runtime's site-packages path."
  }
  function Remove-Stale-LiteLLM-Metadata {
    # ``--target`` merges into an existing directory instead of replacing it,
    # and it does not uninstall the release it is upgrading from.  A seed that
    # already carried 1.106.4 therefore ends up with both
    # ``litellm-1.106.4.dist-info`` and ``litellm-1.106.6.dist-info``, and
    # ``importlib.metadata`` stops being able to name the release at all -- which
    # reads as "the update did nothing" and stops the build on the release it
    # just installed.  Only the metadata directory is removed; the new files
    # already overwrote the module tree.
    Get-ChildItem -LiteralPath $RuntimeSitePackages -Directory -Filter "litellm-*.dist-info" -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -ne "litellm-$LiteLLMVersion.dist-info" } |
      Remove-Item -Recurse -Force
  }
  function Install-LiteLLM {
    & uv pip install --python $RuntimePython --target "$RuntimeSitePackages" --upgrade "litellm[proxy]==$LiteLLMVersion" "fastapi==0.140.3" PyYAML
    if ($LASTEXITCODE -ne 0) { throw "Could not install the bundled Windows runtime dependencies." }
    Remove-Stale-LiteLLM-Metadata
  }
  if ($RuntimeSeedUsable) {
    $SeedCheck = & $RuntimePython -c "from importlib.metadata import version; print(version('litellm'))"
    $SeedVersion = if ($LASTEXITCODE -eq 0) { $SeedCheck.Trim() } else { "none" }
    if ($SeedVersion -ne $LiteLLMVersion) {
      # Keep the large native/Python seed reusable while refreshing the small
      # fast-moving LiteLLM layer when its release lock advances -- or when the
      # seed carries no metadata at all, which is what an interrupted install
      # leaves behind and what used to stop the build here.
      Write-Output "Updating LiteLLM in the prebuilt runtime seed: $SeedVersion -> $LiteLLMVersion."
      Install-LiteLLM
      $SeedCheck = & $RuntimePython -c "from importlib.metadata import version; print(version('litellm'))"
      if ($LASTEXITCODE -ne 0) {
        throw "The prebuilt runtime seed does not carry LiteLLM metadata."
      }
      $SeedVersion = $SeedCheck.Trim()
    }
    if ($SeedVersion -ne $LiteLLMVersion) {
      throw "The prebuilt runtime seed carries LiteLLM $SeedVersion instead of $LiteLLMVersion."
    }
  } else {
    Install-LiteLLM
  }
  Copy-Item -LiteralPath (Join-Path $NodeRuntime "node.exe") -Destination $RuntimeBin -Force
  Copy-Item -LiteralPath (Join-Path $NodeRuntime "bun.exe") -Destination $RuntimeBin -Force
  Copy-Item -LiteralPath (Join-Path $NodeRuntime "BUN_VERSION") -Destination $RuntimeBin -Force
  Copy-Item -LiteralPath (Join-Path $NodeRuntime "BUN_LICENSE") -Destination $RuntimeBin -Force
  $GeneratedScripts = Join-Path $RuntimeBin "Scripts"
  if (Test-Path $GeneratedScripts) {
    Remove-Item -LiteralPath $GeneratedScripts -Recurse -Force
  }
  $CorePruner = Join-Path $ProjectRoot "scripts\prune-windows-core.py"
  & (Join-Path $RuntimeBin "python.exe") $CorePruner $Core
  if ($LASTEXITCODE -ne 0) { throw "Could not prune the Windows Core runtime payload." }

  $LiteLLMCommand = @'
@echo off
setlocal
set "RUNTIME_ROOT=%~dp0"
"%RUNTIME_ROOT%python.exe" -c "from litellm import run_server; run_server()" %*
'@
  Set-Content -LiteralPath (Join-Path $RuntimeBin "litellm.cmd") -Value $LiteLLMCommand -Encoding ascii
  Copy-Item -LiteralPath (Join-Path $ProjectRoot "LITELLM_VERSION") -Destination (Join-Path $Core "runtime\LITELLM_VERSION") -Force
  # Both probe engines are checked here, before the app is packaged: a table
  # set whose pinned texts no longer hash to their recorded digests, or a
  # precision build that never ran, would otherwise surface as a failed deep
  # test long after the build.
  $ModelPrintRoot = Join-Path $Core "young_router\adapters\modelprint"
  $LlmFingerprintRoot = Join-Path $Core "young_router\adapters\llm-fingerprint-detector"
  $ProbeSmokeScript = @"
import hashlib, json, sys
root = r'$ModelPrintRoot'
manifest = json.load(open(root + '/manifest.json', encoding='utf-8'))
texts = manifest['probe_texts']
digests = manifest['probe_text_digests']
assert set(texts) == set(digests) and texts, 'no pinned probe texts staged'
for name, text in texts.items():
    assert hashlib.sha256(text.encode('utf-8')).hexdigest() == digests[name], name
assert manifest['supported_models'], 'no supported model staged'
sys.path.insert(0, r'$Core')
from young_router.adapters import modelprint as adapter
assert adapter.available(), 'the staged tables do not verify'
assert adapter.protocol_for('claude-opus-5') == 'anthropic'
assert adapter.protocol_for('gpt-5.6-sol') == 'openai'
lfd = r'$LlmFingerprintRoot'
lfd_manifest = json.load(open(lfd + '/manifest.json', encoding='utf-8'))
assert lfd_manifest['thresholds'] == {'JSD_MATCH_THRESHOLD': 0.25, 'JSD_MISMATCH_THRESHOLD': 0.35}, lfd_manifest['thresholds']
assert open(lfd + '/dist/index.js', encoding='utf-8').read().strip(), 'the precision probe has no build output'
print('young-router-modelprint-tables-ok')
"@
  $ProbeSmoke = & (Join-Path $RuntimeBin "python.exe") -c $ProbeSmokeScript 2>&1
  if ($LASTEXITCODE -ne 0 -or ($ProbeSmoke -join "`n") -notmatch "young-router-modelprint-tables-ok") {
    throw "The bundled probe tables could not be verified: $ProbeSmoke"
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\workbuddy_stream.mjs"))) {
    throw "The Windows build output does not contain young_router/adapters/workbuddy_stream.mjs."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\connect-workbuddy\lib\index.js"))) {
    throw "The bundled dsh-connect-workbuddy package is missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\connect-qoder\lib\index.js"))) {
    throw "The bundled dsh-connect-qoder package is missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\qoder_worker.mjs"))) {
    throw "The Windows build output does not contain young_router/adapters/qoder_worker.mjs."
  }
  $QoderSurface = & (Join-Path $RuntimeBin "node.exe") (Join-Path $ProjectRoot "scripts\verify_connect_qoder.mjs") (Join-Path $Core "young_router\adapters\connect-qoder\lib\index.js") 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "The bundled dsh-connect-qoder package no longer exports the surface the worker uses: $QoderSurface"
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\our_free_model_worker.mjs"))) {
    throw "The Windows build output does not contain young_router/adapters/our_free_model_worker.mjs."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\our-free-model\src\core\runtime.js")) -or
      -not (Test-Path (Join-Path $Core "young_router\adapters\our-free-model\src\forward.js"))) {
    throw "The bundled Our Free Model sources are missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\dsh-vision-router\lib\core-primitives.js"))) {
    throw "The bundled dsh-vision-router package is missing."
  }
  if (-not (Test-Path (Join-Path $RuntimeBin "node.exe"))) {
    throw "The bundled Node.js runtime is missing."
  }
  $Python = Join-Path $RuntimeBin "python.exe"
  $PreviousPythonPath = $env:PYTHONPATH
  $PreviousProxyProcess = $env:YOUNG_ROUTER_PROXY_PROCESS
  try {
    $env:PYTHONPATH = $Core
    & $Python -c "import litellm.proxy.proxy_server, young_router.core, young_router.core.codex_config, young_router.config, young_router.core.configuration_package, young_router.core.external_provider_import, young_router.core.remote_usage_logs, young_router.core.runtime_settings_io, young_router.webdav.core"
    if ($LASTEXITCODE -ne 0) { throw "Bundled Windows Core import smoke test failed." }
    & (Join-Path $RuntimeBin "litellm.cmd") --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Bundled Windows LiteLLM launcher smoke test failed." }

    $PortableSmoke = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-portable-core-" + [guid]::NewGuid().ToString("N"))
    try {
      Copy-Item -LiteralPath $Core -Destination $PortableSmoke -Recurse -Force
      $PortablePython = Join-Path $PortableSmoke "runtime\bin\python.exe"
      $env:PYTHONPATH = $PortableSmoke
      & $PortablePython -c "import litellm.proxy.proxy_server, young_router.core"
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows Core import smoke test failed." }
      $env:YOUNG_ROUTER_PROXY_PROCESS = "1"
      & $PortablePython -c "from litellm.proxy.types_utils.utils import get_instance_fn; callback = get_instance_fn('young_router.callbacks.image_generation_routing_hook', config_file_path='runtime/config.yaml'); assert callback.__class__.__name__ == 'YoungRouterHook'"
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows callback smoke test failed." }
      & (Join-Path $PortableSmoke "runtime\bin\litellm.cmd") --help | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows LiteLLM launcher smoke test failed." }
    } finally {
      Remove-Item -LiteralPath $PortableSmoke -Recurse -Force -ErrorAction SilentlyContinue
    }
  } finally {
    $env:PYTHONPATH = $PreviousPythonPath
    $env:YOUNG_ROUTER_PROXY_PROCESS = $PreviousProxyProcess
  }

  $CoreRoot = (Resolve-Path $Core).Path
  # React Native Windows pins the Windows SDK to 10.0.22621.0 for a new
  # architecture project and only defers to a value already greater:
  #
  #   <WindowsTargetPlatformVersion Condition="'$(WindowsTargetPlatformVersion)'==''
  #       Or '$(WindowsTargetPlatformVersion)'=='10.0.0.0'">10.0.22621.0</...>
  #   <WindowsTargetPlatformVersion Condition="'$(RnwNewArch)'=='true' And
  #       $([MSBuild]::VersionLessThan('$(WindowsTargetPlatformVersion)', '10.0.22621.0'))">10.0.22621.0</...>
  #
  # The hosted Windows image installs exactly one SDK, and the newer image
  # (``windows-latest``) ships 10.0.26100.0 rather than 22621, so the build dies
  # in RNW's own Common.vcxproj:
  #
  #   error MSB8036: The Windows SDK version 10.0.22621.0 was not found.
  #
  # Any value at or above what RNW requires survives its condition, and a global
  # property overrides it for every project in the solution, including the ones
  # this repository does not own. The newest installed SDK is used, which keeps
  # the build working on a machine that has 22621 as well as the newer one.
  $InstalledSdk = Get-ChildItem -LiteralPath "${env:ProgramFiles(x86)}\Windows Kits\10\Include" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^10\.0\.\d+\.0$' } |
    Sort-Object { [version]$_.Name } -Descending |
    Select-Object -First 1
  if ($null -eq $InstalledSdk) {
    $InstalledSdk = Get-ChildItem -LiteralPath "${env:ProgramFiles}\Windows Kits\10\Include" -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match '^10\.0\.\d+\.0$' } |
      Sort-Object { [version]$_.Name } -Descending |
      Select-Object -First 1
  }
  if ($null -eq $InstalledSdk) {
    throw "No Windows SDK is installed under Windows Kits\\10\\Include."
  }
  Write-Output "Building against Windows SDK $($InstalledSdk.Name)."

  # RNW defaults this to false, which imports the ProjectReferences sheets and
  # compiles every React Native Windows translation unit in the solution -- over
  # half an hour here, and it also makes the package depend on a locally
  # generated Microsoft.ReactNative.pri.  True uses the published
  # Microsoft.ReactNative NuGet package: fast, and it brings a valid PRI.
  #
  # The Release sets UseBundle=true through RNW's Bundle.props. Passing --bundle
  # would select a ReleaseBundle solution configuration that this generated
  # Composition solution does not define.
  #
  # The properties are comma separated because that is the only separator the
  # CLI parses (``options.msbuildprops.split(',')``, then ``prop.split('=')``,
  # discarding anything that does not yield exactly two parts). A semicolon list
  # parses as zero properties and is dropped without a word.
  $RunWindowsArgs = @(
    "--dir", $AppRoot, "exec", "react-native", "run-windows",
    "--no-launch", "--no-deploy", "--no-packager", "--release", "--arch", $WindowsArchName,
    "--sln", "windows\YoungRouter.sln", "--proj", "windows\YoungRouter\YoungRouter.vcxproj",
    "--msbuildprops", "YoungRouterCoreStagingDir=$CoreRoot,RunCodegenWindows=false,WindowsTargetPlatformVersion=$($InstalledSdk.Name),RestoreIgnoreFailedSources=true,RestoreForce=false,UseExperimentalNuget=true"
  )
  $LowResourceBuild = $WindowsArch -eq "ARM64" -and $env:YOUNG_ROUTER_LOW_RESOURCE_BUILD -notin @("0", "false", "no")
  if ($LowResourceBuild) {
    $RunWindowsArgs += "--singleproc"
    $RunWindowsArgs[$RunWindowsArgs.IndexOf("--msbuildprops") + 1] += ",MultiProcessorCompilation=false,MultiProcCL=false"
  }
  & pnpm @RunWindowsArgs
  if ($LASTEXITCODE -ne 0) { throw "React Native Windows host build failed." }

  $WindowsRelease = Join-Path $AppRoot "windows\$WindowsArchName\Release"
  $PackagePayload = Join-Path $AppRoot "windows\YoungRouter.Package\bin\$WindowsArchName\Release\YoungRouter"
  New-Item -ItemType Directory -Force -Path $PackagePayload | Out-Null
  # Replacing the payload cannot overwrite an executable the running app holds:
  # a packaged run outlives the build, and the copy then fails halfway through
  # the artifact with "being used by another process".  The instance is this
  # tree's own output, so the build stops it and replaces it.
  $RunningPayload = Get-Process -Name YoungRouter -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($PackagePayload, [System.StringComparison]::OrdinalIgnoreCase) }
  foreach ($instance in $RunningPayload) {
    Write-Output "Stopping the running packaged app (pid $($instance.Id)) before replacing its payload."
    $instance | Stop-Process -Force -ErrorAction SilentlyContinue
  }
  if ($RunningPayload) { Start-Sleep -Milliseconds 800 }
  # Core is refreshed from the staged tree and copied into the payload below,
  # so taking the release's copy here would write the whole runtime twice.
  $ReleaseCore = Join-Path $WindowsRelease "Core"
  Get-ChildItem -LiteralPath $WindowsRelease -Force |
    Where-Object { $_.Name -ne "Core" } |
    ForEach-Object {
      Copy-Item -LiteralPath $_.FullName -Destination $PackagePayload -Recurse -Force
    }
  Remove-Item -LiteralPath (Join-Path $PackagePayload "Bundle\Bundle") -Recurse -Force -ErrorAction SilentlyContinue

  # The wheel fix lives in RNW source that upstream has not taken, so the shipped
  # binary has to be the prepared source-build output rather than the NuGet copy
  # the host just linked.  Both come from the same RNW version; the file version
  # check is what keeps a stale prebuild from being paired with a newer host.
  $PackagedReactNativeDll = Join-Path $PackagePayload "Microsoft.ReactNative.dll"
  if (-not (Test-Path -LiteralPath $PackagedReactNativeDll -PathType Leaf)) {
    throw "The Windows host did not produce $PackagedReactNativeDll."
  }
  if (-not $UseUpstreamWheel) {
    $PatchedReactNativeVersion = (Get-Item -LiteralPath $ReactNativeTargetDll).VersionInfo.FileVersion
    $PackagedReactNativeVersion = (Get-Item -LiteralPath $PackagedReactNativeDll).VersionInfo.FileVersion
    if (-not $PatchedReactNativeVersion -or $PatchedReactNativeVersion -ne $PackagedReactNativeVersion) {
      throw "The prebuilt RNW binary ($PatchedReactNativeVersion) does not match the packaged RNW ($PackagedReactNativeVersion)."
    }
    Copy-Item -LiteralPath $ReactNativeTargetDll -Destination $PackagedReactNativeDll -Force
  }

  # Replace both generated copies from the staged tree. Merging leaves old
  # Python package metadata and ARM64 extensions beside the x64 runtime.
  $StagedCoreSource = Join-Path $CoreRoot "young_router"
  Get-ChildItem -LiteralPath $StagedCoreSource -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $StagedCoreSource -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force
  $PayloadCore = Join-Path $PackagePayload "Core"
  foreach ($DestinationCore in @($ReleaseCore, $PayloadCore)) {
    if (Test-Path -LiteralPath $DestinationCore) {
      Remove-Item -LiteralPath $DestinationCore -Recurse -Force
    }
    Copy-CoreSource $CoreRoot $DestinationCore
  }

  # Launch the registered app from outside the Release tree so the launcher
  # cannot overwrite the WinUI executable reused by incremental builds.
  $LauncherSource = Join-Path $AppRoot "windows\YoungRouter\YoungRouterLauncher.cpp"
  $LauncherOutput = Join-Path (Split-Path -Parent $WindowsRelease) "YoungRouterLauncher.exe"
  if (-not (Test-Path $LauncherSource) -or -not (Test-Path (Split-Path $LauncherOutput))) {
    throw "Windows launcher source or output directory is missing."
  }
  $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $VsWhere)) {
    throw "Visual Studio vswhere.exe is required to compile the Windows launcher."
  }
  $VsInstallPath = (& $VsWhere -latest -products * -property installationPath).Trim()
  if ([string]::IsNullOrWhiteSpace($VsInstallPath)) {
    throw "Could not locate a Visual Studio installation for the Windows launcher."
  }
  $VcVarsArch = if ($WindowsArch -eq "ARM64") { "arm64" } else { "x64" }
  $VcVars = Join-Path $VsInstallPath "VC\Auxiliary\Build\vcvarsall.bat"
  $LauncherObject = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-launcher-" + $WindowsArchName + ".obj")
  $LauncherCommand = "call `"$VcVars`" $VcVarsArch && cl.exe /nologo /O2 /MT /DUNICODE /D_UNICODE /EHsc /Fo:`"$LauncherObject`" /Fe:`"$LauncherOutput`" `"$LauncherSource`" /link /SUBSYSTEM:WINDOWS shell32.lib user32.lib"
  & cmd.exe /d /s /c $LauncherCommand
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $LauncherOutput)) {
    throw "Could not compile the unpackaged Windows launcher."
  }
  Remove-Item -LiteralPath $LauncherObject -Force -ErrorAction SilentlyContinue
  Write-Output "Windows launcher: $LauncherOutput"

  $BundledPython = Get-ChildItem -LiteralPath (Join-Path $AppRoot "windows") -Recurse -File -Filter "python.exe" |
    Where-Object { $_.FullName -match '[\\/]Core[\\/]runtime[\\/]bin[\\/]python\.exe$' } |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
  if ($null -eq $BundledPython) {
    throw "The Windows build output does not contain Core/runtime/bin/python.exe."
  }
  $BundledBin = $BundledPython.Directory.FullName
  $BundledCore = (Resolve-Path (Join-Path $BundledBin "..\..")).Path
  $BundledLiteLLM = Join-Path $BundledBin "litellm.cmd"
  $BundledVersion = Join-Path $BundledCore "runtime\LITELLM_VERSION"
  if (-not (Test-Path $BundledLiteLLM)) {
    throw "The Windows build output does not contain Core/runtime/bin/litellm.cmd."
  }
  if (-not (Test-Path (Join-Path $BundledBin "node.exe"))) {
    throw "The Windows build output does not contain Core/runtime/bin/node.exe."
  }
  if (-not (Test-Path (Join-Path $BundledBin "bun.exe"))) {
    throw "The Windows build output does not contain Core/runtime/bin/bun.exe, so the Our Free Model lane cannot run."
  }
  if (-not (Test-Path $BundledVersion) -or (Get-Content -Raw $BundledVersion).Trim() -ne $LiteLLMVersion) {
    throw "The Windows build output does not contain the pinned LiteLLM release lock."
  }
  $PreviousPythonPath = $env:PYTHONPATH
  try {
    $env:PYTHONPATH = $BundledCore
    & $BundledPython.FullName -c "import litellm.proxy.proxy_server, young_router.core"
    if ($LASTEXITCODE -ne 0) { throw "Packaged Windows Core import smoke test failed." }
    & $BundledLiteLLM --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Packaged Windows LiteLLM launcher smoke test failed." }
  } finally {
    $env:PYTHONPATH = $PreviousPythonPath
  }
  Write-Output $BundledCore
} finally {
  if (Test-Path $Core) {
    Remove-Item -LiteralPath $Core -Recurse -Force -ErrorAction SilentlyContinue
  }
  # ``$NodeRuntime`` is the staging cache, not this build's work: deleting it
  # here is what made every run download Node and Bun again.
}
