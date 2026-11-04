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
# The bundle itself is served by a Metro instance kept running between runs: a
# one-shot `react-native bundle` rebuilds the whole graph per edit (and its
# `--reset-cache` threw the transform cache away first), while a server answers
# the same bundle in 0-1 s and 3 s after an edit.  The first run after a Metro
# restart still pays the cold build once.
#
#   scripts\dev-loop-windows.ps1 -StopMetro      shut the server down again
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
  [switch]$StopMetro,
  [switch]$InitTree,
  [switch]$Install,
  [string]$Source = '',
  [string]$Tree = ''
)
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') { throw 'dev-loop-windows runs on a Windows host.' }
$TreeWasGiven = [bool]$Tree -or [bool]$env:YOUNG_ROUTER_WINDOWS_TREE
if (-not $Tree) {
  $Tree = if ($env:YOUNG_ROUTER_WINDOWS_TREE) { $env:YOUNG_ROUTER_WINDOWS_TREE }
    else { Join-Path $env:USERPROFILE 'yr-win' }
}
if (-not $Source) { $Source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$Source = (Resolve-Path -LiteralPath $Source).Path

# A shared folder is a reparse point (Parallels mounts one as a symlink to
# ``UNC\psf\Home``), and a checkout reached through one cannot hold node_modules:
# every ancestor is checked rather than the drive letter, because the share is
# mounted on a fixed drive that reports ``Fixed``.
function Test-SharedPath {
  param([string]$Path)
  if ($Path -match '^\\\\') { return $true }
  $current = $Path
  while ($current) {
    $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $true }
    $parent = Split-Path -Parent $current
    if (-not $parent -or $parent -eq $current) { break }
    $current = $parent
  }
  return $false
}

# The second tree exists for one reason: the checkout lives on a Parallels share,
# and pnpm cannot reliably build there.  A machine that has never built has no
# such copy, so ``-InitTree`` creates it from the checkout -- the same copy the
# share case always needed, made a command instead of a paragraph.  Guessing it
# from the path is not possible: a Parallels share is mounted on a fixed drive
# and reports ``Fixed``, so a wrong guess would build in place on a share that
# cannot hold it.
if (-not (Test-Path -LiteralPath $Tree)) {
  if ($InitTree) {
    if ($Source -eq $Tree) { throw "-InitTree needs a checkout and a tree that differ; both are $Source." }
    "creating the Windows working copy at $Tree from $Source"
    New-Item -ItemType Directory -Force -Path $Tree | Out-Null
    # Everything the build reads, and none of what it writes: node_modules is
    # installed in the tree (that is the point of the copy), the staging cache and
    # the build outputs belong to the machine rather than to the checkout, and the
    # macOS halves -- CocoaPods alone is 1.8 GB here -- are 2 GB a Windows build
    # would copy and never read.
    & robocopy $Source $Tree /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP `
      /XD node_modules .staging-cache .venv __pycache__ .pytest_cache Pods Bundle ARM64 x64 obj 'Generated Files' codegen AppPackages bin .git `
      /XF .DS_Store | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Could not copy the checkout into $Tree (robocopy exit $LASTEXITCODE)." }
    "created the tree; its dependencies are installed by -Install"
  } elseif (-not $TreeWasGiven -and -not (Test-SharedPath $Source)) {
    # A checkout on this machine's own disk needs no second copy: it is where the
    # tree would have been made.  This is the whole of a new machine's setup.
    "no copy at $Tree; debugging the checkout in place ($Source)"
    $Tree = $Source
  } else {
    throw @"
The Windows working copy is missing: $Tree

Create it from this checkout (it cannot live on the Mac share):
  scripts\dev-loop-windows.ps1 -InitTree        create the tree here
  scripts\dev-loop-windows.ps1 -Install         install its dependencies
Or pass -Tree to use a copy that already exists.
"@
  }
}
$Tree = (Resolve-Path -LiteralPath $Tree).Path

$TreeRn = Join-Path $Tree 'rn'
$AppRoot = Join-Path $TreeRn 'apps\windows'
if (-not (Test-Path -LiteralPath (Join-Path $TreeRn 'node_modules'))) {
  if ($Install) {
    "installing the tree's dependencies (pnpm install --frozen-lockfile --node-linker=hoisted)"
    Push-Location $TreeRn
    try {
      & pnpm install --frozen-lockfile --node-linker=hoisted
      if ($LASTEXITCODE -ne 0) { throw "pnpm install failed in $TreeRn (exit $LASTEXITCODE)." }
    } finally {
      Pop-Location
    }
  }
}
  if (-not (Test-Path -LiteralPath (Join-Path $TreeRn 'node_modules'))) {
    throw "The Windows working copy has no node_modules: $TreeRn. Rerun with -Install, or run pnpm install --frozen-lockfile --node-linker=hoisted there first; it cannot live on the Mac share."
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
  # The package payload directory carries the platform as MSBuild spells it:
  # ``x64``
  # and ``ARM64``, never an uppercased ``X64``.
  $arch = if ($env:YOUNG_ROUTER_WINDOWS_ARCH -eq 'ARM64') { 'ARM64' } else { 'x64' }
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

# --- the bundle --------------------------------------------------------------
# ``react-native bundle`` starts Metro, builds everything, and exits -- so each
# edit pays for the whole graph, and ``--reset-cache`` threw the transform cache
# away before it started (measured 109 s here).  A Metro server left running
# answers the same bundle in 0-1 s, and 3 s after an edit, because only the
# changed module is transformed.  The server is started on demand and reused by
# every later run; ``-StopMetro`` shuts it down.
$MetroPort = if ($env:YOUNG_ROUTER_METRO_PORT) { [int]$env:YOUNG_ROUTER_METRO_PORT } else { 8081 }
$MetroBase = "http://127.0.0.1:$MetroPort"
$MetroLog = Join-Path ([System.IO.Path]::GetTempPath()) "young-router-metro-$MetroPort.log"

function Test-MetroServer {
  try {
    $response = Invoke-WebRequest -Uri "$MetroBase/status" -UseBasicParsing -TimeoutSec 5
    return $response.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Get-MetroServerProcesses {
  Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.CommandLine -and
      $_.CommandLine -match 'react-native[\\/]cli\.js' -and
      $_.CommandLine -match 'start' -and
      $_.CommandLine -match "--port\s+$MetroPort(\s|$)"
    }
}

function Stop-MetroServer {
  # Two Metros sharing one cache directory wedge each other, and a server that
  # stopped answering is worse than none: the next run would wait out its
  # timeout and bundle twice.  One instance per port, always.
  $servers = @(Get-MetroServerProcesses)
  foreach ($server in $servers) { Stop-Process -Id $server.ProcessId -Force -ErrorAction SilentlyContinue }
  if ($servers.Count -gt 0) {
    $servers | ForEach-Object {
      Get-CimInstance Win32_Process -Filter "ParentProcessId = $($_.ProcessId)" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'node.exe' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 800
  }
  return $servers.Count
}

function Start-MetroServer {
  $cli = Join-Path $AppRoot 'node_modules\react-native\cli.js'
  if (-not (Test-Path -LiteralPath $cli)) { throw "The React Native CLI is missing at $cli." }
  "starting Metro on port $MetroPort (log: $MetroLog)"
  # Started straight from node with its output redirected: routed through a
  # shell that exits, the server loses the stream it writes progress to.
  Start-Process -FilePath (Get-Command node).Source -WorkingDirectory $AppRoot -WindowStyle Hidden `
    -ArgumentList @($cli, 'start', '--port', "$MetroPort") `
    -RedirectStandardOutput $MetroLog -RedirectStandardError "$MetroLog.err" | Out-Null
}

if ($StopMetro) {
  $stopped = Stop-MetroServer
  "stopped $stopped Metro server(s)"
  exit 0
}

$bundleSource = 'metro-server'
if (-not (Test-MetroServer)) {
  # A listening port that does not answer is the wedged case: replace it.
  Stop-MetroServer | Out-Null
  Start-MetroServer
  $deadline = (Get-Date).AddSeconds(90)
  while (-not (Test-MetroServer) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}

if (Test-MetroServer) {
  $bundleUrl = "$MetroBase/index.bundle?platform=windows&dev=false&minify=false"
  try {
    Invoke-WebRequest -Uri $bundleUrl -OutFile $bundle -UseBasicParsing -TimeoutSec 300
  } catch {
    throw "Metro did not answer the bundle request: $($_.Exception.Message)"
  }
  # The server appends how to find its own source map; the file host reads a
  # bundle, not a session, and the two trailing lines make the artifact it was
  # handed differ from the one ``react-native bundle`` writes -- which is what
  # the Release host aborts on, since it has no red box to show.  They point at
  # this server anyway, so the bundle is left exactly as the packager wrote it.
  $fetched = [System.IO.File]::ReadAllText($bundle)
  $trimmed = ($fetched -split "`n" | Where-Object { $_ -notmatch '^//# sourceMappingURL=' -and $_ -notmatch '^//# sourceURL=' }) -join "`n"
  if ($trimmed.Length -ne $fetched.Length) {
    [System.IO.File]::WriteAllText($bundle, $trimmed)
  }
  # ``--assets-dest`` is the one thing the HTTP response does not do: a file
  # bundle resolves an image from the Bundle directory, so an asset that changed
  # has to be copied beside it.  The packager mirrors ``rn\packages\...`` as
  # ``assets\__packages\...``, and a UI edit usually touches none of them -- the
  # copy compares content first and writes only what differs.  Only the two
  # source roots are walked: recursing ``rn\apps`` reads every file in
  # ``node_modules`` on each run, which cost more than the bundle it was
  # refreshing.
  $packagerRoot = Join-Path $Tree 'rn'
  $assetRoots = @((Join-Path $packagerRoot 'packages\shared\src'), (Join-Path $packagerRoot 'apps\windows\src'))
  foreach ($root in $assetRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($asset in Get-ChildItem -LiteralPath $root -Recurse -File -Include *.png, *.jpg, *.jpeg, *.gif, *.webp, *.ttf, *.otf -ErrorAction SilentlyContinue) {
      $relative = $asset.FullName.Substring($packagerRoot.Length).TrimStart('\')
      $target = Join-Path $bundleDirectory ("assets\__" + $relative)
      if ((Test-Path -LiteralPath $target) -and
          (Get-Item -LiteralPath $target).Length -eq $asset.Length) {
        if ((Get-FileHash -LiteralPath $target).Hash -eq (Get-FileHash -LiteralPath $asset.FullName).Hash) { continue }
      }
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
      Copy-Item -LiteralPath $asset.FullName -Destination $target -Force
    }
  }
} else {
  # No server and none could be started: the one-shot packager is still correct,
  # it just costs the whole graph.  Any half-started server is stopped first, so
  # the two never share a transform cache.
  Stop-MetroServer | Out-Null
  $bundleSource = 'react-native bundle'
  Push-Location $AppRoot
  try {
    & pnpm exec react-native bundle --platform windows --entry-file index.js --bundle-output $bundle --assets-dest $bundleDirectory --dev false --minify false
    if ($LASTEXITCODE -ne 0) { throw "Metro failed to build the bundle (exit $LASTEXITCODE)." }
  } finally {
    Pop-Location
  }
}
$bundleInfo = Get-Item -LiteralPath $bundle
"bundled $($bundleInfo.Length) bytes into $bundle ($bundleSource)"

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
# ``shell:AppsFolder`` needs the shell to resolve it: Windows PowerShell does
# that, PowerShell 7 does not unless it is told to.
$packageTarget = "shell:AppsFolder\{0}!App" -f $package.PackageFamilyName
try {
  Start-Process -FilePath $packageTarget -ErrorAction Stop
} catch {
  Start-Process -FilePath 'explorer.exe' -ArgumentList $packageTarget
}
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
