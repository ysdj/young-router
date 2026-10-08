$ErrorActionPreference = "Stop"

$RnRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = (Resolve-Path (Join-Path $RnRoot "..")).Path
$AppRoot = Join-Path $RnRoot "apps\windows"
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  $Core = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
} else {
  $Core = Join-Path $env:RUNNER_TEMP ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
}
$PiNode = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-pi-node-" + [guid]::NewGuid().ToString("N"))
# Third-party staging lives in a persistent cache rather than a fresh GUID
# directory, for the same reason the macOS build does it: a build stages the
# same four releases over and over, each script resolves its upstream release
# on every run, and re-installing a release it has already staged is the
# largest remaining cost.  A tree is reused only when its own lookup resolves
# that exact release (see ``reused_staged_release`` in
# scripts/update_common.py), so deleting this directory is always safe.
$StagingCache = if ($env:YOUNG_ROUTER_STAGING_CACHE) {
  $env:YOUNG_ROUTER_STAGING_CACHE
} else {
  Join-Path $RnRoot ".staging-cache"
}
New-Item -ItemType Directory -Force -Path $StagingCache | Out-Null
$PiPackage = Join-Path $StagingCache "pi-web-access"
$VeridropWork = Join-Path $StagingCache "veridrop"
$WorkBuddyConnectWork = Join-Path $StagingCache "workbuddy-connect"
$DshVisionRouterWork = Join-Path $StagingCache "dsh-vision-router"

function Copy-CoreSource {
  param([string]$Source, [string]$Destination)
  if (-not (Test-Path $Source)) { throw "Bundled Core source is incomplete." }
  Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

try {
  Set-Location $RnRoot
  if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "pnpm is required to build the React Native Windows host."
  }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to build the self-contained Windows Core runtime."
  }
  & uv run --no-project --python 3.12 (Join-Path $ProjectRoot "scripts\update_litellm.py")
  if ($LASTEXITCODE -ne 0) { throw "Could not update LiteLLM to the latest stable release." }
  $PiWebAccessUpdater = Join-Path $ProjectRoot "scripts\update_pi_web_access.py"
  & uv run --no-project --python 3.12 $PiWebAccessUpdater `
    --output $PiPackage `
    --node-output $PiNode
  if ($LASTEXITCODE -ne 0) { throw "Could not update pi-web-access and the bundled Node.js runtime." }
  # The authenticity deep test runs the latest upstream Veridrop, so every
  # artifact build re-checks its default branch instead of packaging a stale
  # copy of the scan program.
  $VeridropUpdater = Join-Path $ProjectRoot "scripts\update_veridrop.py"
  & uv run --no-project --python 3.12 $VeridropUpdater --output $VeridropWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Veridrop scan program." }
  # WorkBuddy access is driven through the published third-party package, so
  # every artifact build re-resolves its latest release.
  $WorkBuddyConnectUpdater = Join-Path $ProjectRoot "scripts\update_workbuddy_connect.py"
  & uv run --no-project --python 3.12 $WorkBuddyConnectUpdater --output $WorkBuddyConnectWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-workbuddy-connect package." }
  # The vision fallback routes through the staged upstream package rather than a
  # copy of its chain, so every artifact build re-resolves its latest release.
  $DshVisionRouterUpdater = Join-Path $ProjectRoot "scripts\update_dsh_vision_router.py"
  & uv run --no-project --python 3.12 $DshVisionRouterUpdater --output $DshVisionRouterWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-vision-router package." }
  if (-not (Test-Path (Join-Path $AppRoot "windows"))) {
    throw "React Native Windows host project is missing at rn/apps/windows/windows."
  }

  pnpm run build

  Remove-Item -LiteralPath $Core -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $Core | Out-Null
  Copy-CoreSource (Join-Path $ProjectRoot "sitecustomize.py") (Join-Path $Core "sitecustomize.py")
  Copy-CoreSource (Join-Path $ProjectRoot "young_router") (Join-Path $Core "young_router")
  Copy-CoreSource $PiPackage (Join-Path $Core "young_router\adapters\pi-web-access")
  Copy-CoreSource $VeridropWork (Join-Path $Core "young_router\adapters\veridrop")
  Copy-CoreSource $WorkBuddyConnectWork (Join-Path $Core "young_router\adapters\workbuddy-connect")
  Copy-CoreSource $DshVisionRouterWork (Join-Path $Core "young_router\adapters\dsh-vision-router")
  Get-ChildItem -LiteralPath $Core -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $Core -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force

  $RuntimeBin = Join-Path $Core "runtime\bin"
  $PythonInstalls = Join-Path $Core ".python-installs"
  New-Item -ItemType Directory -Path $RuntimeBin, $PythonInstalls | Out-Null
  uv python install "cpython-3.12-windows-x86_64-none" --install-dir $PythonInstalls --no-bin
  $PythonSource = Get-ChildItem -LiteralPath $PythonInstalls -Directory |
    Where-Object { $_.Name -like "cpython-3.12*-windows-x86_64-none" } |
    Select-Object -First 1
  if ($null -eq $PythonSource -or -not (Test-Path (Join-Path $PythonSource.FullName "python.exe"))) {
    throw "uv did not install the expected standalone Windows x64 Python 3.12 runtime."
  }
  Copy-Item -Path (Join-Path $PythonSource.FullName "*") -Destination $RuntimeBin -Recurse -Force
  Remove-Item -LiteralPath $PythonInstalls -Recurse -Force

  $LiteLLMVersion = (Get-Content -Raw (Join-Path $ProjectRoot "LITELLM_VERSION")).Trim()
  # ``uv`` marks the standalone interpreter it installs as externally managed
  # (``EXTERNALLY-MANAGED`` in its ``lib/python3.12``), so installing *into* it
  # is refused:
  #
  #   error: The interpreter at ... is externally managed, and indicates the
  #   following: This Python installation is managed by uv and should not be
  #   modified.
  #
  # ``--target`` writes the packages into that interpreter's own site-packages
  # instead of asking it to accept an install, so the runtime keeps the layout
  # its wrappers and the staged Veridrop dependencies already use. The macOS
  # build has always installed this way, which is why the same refusal never
  # appeared there.
  $RuntimeSitePackages = & (Join-Path $RuntimeBin "python.exe") -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
  if ($LASTEXITCODE -ne 0 -or -not $RuntimeSitePackages) {
    throw "Could not resolve the bundled Windows runtime's site-packages path."
  }
  uv pip install --python (Join-Path $RuntimeBin "python.exe") `
    --target "$RuntimeSitePackages" `
    "litellm[proxy]==$LiteLLMVersion" "fastapi==0.140.3" PyYAML Pillow
  if ($LASTEXITCODE -ne 0) { throw "Could not install the bundled Windows runtime dependencies." }
  # The staged scan program is imported into the Core's own interpreter, so what
  # it imports and the bundled runtime does not already carry is added to that
  # runtime here - and only that: a dependency the runtime already has is never
  # upgraded under the release's own pins.
  $VeridropDeps = & (Join-Path $RuntimeBin "python.exe") (Join-Path $ProjectRoot "scripts\update_veridrop.py") `
    --output $VeridropWork --install-deps (Join-Path $RuntimeBin "Lib\site-packages") `
    --python (Join-Path $RuntimeBin "python.exe") 2>&1
  if ($LASTEXITCODE -ne 0) { throw "Could not complete the bundled Veridrop dependencies: $VeridropDeps" }
  Copy-Item -LiteralPath (Join-Path $PiNode "node.exe") -Destination $RuntimeBin -Force
  $GeneratedScripts = Join-Path $RuntimeBin "Scripts"
  if (Test-Path $GeneratedScripts) {
    Remove-Item -LiteralPath $GeneratedScripts -Recurse -Force
  }

  $LiteLLMCommand = @'
@echo off
setlocal
set "RUNTIME_ROOT=%~dp0"
"%RUNTIME_ROOT%python.exe" -c "from litellm import run_server; run_server()" %*
'@
  Set-Content -LiteralPath (Join-Path $RuntimeBin "litellm.cmd") -Value $LiteLLMCommand -Encoding ascii
  Copy-Item -LiteralPath (Join-Path $ProjectRoot "LITELLM_VERSION") -Destination (Join-Path $Core "runtime\LITELLM_VERSION") -Force
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\pi-web-access\index.ts"))) {
    throw "The bundled pi-web-access package is missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\veridrop\src\relay_detector\cli.py"))) {
    throw "The bundled Veridrop scan program is missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\veridrop\LICENSE"))) {
    throw "The bundled Veridrop license text is missing."
  }
  # The staged program is imported into the Core's own interpreter, so the
  # bundle proves that import - and the dependency set the runtime was
  # completed with - before the app is packaged.
  $VeridropRoot = Join-Path $Core "young_router\adapters\veridrop"
  $VeridropImport = & (Join-Path $RuntimeBin "python.exe") -c @"
import sys
sys.path.insert(0, r'$VeridropRoot\src')
import relay_detector.cli
import relay_detector.protocols.anthropic
import relay_detector.protocols.openai
import relay_detector.protocols.gemini
print('young-router-veridrop-import-ok')
"@ 2>&1
  if ($LASTEXITCODE -ne 0 -or ($VeridropImport -join "`n") -notmatch "young-router-veridrop-import-ok") {
    throw "The bundled Veridrop program could not be imported: $VeridropImport"
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\workbuddy_stream.mjs"))) {
    throw "The Windows build output does not contain young_router/adapters/workbuddy_stream.mjs."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\workbuddy-connect\lib\index.js"))) {
    throw "The bundled dsh-workbuddy-connect package is missing."
  }
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\dsh-vision-router\lib\core-primitives.js"))) {
    throw "The bundled dsh-vision-router package is missing."
  }
  if (-not (Test-Path (Join-Path $RuntimeBin "node.exe"))) {
    throw "The bundled Node.js runtime is missing."
  }
  $PiSmokeConfig = Join-Path $PiNode "smoke-config"
  "" | & (Join-Path $RuntimeBin "node.exe") `
    (Join-Path $Core "young_router\adapters\pi_web_access_worker.mjs") `
    --entry (Join-Path $Core "young_router\adapters\pi-web-access\index.ts") `
    --config-dir $PiSmokeConfig
  if ($LASTEXITCODE -ne 0) {
    throw "The bundled pi-web-access worker could not load its staged SDK."
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
  # Release sets UseBundle=true through RNW's Bundle.props. Passing --bundle
  # would select a ReleaseBundle solution configuration that this generated
  # Composition solution does not define.
  pnpm --dir $AppRoot exec react-native run-windows --no-launch --no-deploy --no-packager --release --arch x64 `
    --sln "windows\YoungRouter.sln" --proj "windows\YoungRouter\YoungRouter.vcxproj" `
    --msbuildprops "YoungRouterCoreStagingDir=$CoreRoot;RunCodegenWindows=false"

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
  if (-not (Test-Path (Join-Path $BundledCore "young_router\adapters\pi-web-access\index.ts"))) {
    throw "The Windows build output does not contain young_router/adapters/pi-web-access/index.ts."
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
  if (Test-Path $PiNode) {
    Remove-Item -LiteralPath $PiNode -Recurse -Force -ErrorAction SilentlyContinue
  }
}
