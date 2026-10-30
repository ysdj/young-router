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
$PythonTarget = if ($WindowsArch -eq "ARM64") {
  "cpython-3.12-windows-arm64-none"
} else {
  "cpython-3.12-windows-x86_64-none"
}
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  $Core = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
} else {
  $Core = Join-Path $env:RUNNER_TEMP ("young-router-rn-core-" + [guid]::NewGuid().ToString("N"))
}
$NodeRuntime = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-node-runtime-" + [guid]::NewGuid().ToString("N"))
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
$VeridropWork = Join-Path $StagingCache "veridrop"
$ConnectWorkBuddyWork = Join-Path $StagingCache "connect-workbuddy"
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
  & uv run --no-project --python 3.12 (Join-Path $ProjectRoot "scripts\update_litellm.py")
  if ($LASTEXITCODE -ne 0) { throw "Could not update LiteLLM to the latest stable release." }
  $NodeRuntimeUpdater = Join-Path $ProjectRoot "scripts\update_node_runtime.py"
  & uv run --no-project --python 3.12 $NodeRuntimeUpdater `
    --output $NodeRuntime
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Node.js runtime." }
  # The authenticity deep test runs the latest upstream Veridrop, so every
  # artifact build re-checks its default branch instead of packaging a stale
  # copy of the scan program.
  $VeridropUpdater = Join-Path $ProjectRoot "scripts\update_veridrop.py"
  & uv run --no-project --python 3.12 $VeridropUpdater --output $VeridropWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled Veridrop scan program." }
  # WorkBuddy access is driven through the published third-party package, so
  # every artifact build re-resolves its latest release.
  $ConnectWorkBuddyUpdater = Join-Path $ProjectRoot "scripts\update_connect_workbuddy.py"
  & uv run --no-project --python 3.12 $ConnectWorkBuddyUpdater --output $ConnectWorkBuddyWork
  if ($LASTEXITCODE -ne 0) { throw "Could not update the bundled dsh-connect-workbuddy package." }
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

  pnpm run build

  Remove-Item -LiteralPath $Core -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $Core | Out-Null
  Copy-CoreSource (Join-Path $ProjectRoot "sitecustomize.py") (Join-Path $Core "sitecustomize.py")
  Copy-CoreSource (Join-Path $ProjectRoot "young_router") (Join-Path $Core "young_router")
  Copy-CoreSource $VeridropWork (Join-Path $Core "young_router\adapters\veridrop")
  Copy-CoreSource $ConnectWorkBuddyWork (Join-Path $Core "young_router\adapters\connect-workbuddy")
  Copy-CoreSource $OurFreeModelWork (Join-Path $Core "young_router\adapters\our-free-model")
  Copy-CoreSource $DshVisionRouterWork (Join-Path $Core "young_router\adapters\dsh-vision-router")
  Get-ChildItem -LiteralPath $Core -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $Core -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force

  $RuntimeBin = Join-Path $Core "runtime\bin"
  $PythonInstalls = Join-Path $Core ".python-installs"
  New-Item -ItemType Directory -Path $RuntimeBin, $PythonInstalls | Out-Null
  $RuntimeSeed = $env:YOUNG_ROUTER_RUNTIME_SEED
  if ($RuntimeSeed -and (Test-Path (Join-Path $RuntimeSeed "bin\python.exe"))) {
    Copy-Item -Path (Join-Path $RuntimeSeed "bin\*") -Destination $RuntimeBin -Recurse -Force
  } else {
    uv python install $PythonTarget --install-dir $PythonInstalls --no-bin
    $PythonSource = Get-ChildItem -LiteralPath $PythonInstalls -Directory |
      Where-Object { $_.Name -like "cpython-3.12*-windows-*" } |
      Select-Object -First 1
    if ($null -eq $PythonSource -or -not (Test-Path (Join-Path $PythonSource.FullName "python.exe"))) {
      throw "uv did not install the expected standalone Windows $WindowsArch Python 3.12 runtime."
    }
    Copy-Item -Path (Join-Path $PythonSource.FullName "*") -Destination $RuntimeBin -Recurse -Force
  }
  Remove-Item -LiteralPath $PythonInstalls -Recurse -Force -ErrorAction SilentlyContinue

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
  if ($RuntimeSeed) {
    $SeedCheck = & (Join-Path $RuntimeBin "python.exe") -c "from importlib.metadata import version; print(version('litellm'))"
    if ($LASTEXITCODE -ne 0) {
      throw "The prebuilt runtime seed does not carry LiteLLM metadata."
    }
    if ($SeedCheck.Trim() -ne $LiteLLMVersion) {
      # Keep the large native/Python seed reusable while refreshing the small
      # fast-moving LiteLLM layer when its release lock advances.
      Write-Output "Updating LiteLLM in the prebuilt runtime seed: $($SeedCheck.Trim()) -> $LiteLLMVersion."
      uv pip install --python (Join-Path $RuntimeBin "python.exe") --target "$RuntimeSitePackages" --upgrade "litellm[proxy]==$LiteLLMVersion" "fastapi==0.140.3" PyYAML
      if ($LASTEXITCODE -ne 0) { throw "Could not update LiteLLM in the prebuilt runtime seed." }
    }
  } else {
    uv pip install --python (Join-Path $RuntimeBin "python.exe") `
      --target "$RuntimeSitePackages" `
      "litellm[proxy]==$LiteLLMVersion" "fastapi==0.140.3" PyYAML
    if ($LASTEXITCODE -ne 0) { throw "Could not install the bundled Windows runtime dependencies." }
  }
  # The staged scan program is imported into the Core's own interpreter, so what
  # it imports and the bundled runtime does not already carry is added to that
  # runtime here - and only that: a dependency the runtime already has is never
  # upgraded under the release's own pins.
  $VeridropDeps = & (Join-Path $RuntimeBin "python.exe") (Join-Path $ProjectRoot "scripts\update_veridrop.py") `
    --output $VeridropWork --install-deps (Join-Path $RuntimeBin "Lib\site-packages") `
    --python (Join-Path $RuntimeBin "python.exe") 2>&1
  if ($LASTEXITCODE -ne 0) { throw "Could not complete the bundled Veridrop dependencies: $VeridropDeps" }
  Copy-Item -LiteralPath (Join-Path $NodeRuntime "node.exe") -Destination $RuntimeBin -Force
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
  if (-not (Test-Path (Join-Path $Core "young_router\adapters\connect-workbuddy\lib\index.js"))) {
    throw "The bundled dsh-connect-workbuddy package is missing."
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

  # Release sets UseBundle=true through RNW's Bundle.props. Passing --bundle
  # would select a ReleaseBundle solution configuration that this generated
  # Composition solution does not define.
  #
  # The properties are comma separated because that is the only separator the
  # CLI parses (``options.msbuildprops.split(',')``, then ``prop.split('=')``,
  # discarding anything that does not yield exactly two parts). A semicolon list
  # parses as zero properties and is dropped without a word.
  $RunWindowsArgs = @(
    "--dir", $AppRoot, "exec", "react-native", "run-windows",
    "--no-launch", "--no-deploy", "--no-packager", "--release", "--arch", $WindowsArch,
    "--sln", "windows\YoungRouter.sln", "--proj", "windows\YoungRouter\YoungRouter.vcxproj",
    "--msbuildprops", "YoungRouterCoreStagingDir=$CoreRoot,RunCodegenWindows=false,WindowsTargetPlatformVersion=$($InstalledSdk.Name),RestoreIgnoreFailedSources=true,RestoreForce=false"
  )
  $LowResourceBuild = $WindowsArch -eq "ARM64" -and $env:YOUNG_ROUTER_LOW_RESOURCE_BUILD -notin @("0", "false", "no")
  if ($LowResourceBuild) {
    $RunWindowsArgs += "--singleproc"
    $RunWindowsArgs[$RunWindowsArgs.IndexOf("--msbuildprops") + 1] += ",MultiProcessorCompilation=false,MultiProcCL=false"
  }
  & pnpm @RunWindowsArgs

  $WindowsRelease = Join-Path $AppRoot "windows\$WindowsArch\Release"
  $PackagePayload = Join-Path $AppRoot "windows\YoungRouter.Package\bin\$WindowsArch\Release\YoungRouter"
  New-Item -ItemType Directory -Force -Path $PackagePayload | Out-Null
  Copy-Item -Path (Join-Path $WindowsRelease "*") -Destination $PackagePayload -Recurse -Force
  Remove-Item -LiteralPath (Join-Path $PackagePayload "Bundle\Bundle") -Recurse -Force -ErrorAction SilentlyContinue

  # The WinUI host links Windows App SDK and can fail before WinMain when a
  # user double-clicks the unpackaged build-output EXE on ARM64. Keep the
  # packaged host produced above, then replace only that convenient raw-output
  # path with a tiny launcher that hands off to the installed MSIX.
  $LauncherSource = Join-Path $AppRoot "windows\YoungRouter\YoungRouterLauncher.cpp"
  $LauncherOutput = Join-Path $WindowsRelease "YoungRouter.exe"
  if (-not (Test-Path $LauncherSource) -or -not (Test-Path (Split-Path $LauncherOutput))) {
    throw "Windows launcher source or Release output directory is missing."
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
  $LauncherObject = Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-launcher-" + $WindowsArch + ".obj")
  $LauncherCommand = "call `"$VcVars`" $VcVarsArch && cl.exe /nologo /O2 /MT /DUNICODE /D_UNICODE /EHsc /Fo:`"$LauncherObject`" /Fe:`"$LauncherOutput`" `"$LauncherSource`" /link /SUBSYSTEM:WINDOWS shell32.lib user32.lib"
  & cmd.exe /d /s /c $LauncherCommand
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $LauncherOutput)) {
    throw "Could not compile the unpackaged Windows launcher."
  }
  Remove-Item -LiteralPath $LauncherObject -Force -ErrorAction SilentlyContinue
  Write-Output "Windows raw-output launcher: $LauncherOutput"

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
  if (Test-Path $NodeRuntime) {
    Remove-Item -LiteralPath $NodeRuntime -Recurse -Force -ErrorAction SilentlyContinue
  }
}
