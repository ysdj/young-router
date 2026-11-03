# Carry a shared-UI change into the running Windows app without a build.
#
# `pnpm run build:windows` is the deploy: it re-resolves four staged upstream
# releases, downloads the pinned LiteLLM and Node runtimes, stages a Core, and
# compiles the WinUI 3 host with MSBuild.  None of that is about a change to
# `rn/packages/shared/src`, which is JavaScript the Release host reads from
# `Bundle\index.windows.bundle` at startup -- write a new bundle into the
# installed package and restart the app.
#
# The Windows host cannot run pnpm from the Mac home share: Node resolves the
# share as a UNC path and pnpm 11 aborts with "current dir is an absolute path
# with drive letter", and Parallels cannot create pnpm's symlinks there.  So
# there are two trees with different jobs:
#
#   * the checkout on the share is the source of truth (`Source`, the
#     repository this script lives in);
#   * a copy on the VM's local disk owns `node_modules` and the build outputs
#     (`Tree`: `YOUNG_ROUTER_WINDOWS_TREE`, default `%USERPROFILE%\yr-win`).
#
# This script syncs the JavaScript surface and the Windows build helpers from
# the first into the second, bundles it into the registered package's own Bundle
# directory, and restarts the app.  Measured: 35 to 45 s, almost all of it
# Metro.
#
# What it refuses, and why:
#
#   * a native source newer than the installed host executable -- the exe
#     compiles it, so a copied file would leave a window that looks updated
#     while running the old C++.  `-AllowStaleNative` is the deliberate escape
#     for a JavaScript-only change made while native work is still in progress;
#     the run then states which files the window does not carry;
#   * a changed dependency or lockfile -- the tree needs its own
#     `pnpm install --frozen-lockfile --node-linker=hoisted` first.
#
#   scripts\dev-loop-windows.ps1                    sync, rebundle, restart
#   scripts\dev-loop-windows.ps1 -Check             report, change nothing
#   scripts\dev-loop-windows.ps1 -NoRestart         sync and rebundle only
#   scripts\dev-loop-windows.ps1 -AllowStaleNative  carry JavaScript only
#
# `scripts\windows-ui.ps1` is the other half: it dumps the window's UI
# Automation tree, clicks an element by name, and photographs the app window.
# A bundle that compiled is not evidence about the window; read it there.
#
# This never replaces the deploy: run `pnpm run build:windows` once the change
# is settled.
param(
  [switch]$Check,
  [switch]$NoRestart,
  [switch]$AllowStaleNative,
  [string]$Source = '',
  [string]$Tree = ''
)
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') { throw 'dev-loop-windows runs on a Windows host.' }
if (-not $Tree) {
  $Tree = if ($env:YOUNG_ROUTER_WINDOWS_TREE) { $env:YOUNG_ROUTER_WINDOWS_TREE }
    else { Join-Path $env:USERPROFILE 'yr-win' }
}
if (-not $Source) { $Source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$Source = (Resolve-Path -LiteralPath $Source).Path
if (-not (Test-Path -LiteralPath $Tree)) { throw "The Windows working copy is missing: $Tree" }
$Tree = (Resolve-Path -LiteralPath $Tree).Path

$TreeRn = Join-Path $Tree 'rn'
$AppRoot = Join-Path $TreeRn 'apps\windows'
if (-not (Test-Path -LiteralPath (Join-Path $TreeRn 'node_modules'))) {
  throw "The Windows working copy has no node_modules: $TreeRn. Run pnpm install --frozen-lockfile --node-linker=hoisted there first; it cannot live on the Mac share."
}
if (-not (Test-Path -LiteralPath (Join-Path $AppRoot 'windows'))) {
  throw "The React Native Windows host project is missing: $AppRoot\windows"
}

# Native surfaces whose content only the host executable can carry.  Build
# output that lives inside these trees (Bundle, ARM64, obj, codegen,
# AutolinkedNativeModules.g.*) is excluded: a build regenerates it, so reading
# it as source would report a tree as permanently stale.  So are the manifests,
# which `scripts/version.py` stamps from each checkout's own BUILD_NUMBER: two
# copies of the same source differ there by construction.
$GeneratedNames = @('Package.appxmanifest', 'AutolinkedNativeModules.g.*')
$NativeRoots = @(
  @{ Path = 'rn\apps\windows\src\native'; Exclude = @('Bundle', 'ARM64', 'x64', 'win32', 'obj', 'Generated Files', 'codegen') },
  @{ Path = 'rn\apps\windows\windows\YoungRouter'; Exclude = @('Bundle', 'ARM64', 'x64', 'win32', 'obj', 'Generated Files', 'codegen') },
  @{ Path = 'rn\apps\windows\windows\YoungRouter.Package'; Exclude = @('bin', 'obj', 'AppPackages', 'Bundle', 'codegen') },
  @{ Path = 'rn\packages\shared\src\ui\windows'; Exclude = @() }
)
$DependencyFiles = @(
  'rn\apps\windows\package.json',
  'rn\packages\shared\package.json',
  'rn\package.json',
  'rn\pnpm-lock.yaml'
)
$JsRoots = @('rn\packages\shared\src')
$JsFiles = @('rn\apps\windows\index.js', 'rn\apps\windows\babel.config.js', 'rn\apps\windows\metro.config.js')
$WindowsBuildFiles = @('rn\scripts\build-windows.ps1', 'rn\scripts\patch-react-native-windows.mjs')

function Get-ChangedFiles {
  param([string]$Left, [string]$Right, [string[]]$ExcludeDirectories = @())
  $changed = New-Object System.Collections.Generic.List[string]
  if (-not (Test-Path -LiteralPath $Left)) { return $changed }
  $leftBase = (Resolve-Path -LiteralPath $Left).Path
  foreach ($file in Get-ChildItem -LiteralPath $leftBase -Recurse -File -Force) {
    $relative = $file.FullName.Substring($leftBase.Length).TrimStart('\')
    $skip = $false
    foreach ($segment in ($relative -split '\\')) {
      if ($ExcludeDirectories -contains $segment) { $skip = $true; break }
    }
    if ($skip) { continue }
    foreach ($generated in $GeneratedNames) {
      if ($file.Name -like $generated) { $skip = $true; break }
    }
    if ($skip) { continue }
    $counterpart = Join-Path $Right $relative
    if (-not (Test-Path -LiteralPath $counterpart)) { $changed.Add($relative); continue }
    $other = Get-Item -LiteralPath $counterpart -Force
    if ($other.Length -ne $file.Length) { $changed.Add($relative); continue }
    if ($other.LastWriteTimeUtc -ne $file.LastWriteTimeUtc) {
      if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $counterpart -Algorithm SHA256).Hash) {
        $changed.Add($relative)
      }
    }
  }
  return $changed
}

$package = Get-AppxPackage -Name YoungRouter | Select-Object -First 1
$installDirectory = if ($package) { $package.InstallLocation } else { Join-Path $AppRoot 'windows\YoungRouter.Package\bin\ARM64\Release' }
$hostExe = Join-Path $installDirectory 'YoungRouter\YoungRouter.exe'
$bundleDirectory = if (Test-Path -LiteralPath (Join-Path $installDirectory 'YoungRouter\Bundle')) {
  Join-Path $installDirectory 'YoungRouter\Bundle'
} else {
  $arch = if ($env:YOUNG_ROUTER_WINDOWS_ARCH) { $env:YOUNG_ROUTER_WINDOWS_ARCH.Trim().ToUpperInvariant() } else { 'X64' }
  Join-Path $AppRoot "windows\YoungRouter.Package\bin\$arch\Release\YoungRouter\Bundle"
}
if (-not (Test-Path -LiteralPath $bundleDirectory)) { throw "No installed bundle directory at $bundleDirectory" }
$bundle = Join-Path $bundleDirectory 'index.windows.bundle'

# A native source only the host executable can carry.  Two facts make it
# stale, and both are read off the tree the exe was built in: the checkout's
# content differs from the tree's (the exe cannot contain the checkout's
# version), or the tree's own file is newer than the exe (copied in without a
# rebuild).  A checkout file that is merely newer -- touched, not changed -- is
# not a gap: the content already matches what the exe was linked from.
$nativeGaps = New-Object System.Collections.Generic.List[string]
$builtAt = if (Test-Path -LiteralPath $hostExe) { (Get-Item -LiteralPath $hostExe).LastWriteTimeUtc } else { [DateTime]::MinValue }
foreach ($root in $NativeRoots) {
  $leftRoot = Join-Path $Source $root.Path
  $rightRoot = Join-Path $Tree $root.Path
  foreach ($file in (Get-ChangedFiles -Left $leftRoot -Right $rightRoot -ExcludeDirectories $root.Exclude)) {
    $nativeGaps.Add("$(Join-Path $root.Path $file) (not in the tree the exe was built from)")
  }
  if (-not (Test-Path -LiteralPath $leftRoot)) { continue }
  $base = (Resolve-Path -LiteralPath $leftRoot).Path
  foreach ($file in Get-ChildItem -LiteralPath $base -Recurse -File -Force) {
    $relative = $file.FullName.Substring($base.Length).TrimStart('\')
    $skip = $false
    foreach ($segment in ($relative -split '\\')) {
      if ($root.Exclude -contains $segment) { $skip = $true; break }
    }
    if ($skip) { continue }
    foreach ($generated in $GeneratedNames) {
      if ($file.Name -like $generated) { $skip = $true; break }
    }
    if ($skip) { continue }
    $treeFile = Join-Path $rightRoot $relative
    if (-not (Test-Path -LiteralPath $treeFile)) { continue }
    if ((Get-Item -LiteralPath $treeFile -Force).LastWriteTimeUtc -gt $builtAt) {
      $nativeGaps.Add("$(Join-Path $root.Path $relative) (newer than the exe)")
    }
  }
}

$dependencyChanges = New-Object System.Collections.Generic.List[string]
foreach ($file in $DependencyFiles) {
  $left = Join-Path $Source $file
  $right = Join-Path $Tree $file
  if (-not (Test-Path -LiteralPath $left)) { continue }
  if (-not (Test-Path -LiteralPath $right)) { $dependencyChanges.Add("$file (missing in the tree)"); continue }
  if ((Get-FileHash -LiteralPath $left -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $right -Algorithm SHA256).Hash) {
    $dependencyChanges.Add("$file (dependency change)")
  }
}

$jsChanged = New-Object System.Collections.Generic.List[string]
foreach ($root in $JsRoots) {
  foreach ($file in (Get-ChangedFiles -Left (Join-Path $Source $root) -Right (Join-Path $Tree $root))) {
    $jsChanged.Add((Join-Path $root $file))
  }
}
foreach ($file in $JsFiles) {
  $left = Join-Path $Source $file
  $right = Join-Path $Tree $file
  if (-not (Test-Path -LiteralPath $left)) { continue }
  if (-not (Test-Path -LiteralPath $right)) { $jsChanged.Add($file); continue }
  if ((Get-FileHash -LiteralPath $left -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $right -Algorithm SHA256).Hash) { $jsChanged.Add($file) }
}

$windowsBuildChanged = New-Object System.Collections.Generic.List[string]
foreach ($file in $WindowsBuildFiles) {
  $left = Join-Path $Source $file
  $right = Join-Path $Tree $file
  if (-not (Test-Path -LiteralPath $left)) { continue }
  if (-not (Test-Path -LiteralPath $right)) { $windowsBuildChanged.Add($file); continue }
  if ((Get-FileHash -LiteralPath $left -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $right -Algorithm SHA256).Hash) { $windowsBuildChanged.Add($file) }
}

"source: $Source"
"tree:   $Tree"
"host:   $hostExe ($($builtAt.ToLocalTime()))"
"js changes: $($jsChanged.Count)"
foreach ($file in $jsChanged) { "  $file" }
"Windows build helper changes: $($windowsBuildChanged.Count)"
foreach ($file in $windowsBuildChanged) { "  $file" }

if ($dependencyChanges.Count -gt 0) {
  "dependency changes: $($dependencyChanges.Count)"
  foreach ($file in $dependencyChanges) { "  $file" }
  if (-not $Check) {
    throw @"
The tree's dependency tree does not match this checkout.  Install it in the
tree first (it cannot live on the share):
  cd $TreeRn
  pnpm install --frozen-lockfile --node-linker=hoisted
"@
  }
}

if ($nativeGaps.Count -gt 0) {
  "native sources the host executable cannot carry: $($nativeGaps.Count)"
  foreach ($file in $nativeGaps) { "  $file" }
}

$blocked = if ($nativeGaps.Count -gt 0 -and -not $AllowStaleNative) { $true } elseif ($dependencyChanges.Count -gt 0) { $true } else { $false }

if ($Check) {
  $stale = $jsChanged.Count -gt 0 -or $windowsBuildChanged.Count -gt 0 -or $nativeGaps.Count -gt 0 -or $dependencyChanges.Count -gt 0
  $newest = Get-ChildItem -LiteralPath (Join-Path $Tree 'rn\packages\shared\src') -Recurse -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
  if ($newest -and (Test-Path -LiteralPath $bundle)) {
    if ((Get-Item -LiteralPath $bundle).LastWriteTimeUtc -lt $newest.LastWriteTimeUtc) {
      "the installed bundle predates $($newest.Name)"
      $stale = $true
    }
  }
  if ($stale) { exit 1 }
  'the installed app carries this tree'
  exit 0
}

if ($blocked) {
  throw @"
The fast loop carries JavaScript only, and this checkout has work the running
host cannot show.  Build the artifact instead:
  cd $TreeRn
  pnpm run build:windows
(For a JavaScript-only change made while native work is in progress, rerun
this script with -AllowStaleNative and report the native gap.)
"@
}

if ($Source -ne $Tree -and ($jsChanged.Count -gt 0 -or $windowsBuildChanged.Count -gt 0)) {
  foreach ($root in $JsRoots) {
    $from = Join-Path $Source $root
    $to = Join-Path $Tree $root
    if (-not (Test-Path -LiteralPath $from)) { continue }
    & robocopy $from $to /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XF .DS_Store | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Could not sync $root (robocopy exit $LASTEXITCODE)." }
  }
  foreach ($file in $JsFiles) {
    $from = Join-Path $Source $file
    if (-not (Test-Path -LiteralPath $from)) { continue }
    Copy-Item -LiteralPath $from -Destination (Join-Path $Tree $file) -Force
  }
  foreach ($file in $WindowsBuildFiles) {
    $from = Join-Path $Source $file
    if (-not (Test-Path -LiteralPath $from)) { continue }
    Copy-Item -LiteralPath $from -Destination (Join-Path $Tree $file) -Force
  }
  "synced $($jsChanged.Count) JavaScript file(s) and $($windowsBuildChanged.Count) Windows build helper file(s) into the tree"
}

$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if (-not $pnpm) { throw 'pnpm is not on PATH.' }
Push-Location $AppRoot
try {
  & pnpm exec react-native bundle --platform windows --entry-file index.js --bundle-output $bundle --assets-dest $bundleDirectory --dev false --reset-cache --minify false
  if ($LASTEXITCODE -ne 0) { throw "Metro failed to build the bundle (exit $LASTEXITCODE)." }
} finally {
  Pop-Location
}
$bundleInfo = Get-Item -LiteralPath $bundle
"bundled $($bundleInfo.Length) bytes into $bundle"

if ($NoRestart) { 'left the running app alone (-NoRestart)'; exit 0 }
if (-not $package) { throw 'YoungRouter is not registered, so there is no app to restart.' }

Get-Process YoungRouter -ErrorAction SilentlyContinue | Stop-Process -Force
foreach ($name in 'python', 'node') {
  Get-Process $name -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($installDirectory, [System.StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force
}
Start-Sleep -Seconds 2
$started = Get-Date
Start-Process ("shell:AppsFolder\{0}!App" -f $package.PackageFamilyName)
$app = $null
while (((Get-Date) - $started).TotalSeconds -lt 60) {
  Start-Sleep -Milliseconds 300
  $app = Get-Process YoungRouter -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
  if ($app -and $app.MainWindowHandle -ne [IntPtr]::Zero) { break }
}
if (-not $app -or $app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'YoungRouter did not come back with a window.' }
# The handle appears before React has mounted anything, so this line is the
# launch being observable, not the pane: read the window with windows-ui.ps1.
Start-Sleep -Seconds 3
"restarted pid=$($app.Id) package=$($package.PackageFullName) after {0:N1}s" -f ((Get-Date) - $started).TotalSeconds
if ($nativeGaps.Count -gt 0) {
  "the host executable cannot carry $($nativeGaps.Count) native source(s); this window runs an earlier native build"
}
