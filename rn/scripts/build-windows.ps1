$ErrorActionPreference = "Stop"

$RnRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = (Resolve-Path (Join-Path $RnRoot "..")).Path
$AppRoot = Join-Path $RnRoot "apps\windows"
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  $Core = Join-Path ([System.IO.Path]::GetTempPath()) ("litellm-menu-rn-core-" + [guid]::NewGuid().ToString("N"))
} else {
  $Core = Join-Path $env:RUNNER_TEMP ("litellm-menu-rn-core-" + [guid]::NewGuid().ToString("N"))
}

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
  if (-not (Test-Path (Join-Path $AppRoot "windows"))) {
    throw "React Native Windows host project is missing at rn/apps/windows/windows."
  }

  pnpm run build
  pnpm run codegen:windows:check

  Remove-Item -LiteralPath $Core -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $Core | Out-Null
  foreach ($Name in @(
      "codex_config.py",
      "configuration_package.py",
      "external_provider_import.py",
      "remote_usage_logs.py",
      "runtime_settings_io.py",
      "sitecustomize.py")) {
    Copy-CoreSource (Join-Path $ProjectRoot $Name) (Join-Path $Core $Name)
  }
  foreach ($Name in @("litellm_menu", "config_editor_core", "webdav")) {
    Copy-CoreSource (Join-Path $ProjectRoot $Name) (Join-Path $Core $Name)
  }
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
  uv pip install --python (Join-Path $RuntimeBin "python.exe") `
    "litellm[proxy]==$LiteLLMVersion" "fastapi==0.140.3" PyYAML Pillow ddgs
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

  $Python = Join-Path $RuntimeBin "python.exe"
  $PreviousPythonPath = $env:PYTHONPATH
  $PreviousProxyProcess = $env:LITELLM_MENU_PROXY_PROCESS
  try {
    $env:PYTHONPATH = $Core
    & $Python -c "import litellm.proxy.proxy_server, litellm_menu.core, codex_config, config_editor_core, configuration_package, external_provider_import, webdav.core"
    if ($LASTEXITCODE -ne 0) { throw "Bundled Windows Core import smoke test failed." }
    & (Join-Path $RuntimeBin "litellm.cmd") --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Bundled Windows LiteLLM launcher smoke test failed." }

    $PortableSmoke = Join-Path ([System.IO.Path]::GetTempPath()) ("litellm-menu-portable-core-" + [guid]::NewGuid().ToString("N"))
    try {
      Copy-Item -LiteralPath $Core -Destination $PortableSmoke -Recurse -Force
      $PortablePython = Join-Path $PortableSmoke "runtime\bin\python.exe"
      $env:PYTHONPATH = $PortableSmoke
      & $PortablePython -c "import litellm.proxy.proxy_server, litellm_menu.core"
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows Core import smoke test failed." }
      $env:LITELLM_MENU_PROXY_PROCESS = "1"
      & $PortablePython -c "from litellm.proxy.types_utils.utils import get_instance_fn; callback = get_instance_fn('litellm_menu.callbacks.image_generation_routing_hook', config_file_path='runtime/config.yaml'); assert callback.__class__.__name__ == 'LiteLLMMenuHook'"
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows callback smoke test failed." }
      & (Join-Path $PortableSmoke "runtime\bin\litellm.cmd") --help | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "Relocated Windows LiteLLM launcher smoke test failed." }
    } finally {
      Remove-Item -LiteralPath $PortableSmoke -Recurse -Force -ErrorAction SilentlyContinue
    }
  } finally {
    $env:PYTHONPATH = $PreviousPythonPath
    $env:LITELLM_MENU_PROXY_PROCESS = $PreviousProxyProcess
  }

  $CoreRoot = (Resolve-Path $Core).Path
  # Release sets UseBundle=true through RNW's Bundle.props. Passing --bundle
  # would select a ReleaseBundle solution configuration that this generated
  # Composition solution does not define.
  pnpm --dir $AppRoot exec react-native run-windows --no-launch --no-deploy --no-packager --release --arch x64 `
    --sln "windows\LiteLLMMenu.sln" --proj "windows\LiteLLMMenu\LiteLLMMenu.vcxproj" `
    --msbuildprops "LiteLLMMenuCoreStagingDir=$CoreRoot;RunCodegenWindows=false"

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
  if (-not (Test-Path $BundledVersion) -or (Get-Content -Raw $BundledVersion).Trim() -ne $LiteLLMVersion) {
    throw "The Windows build output does not contain the pinned LiteLLM release lock."
  }
  $PreviousPythonPath = $env:PYTHONPATH
  try {
    $env:PYTHONPATH = $BundledCore
    & $BundledPython.FullName -c "import litellm.proxy.proxy_server, litellm_menu.core"
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
}
