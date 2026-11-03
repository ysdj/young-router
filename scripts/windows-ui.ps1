# Drive and photograph the installed Windows app.
#
# The Windows host draws React Native into a WinUI 3 window with a custom
# title bar, so there is no HTML to query and no AppleScript to run.  What is
# available is UI Automation: every shared UI text node and native control
# arrives as an element with a name and a bounding rectangle, and the settings
# panes are scrollable panes.  This is the Windows half of the "capture the
# target window and inspect the image" rule.
#
# Two traps cost a session each, so they are handled here:
#
#   * Windows PowerShell 5.1 decodes a script without a BOM as ANSI and decodes
#     `-File` arguments through the OEM code page, so a Chinese element name
#     passed from Git Bash arrives as mojibake and matches nothing.  Names are
#     therefore accepted as base64 of UTF-8 (`-NameB64`), and `dump` prints
#     UTF-8.  Generate one with: printf '%s' '<name>' | base64 -w0
#   * `$pid` is read-only in PowerShell, and a naive EnumWindows callback that
#     assigns it dies with "VariableNotWritable" while printing nothing.
#
# Usage:
#   windows-ui.ps1 dump [-Filter <regex>]      every named element, its rect
#   windows-ui.ps1 click -NameB64 <base64>     click the element's centre
#   windows-ui.ps1 clickxy -X <x> -Y <y>       click a screen point
#   windows-ui.ps1 wait -NameB64 <b64> [-TimeoutSeconds <n>]
#   windows-ui.ps1 panes                       scrollable panes and their state
#   windows-ui.ps1 scrollto -Percent <p>       scroll the tallest overflow pane
#   windows-ui.ps1 wheel -X <x> -Y <y> -Ticks <n>
#   windows-ui.ps1 move -X <x> -Y <y> -Width <w> -Height <h>
#   windows-ui.ps1 shot -Path <file.png>       photograph the app window only
#
# `scrollto` walks a pane with ScrollPattern increments; `SetScrollPercent` is
# accepted and ignored by the settings panes, and a synthetic wheel only reaches
# the composition island after a synthetic mouse *move* -- wheel alone is
# dropped -- so `wheel` sends both.
#
# `move` is the other way to bring a lower block into view, and the only one
# that needs no pane cooperation: a taller window shows the whole pane.
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Command,
  [string]$Filter = '',
  [string]$Name = '',
  [string]$NameB64 = '',
  [int]$X = 0,
  [int]$Y = 0,
  [int]$Ticks = 0,
  [int]$Width = 0,
  [int]$Height = 0,
  [double]$Percent = 0,
  [int]$TimeoutSeconds = 20,
  [string]$Path = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class YoungRouterWindow {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint to, bool attachInput);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool repaint);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[YoungRouterWindow]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

function Get-Name {
  if ($NameB64) { return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($NameB64)) }
  return $Name
}
function Get-AppWindow {
  $process = Get-Process YoungRouter -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
  if (-not $process) { throw 'YoungRouter is not running.' }
  if ($process.MainWindowHandle -eq [IntPtr]::Zero) { throw 'YoungRouter has no top-level window yet.' }
  return $process.MainWindowHandle
}
function Get-AppTree {
  return [System.Windows.Automation.AutomationElement]::FromHandle((Get-AppWindow))
}
function Find-Element([string]$wanted) {
  $tree = Get-AppTree
  $descendants = $tree.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $partial = $null
  foreach ($element in $descendants) {
    $candidate = $element.Current.Name
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    if ($candidate -eq $wanted) { return $element }
    if (-not $partial -and $candidate.Contains($wanted)) { $partial = $element }
  }
  return $partial
}
function Focus-App {
  # A background process may not call SetForegroundWindow: Windows hands the
  # click to whichever window already has focus, so a scripted click on an
  # unfocused app silently edits what the user is looking at.  Attaching to the
  # foreground thread lifts that restriction; the topmost hop is the fallback.
  $handle = Get-AppWindow
  if ([YoungRouterWindow]::IsIconic($handle)) { [YoungRouterWindow]::ShowWindow($handle, 9) | Out-Null }
  if ([YoungRouterWindow]::GetForegroundWindow() -ne $handle) {
    $foregroundThread = [YoungRouterWindow]::GetWindowThreadProcessId([YoungRouterWindow]::GetForegroundWindow(), [IntPtr]::Zero)
    $thisThread = [YoungRouterWindow]::GetCurrentThreadId()
    $attached = $false
    try {
      $attached = [YoungRouterWindow]::AttachThreadInput($thisThread, $foregroundThread, $true)
      [YoungRouterWindow]::BringWindowToTop($handle) | Out-Null
      [YoungRouterWindow]::SetForegroundWindow($handle) | Out-Null
    } finally {
      if ($attached) { [YoungRouterWindow]::AttachThreadInput($thisThread, $foregroundThread, $false) | Out-Null }
    }
    if ([YoungRouterWindow]::GetForegroundWindow() -ne $handle) {
      [YoungRouterWindow]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, 0x0043) | Out-Null
      [YoungRouterWindow]::SetForegroundWindow($handle) | Out-Null
      [YoungRouterWindow]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, 0x0043) | Out-Null
    }
  }
  Start-Sleep -Milliseconds 300
}
function Get-ScrollPanes {
  $tree = Get-AppTree
  $descendants = $tree.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $panes = @()
  foreach ($element in $descendants) {
    $pattern = $null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$pattern)) {
      $panes += , @($element, $pattern)
    }
  }
  return $panes
}

switch ($Command) {
  'dump' {
    $tree = Get-AppTree
    "window: '$($tree.Current.Name)' $($tree.Current.BoundingRectangle)"
    $descendants = $tree.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    "elements: $($descendants.Count)"
    foreach ($element in $descendants) {
      $label = $element.Current.Name
      if ([string]::IsNullOrWhiteSpace($label)) { continue }
      if ($Filter -and $label -notmatch $Filter) { continue }
      $rect = $element.Current.BoundingRectangle
      "{0,-8} '{1}' {2},{3} {4}x{5}" -f $element.Current.ControlType.ProgrammaticName.Replace('ControlType.', ''), $label, [int]$rect.Left, [int]$rect.Top, [int]$rect.Width, [int]$rect.Height
    }
  }
  'click' {
    $wanted = Get-Name
    Focus-App
    $element = Find-Element $wanted
    if (-not $element) { "not found: $wanted"; exit 1 }
    $rect = $element.Current.BoundingRectangle
    $x = [int]($rect.Left + $rect.Width / 2); $y = [int]($rect.Top + $rect.Height / 2)
    [YoungRouterWindow]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 120
    [YoungRouterWindow]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [YoungRouterWindow]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    "clicked '$wanted' at $x,$y"
  }
  'clickxy' {
    Focus-App
    [YoungRouterWindow]::SetCursorPos($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 120
    [YoungRouterWindow]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [YoungRouterWindow]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    "clicked $X,$Y"
  }
  'wheel' {
    Focus-App
    $delta = if ($Ticks -lt 0) { -120 } else { 120 }
    $handle = Get-AppWindow
    # The shell owns a full-screen XAML island of its own, so a window that is
    # merely foreground is not necessarily the one the cursor hits: hop to
    # topmost for the gesture and drop back afterwards.  The move matters too --
    # a composition island drops an injected wheel that no move preceded.
    [YoungRouterWindow]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, 0x0043) | Out-Null
    Start-Sleep -Milliseconds 200
    [YoungRouterWindow]::SetCursorPos($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 120
    [YoungRouterWindow]::mouse_event(0x0001, 1, 1, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [YoungRouterWindow]::mouse_event(0x0001, -1, -1, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 120
    for ($i = 0; $i -lt [math]::Abs($Ticks); $i++) {
      [YoungRouterWindow]::mouse_event(0x0800, 0, 0, $delta, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 40
    }
    Start-Sleep -Milliseconds 250
    [YoungRouterWindow]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, 0x0043) | Out-Null
    "wheeled $Ticks at $X,$Y"
  }
  'wait' {
    $wanted = Get-Name
    $started = Get-Date
    while (((Get-Date) - $started).TotalSeconds -lt $TimeoutSeconds) {
      if (Find-Element $wanted) { "found '$wanted' after {0:N1}s" -f ((Get-Date) - $started).TotalSeconds; exit 0 }
      Start-Sleep -Milliseconds 250
    }
    "not found '$wanted' after ${TimeoutSeconds}s"
    exit 1
  }
  'panes' {
    $panes = Get-ScrollPanes
    $index = 0
    foreach ($pane in $panes) {
      $rect = $pane[0].Current.BoundingRectangle
      $pattern = $pane[1]
      "pane $index rect=$([int]$rect.Left),$([int]$rect.Top) $([int]$rect.Width)x$([int]$rect.Height) vertical=$([math]::Round($pattern.Current.VerticalScrollPercent, 1)) view=$([math]::Round($pattern.Current.VerticalViewSize, 1))"
      $index++
    }
  }
  'scrollto' {
    $panes = Get-ScrollPanes
    if ($panes.Count -eq 0) { 'no scrollable pane'; exit 1 }
    $chosen = 0; $largest = -1
    for ($i = 0; $i -lt $panes.Count; $i++) {
      $rect = $panes[$i][0].Current.BoundingRectangle
      $area = $rect.Width * $rect.Height
      if ($panes[$i][1].Current.VerticalViewSize -lt 99.9 -and $area -gt $largest) { $chosen = $i; $largest = $area }
    }
    # `SetScrollPercent` is answered and ignored by these panes; the increment
    # call is the one that moves them, so the target is reached in steps.
    $pattern = $panes[$chosen][1]
    $noAmount = [System.Windows.Automation.ScrollAmount]::NoAmount
    $step = [System.Windows.Automation.ScrollAmount]::LargeIncrement
    $current = $pattern.Current.VerticalScrollPercent
    $start = $current
    if ($current -gt $Percent) { $step = [System.Windows.Automation.ScrollAmount]::LargeDecrement }
    for ($i = 0; $i -lt 40; $i++) {
      $current = $pattern.Current.VerticalScrollPercent
      if ([math]::Abs($current - $Percent) -le 3) { break }
      $pattern.Scroll($noAmount, $step)
      Start-Sleep -Milliseconds 120
      if (([math]::Abs($pattern.Current.VerticalScrollPercent - $current)) -lt 0.5) { break }
    }
    $after = [math]::Round($pattern.Current.VerticalScrollPercent, 1)
    if ([math]::Abs($after - $start) -lt 0.5) {
      "pane $chosen did not move from $start%: fall back to -Command move with a taller -Height."
      exit 1
    }
    "scrolled pane $chosen from $start% to $after% (asked for $Percent%; the pane reports no movement past that)"
  }
  'move' {
    $handle = Get-AppWindow
    [YoungRouterWindow]::MoveWindow($handle, $X, $Y, $Width, $Height, $true) | Out-Null
    Start-Sleep -Milliseconds 400
    "moved to $X,$Y ${Width}x${Height}"
  }
  'shot' {
    if (-not $Path) { throw 'shot needs -Path.' }
    Focus-App
    Start-Sleep -Milliseconds 350
    $handle = Get-AppWindow
    $rect = New-Object YoungRouterWindow+RECT
    [YoungRouterWindow]::GetWindowRect($handle, [ref]$rect) | Out-Null
    $bitmap = New-Object System.Drawing.Bitmap ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $bitmap.Dispose()
    "wrote $Path $($rect.Right - $rect.Left)x$($rect.Bottom - $rect.Top) at $($rect.Left),$($rect.Top)"
  }
  default { "unknown command: $Command"; exit 2 }
}
