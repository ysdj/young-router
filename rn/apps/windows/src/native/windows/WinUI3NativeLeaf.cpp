#include "pch.h"
#include "WinUI3NativeLeaf.h"
#include "CoreIPCBridge.h"

#include <windows.h>
#include <dwmapi.h>
#include <shlobj.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cwctype>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <thread>
#include <winreg.h>
#include <winver.h>
#include <winrt/Windows.ApplicationModel.h>
#include <winrt/Windows.ApplicationModel.DataTransfer.h>
#include <winrt/Windows.UI.h>
#include <winrt/Windows.System.h>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Microsoft.UI.Interop.h>
#include <winrt/Microsoft.UI.Windowing.h>
#include <winrt/Microsoft.UI.Xaml.Automation.h>
#include <winrt/Microsoft.UI.Xaml.Input.h>
#include <winrt/Microsoft.Web.WebView2.Core.h>

namespace {
constexpr UINT kTrayMessage = WM_APP + 31;
constexpr UINT kQuitMessage = WM_APP + 32;
constexpr UINT kTrayMenuFirstCommand = 41000;
constexpr double kUIFontSize = 13.0;

// Plaintext relay keys this app already read, keyed by "account:resource".
// The sheet is rebuilt every time it opens and Core's lease is read-once and
// process-local, so the host remembers what a sheet read: the next sheet shows
// those keys at once instead of an empty value while its own read runs.  The
// memo is bounded and a key whose read is empty is forgotten.
std::mutex &RelayKeyMemoLock() {
  static std::mutex lock;
  return lock;
}

std::map<std::wstring, std::wstring> &RelayKeyMemo() {
  static std::map<std::wstring, std::wstring> memo;
  return memo;
}

std::wstring RelayKeyMemoEntry(std::wstring const& account_id, std::wstring const& resource_id) {
  return account_id + L"\x1f" + resource_id;
}

void RememberRelayKey(std::wstring const& account_id, std::wstring const& resource_id, std::wstring const& value) {
  if (account_id.empty() || resource_id.empty()) return;
  std::lock_guard<std::mutex> guard(RelayKeyMemoLock());
  auto& memo = RelayKeyMemo();
  const std::wstring entry = RelayKeyMemoEntry(account_id, resource_id);
  if (value.empty()) {
    memo.erase(entry);
    return;
  }
  memo[entry] = value;
  while (memo.size() > 1024) memo.erase(memo.begin());
}

std::wstring RememberedRelayKey(std::wstring const& account_id, std::wstring const& resource_id) {
  std::lock_guard<std::mutex> guard(RelayKeyMemoLock());
  auto const& memo = RelayKeyMemo();
  auto found = memo.find(RelayKeyMemoEntry(account_id, resource_id));
  return found == memo.end() ? std::wstring{} : found->second;
}

namespace web = winrt::Microsoft::Web::WebView2::Core;

struct ReadOnlyCodeViewerState {
  winrt::Microsoft::UI::Xaml::Window dialog{nullptr};
  winrt::Microsoft::UI::Xaml::Controls::WebView2 webview{nullptr};
  web::CoreWebView2 core{nullptr};
  winrt::event_token activated_token{};
  winrt::event_token navigation_completed_token{};
  winrt::event_token web_message_token{};
  bool started = false;
  bool command_sent = false;
  bool finished = false;
  bool failed = false;
};

struct ContentSize {
  LONG width;
  LONG height;
};

ContentSize RouteMinimumContentSize(std::wstring_view route) {
  // These are the legacy window content sizes in 96-DPI logical pixels. They
  // deliberately live at the native window boundary: React owns the shared
  // page, while Win32 owns frame constraints and DPI conversion.
  // Every settings pane shares one window, so it uses one minimum size wide
  // enough for the sidebar plus the widest pane.
  if (route == L"general-settings" || route == L"providers-models" || route == L"codex-settings" ||
      route == L"claude-settings" || route == L"runtime-settings" || route == L"data-management" ||
      route == L"logs") {
    return {900, 560};
  }
  if (route == L"provider-wizard") return {540, 420};
  if (route == L"file-editor") return {900, 560};
  // The hidden menu-bar host has no route surface. Keep its fallback small so
  // it never inherits a settings window's minimum size before a route opens.
  return {320, 160};
}

ContentSize RouteInitialContentSize(std::wstring_view route) {
  if (route == L"general-settings" || route == L"providers-models" || route == L"codex-settings" ||
      route == L"claude-settings" || route == L"runtime-settings" || route == L"data-management" ||
      route == L"logs") {
    return {960, 640};
  }
  if (route == L"provider-wizard") return {620, 460};
  if (route == L"file-editor") return {900, 560};
  return {320, 160};
}

LONG DipToPhysicalPixels(LONG value, UINT dpi) {
  return MulDiv(value, static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI);
}

POINT FrameTrackSizeForContent(HWND window, ContentSize content) {
  const UINT window_dpi = window == nullptr ? USER_DEFAULT_SCREEN_DPI : GetDpiForWindow(window);
  const UINT dpi = window_dpi == 0 ? USER_DEFAULT_SCREEN_DPI : window_dpi;
  RECT frame{
      0,
      0,
      DipToPhysicalPixels(content.width, dpi),
      DipToPhysicalPixels(content.height, dpi),
  };
  if (window == nullptr) return {frame.right - frame.left, frame.bottom - frame.top};

  const auto style = static_cast<DWORD>(GetWindowLongPtrW(window, GWL_STYLE));
  const auto extended_style = static_cast<DWORD>(GetWindowLongPtrW(window, GWL_EXSTYLE));
  const auto has_menu = GetMenu(window) != nullptr;
  if (!AdjustWindowRectExForDpi(&frame, style, has_menu, extended_style, dpi)) {
    return {frame.right - frame.left, frame.bottom - frame.top};
  }
  return {frame.right - frame.left, frame.bottom - frame.top};
}

LRESULT CALLBACK TrayWindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
  auto leaf = reinterpret_cast<YoungRouter::WinUI3NativeLeaf*>(GetPropW(window, L"YoungRouter.NativeLeaf"));
  auto previous = leaf ? reinterpret_cast<WNDPROC>(GetPropW(window, L"YoungRouter.PreviousWindowProc")) : nullptr;
  if (leaf) {
    if (message == WM_GETMINMAXINFO) {
      LRESULT result = previous
          ? CallWindowProcW(previous, window, message, wparam, lparam)
          : DefWindowProcW(window, message, wparam, lparam);
      auto minmax = reinterpret_cast<MINMAXINFO*>(lparam);
      if (minmax != nullptr) {
        const auto minimum = leaf->MinimumTrackSizeForActiveRoute();
        minmax->ptMinTrackSize.x = std::max(minmax->ptMinTrackSize.x, minimum.x);
        minmax->ptMinTrackSize.y = std::max(minmax->ptMinTrackSize.y, minimum.y);
      }
      return result;
    }
    if (leaf->HandleWindowMessage(message, wparam, lparam)) return 0;
  }
  return previous ? CallWindowProcW(previous, window, message, wparam, lparam) : DefWindowProcW(window, message, wparam, lparam);
}

std::wstring ModulePath() {
  std::vector<wchar_t> path(32768, L'\0');
  DWORD length = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
  return length > 0 && length < path.size() ? std::wstring(path.data(), length) : std::wstring{};
}

std::wstring CodeEditorWebViewDataFolder() {
  PWSTR folder = nullptr;
  if (FAILED(SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_CREATE, nullptr, &folder)) || folder == nullptr) {
    return {};
  }
  std::filesystem::path path(folder);
  CoTaskMemFree(folder);
  path /= L"Young Router";
  path /= L"CodeEditorWebView2";
  std::error_code error;
  std::filesystem::create_directories(path, error);
  return error ? std::wstring{} : path.wstring();
}

std::wstring Utf8ToWide(std::string const& value) {
  if (value.empty()) return {};
  int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
  if (count <= 0) return {};
  std::wstring result(static_cast<size_t>(count), L'\0');
  MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), count);
  return result;
}

// The sheet receives one line per model, so a name may never contain a line
// break of its own; Core rejects control characters in model names.
std::vector<std::wstring> SplitLines(std::wstring const& value) {
  std::vector<std::wstring> lines;
  size_t start = 0;
  while (start <= value.size()) {
    auto end = value.find(L'\n', start);
    if (end == std::wstring::npos) end = value.size();
    if (end > start) lines.push_back(value.substr(start, end - start));
    if (end == value.size()) break;
    start = end + 1;
  }
  return lines;
}

// 密钥值 shows one line: keep the head and the tail that identify the key.
// The value behind it stays whole, and the copy action hands over all of it.
std::wstring EllipsizeMiddle(std::wstring const& value) {
  constexpr size_t kHead = 14;
  constexpr size_t kTail = 10;
  if (value.size() <= kHead + kTail + 1) return value;
  return value.substr(0, kHead) + L"\u2026" + value.substr(value.size() - kTail);
}

std::string WideToUtf8(std::wstring const& value) {
  if (value.empty()) return {};
  int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  if (count <= 0) return {};
  std::string result(static_cast<size_t>(count), '\0');
  WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), count, nullptr, nullptr);
  return result;
}

std::wstring FoldModelSearchText(std::wstring value) {
  std::transform(value.begin(), value.end(), value.begin(), [](wchar_t character) {
    return static_cast<wchar_t>(std::towlower(character));
  });
  return value;
}

bool RunOwnedModalWindow(
    winrt::Microsoft::UI::Xaml::Window const& dialog,
    HWND owner,
    winrt::Windows::Graphics::SizeInt32 content_size_dips,
    bool& finished) {
  HWND dialog_handle = nullptr;
  winrt::check_hresult(dialog.as<::IWindowNative>()->get_WindowHandle(&dialog_handle));
  YoungRouter::DisableWindowTransitions(dialog_handle);
  const bool disable_owner = owner != nullptr && IsWindow(owner);
  if (disable_owner) {
    SetWindowLongPtrW(dialog_handle, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(owner));
    EnableWindow(owner, FALSE);
  }

  auto window_id = winrt::Microsoft::UI::GetWindowIdFromWindow(dialog_handle);
  const auto frame = YoungRouter::FrameTrackSizeForContentDips(
      dialog_handle, content_size_dips.Width, content_size_dips.Height);
  winrt::Microsoft::UI::Windowing::AppWindow::GetFromWindowId(window_id).Resize({frame.x, frame.y});
  dialog.Activate();

  MSG message{};
  BOOL message_result = TRUE;
  while (!finished && (message_result = GetMessageW(&message, nullptr, 0, 0)) > 0) {
    TranslateMessage(&message);
    DispatchMessageW(&message);
  }
  if (!finished) {
    try {
      dialog.Close();
    } catch (...) {
    }
  }
  if (disable_owner && IsWindow(owner)) {
    EnableWindow(owner, TRUE);
    SetForegroundWindow(owner);
  }
  if (message_result == 0) PostQuitMessage(static_cast<int>(message.wParam));
  return finished && message_result >= 0;
}

void FailReadOnlyCodeViewer(std::weak_ptr<ReadOnlyCodeViewerState> const& weak_state) noexcept {
  if (auto state = weak_state.lock(); state && !state->finished) {
    state->failed = true;
    try {
      state->dialog.Close();
    } catch (...) {
      state->finished = true;
    }
  }
}

winrt::fire_and_forget SendReadOnlyCodeViewerCommand(
    std::weak_ptr<ReadOnlyCodeViewerState> weak_state,
    winrt::hstring script) {
  auto state = weak_state.lock();
  if (!state || state->finished || !state->webview) co_return;
  try {
    co_await state->webview.ExecuteScriptAsync(script);
  } catch (...) {
    FailReadOnlyCodeViewer(weak_state);
  }
}

winrt::fire_and_forget InitializeReadOnlyCodeViewer(
    std::weak_ptr<ReadOnlyCodeViewerState> weak_state,
    winrt::hstring html,
    winrt::hstring text,
    winrt::hstring language) {
  auto state = weak_state.lock();
  if (!state || state->finished || !state->webview) co_return;
  try {
    const auto data_folder = CodeEditorWebViewDataFolder();
    if (data_folder.empty()) throw winrt::hresult_error(E_FAIL);
    web::CoreWebView2EnvironmentOptions environment_options;
    auto environment = co_await web::CoreWebView2Environment::CreateWithOptionsAsync(
        winrt::hstring{}, winrt::hstring(data_folder), environment_options);
    state = weak_state.lock();
    if (!state || state->finished || !state->webview) co_return;
    auto controller_options = environment.CreateCoreWebView2ControllerOptions();
    co_await state->webview.EnsureCoreWebView2Async(environment, controller_options);
    state = weak_state.lock();
    if (!state || state->finished || !state->webview) co_return;

    state->core = state->webview.CoreWebView2();
    auto payload = winrt::Windows::Data::Json::JsonObject{};
    payload.Insert(L"type", winrt::Windows::Data::Json::JsonValue::CreateStringValue(winrt::hstring(L"replace")));
    payload.Insert(L"documentKey", winrt::Windows::Data::Json::JsonValue::CreateStringValue(winrt::hstring(L"readonly")));
    payload.Insert(L"value", winrt::Windows::Data::Json::JsonValue::CreateStringValue(text));
    payload.Insert(L"baseline", winrt::Windows::Data::Json::JsonValue::CreateStringValue(text));
    payload.Insert(L"language", winrt::Windows::Data::Json::JsonValue::CreateStringValue(language));
    payload.Insert(L"readOnly", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(true));
    payload.Insert(L"showDiff", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(false));
    auto script = winrt::hstring(L"window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(") +
        payload.Stringify() + L");";
    state->web_message_token = state->core.WebMessageReceived(
        [weak_state, script = std::move(script)](auto const&, auto const& args) {
          auto current = weak_state.lock();
          if (!current || current->finished || current->command_sent) return;
          try {
            auto message = winrt::Windows::Data::Json::JsonObject::Parse(args.TryGetWebMessageAsString());
            if (message.GetNamedString(L"type", winrt::hstring{}) != L"ready") return;
            current->command_sent = true;
            SendReadOnlyCodeViewerCommand(weak_state, script);
          } catch (...) {
            FailReadOnlyCodeViewer(weak_state);
          }
        });
    state->webview.NavigateToString(html);
  } catch (...) {
    FailReadOnlyCodeViewer(weak_state);
  }
}
}  // namespace

namespace YoungRouter {

void DisableWindowTransitions(HWND window) noexcept {
  if (window == nullptr) return;
  const BOOL disabled = TRUE;
  DwmSetWindowAttribute(
      window,
      DWMWA_TRANSITIONS_FORCEDISABLED,
      &disabled,
      static_cast<DWORD>(sizeof(disabled)));
}

POINT FrameTrackSizeForContentDips(HWND window, LONG width, LONG height) {
  return FrameTrackSizeForContent(
      window,
      {
          std::max<LONG>(1, width),
          std::max<LONG>(1, height),
      });
}

std::shared_ptr<WinUI3NativeLeaf> WinUI3NativeLeaf::Shared() {
  static auto instance = std::make_shared<WinUI3NativeLeaf>();
  return instance;
}

WinUI3NativeLeaf::~WinUI3NativeLeaf() {
  RemoveTray();
  if (window_handle_ != nullptr && previous_window_proc_ != nullptr) {
    SetWindowLongPtrW(window_handle_, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(previous_window_proc_));
    RemovePropW(window_handle_, L"YoungRouter.PreviousWindowProc");
    RemovePropW(window_handle_, L"YoungRouter.NativeLeaf");
  }
}

void WinUI3NativeLeaf::Initialize(winrt::Microsoft::UI::Xaml::Window const& window) {
  window_handle_ = nullptr;
  window.as<::IWindowNative>()->get_WindowHandle(&window_handle_);
  DisableWindowTransitions(window_handle_);
  InstallWindowHook();
  EnsureTray();
}

void WinUI3NativeLeaf::Initialize(HWND window_handle) {
  window_handle_ = window_handle;
  DisableWindowTransitions(window_handle_);
  InstallWindowHook();
  EnsureTray();
}

void WinUI3NativeLeaf::SetStatus(std::wstring_view title, bool running) {
  status_title_ = title;
  status_title_is_bootstrap_ = false;
  service_running_ = running;
  EnsureTray();
  if (tray_visible_) {
    wcsncpy_s(tray_.szTip, status_title_.c_str(), _TRUNCATE);
    Shell_NotifyIconW(NIM_MODIFY, &tray_);
  }
}

void WinUI3NativeLeaf::SetActions(std::vector<NativeMenuAction> const& actions) {
  actions_.clear();
  actions_.reserve(actions.size());
  for (auto const& action : actions) {
    // Codex / Claude share one settings entry and Recovery is a Logs tab.
    // Ignore retired actions while a shared bundle is being updated.
    if (action.id != L"open-claude-settings" && action.id != L"open-recovery" &&
        action.id != L"service-start" && action.id != L"service-stop" &&
        action.id != L"service-restart" && action.id != L"service-reload" &&
        action.id != L"service-health") {
      actions_.push_back(action);
    }
  }
}

void WinUI3NativeLeaf::SetLocalization(std::map<std::string, std::wstring> strings) {
  for (auto& [key, value] : strings) {
    if (!value.empty()) strings_.insert_or_assign(std::move(key), std::move(value));
  }
  if (status_title_is_bootstrap_) {
    auto status = Localized("serviceStatus", L"Status: {status}");
    auto starting = Localized("serviceStarting", L"Starting");
    const auto marker = status.find(L"{status}");
    if (marker != std::wstring::npos) status.replace(marker, 8, starting);
    status_title_ = std::move(status);
  }
  if (window_handle_ != nullptr && !active_route_.empty()) {
    SetWindowTextW(window_handle_, RouteTitle(active_route_).c_str());
  }
  if (tray_visible_) {
    auto tooltip = status_title_.empty() ? Localized("appTitle", L"Young Router") : status_title_;
    wcsncpy_s(tray_.szTip, tooltip.c_str(), _TRUNCATE);
    Shell_NotifyIconW(NIM_MODIFY, &tray_);
  }
}

void WinUI3NativeLeaf::SetActionHandler(std::function<void(std::string const&)> handler) {
  std::vector<std::string> pending;
  {
    std::lock_guard guard(action_mutex_);
    action_handler_ = std::move(handler);
    if (action_handler_) pending.swap(pending_actions_);
  }
  for (auto const& action : pending) DispatchAction(action);
}

void WinUI3NativeLeaf::DispatchAction(std::string const& action) {
  std::function<void(std::string const&)> handler;
  {
    std::lock_guard guard(action_mutex_);
    handler = action_handler_;
    if (!handler) {
      pending_actions_.push_back(action);
      return;
    }
  }
  handler(action);
}

bool WinUI3NativeLeaf::HandleWindowMessage(UINT message, WPARAM wparam, LPARAM lparam) {
  if (message == WM_CLOSE && !quitting_) {
    if (!active_route_.empty()) {
      DispatchAction("request-close-" + WideToUtf8(active_route_));
    }
    return true;
  }
  if (message == kQuitMessage) {
    quitting_ = true;
    if (window_handle_ != nullptr) PostMessageW(window_handle_, WM_CLOSE, 0, 0);
    return true;
  }
  if (message == kTrayMessage && wparam == tray_.uID) {
    if (lparam == WM_LBUTTONUP || lparam == WM_LBUTTONDBLCLK) {
      DispatchDefaultTrayAction();
    } else if (lparam == WM_RBUTTONUP || lparam == WM_CONTEXTMENU) {
      ShowTrayMenu();
    }
    return true;
  } else if (message == WM_COMMAND && LOWORD(wparam) >= kTrayMenuFirstCommand &&
             LOWORD(wparam) < kTrayMenuFirstCommand + actions_.size()) {
    DispatchTrayAction(static_cast<size_t>(LOWORD(wparam) - kTrayMenuFirstCommand));
    return true;
  }
  return false;
}

void WinUI3NativeLeaf::OpenRoute(std::wstring_view route) {
  active_route_ = route;
  if (route == L"home") {
    // "home" leaves the settings shell for the tray, mirroring the macOS
    // host: hide the single window instead of showing an empty shell pane.
    if (window_handle_ != nullptr) ShowWindow(window_handle_, SW_HIDE);
    return;
  }
  if (window_handle_ != nullptr) {
    SetWindowTextW(window_handle_, RouteTitle(route).c_str());
    const auto frame = FrameTrackSizeForContent(window_handle_, RouteInitialContentSize(route));
    SetWindowPos(
        window_handle_, nullptr, 0, 0, frame.x, frame.y,
        SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOZORDER);
    ShowWindow(window_handle_, SW_RESTORE);
    SetForegroundWindow(window_handle_);
  }
}

void WinUI3NativeLeaf::CloseRoute(std::wstring_view route) {
  if (active_route_ == route) {
    active_route_.clear();
    if (window_handle_ != nullptr) ShowWindow(window_handle_, SW_HIDE);
  }
}

POINT WinUI3NativeLeaf::MinimumTrackSizeForActiveRoute() const {
  return FrameTrackSizeForContent(window_handle_, RouteMinimumContentSize(active_route_));
}

bool WinUI3NativeLeaf::SetWindowContentSize(std::wstring_view route, double width, double height) {
  constexpr double kMinimumContentExtent = 128.0;
  constexpr double kMaximumContentExtent = 8192.0;
  if (window_handle_ == nullptr || active_route_ != route || !std::isfinite(width) || !std::isfinite(height) ||
      width < kMinimumContentExtent || height < kMinimumContentExtent ||
      width > kMaximumContentExtent || height > kMaximumContentExtent) {
    return false;
  }

  const auto frame = FrameTrackSizeForContent(
      window_handle_, {static_cast<LONG>(std::lround(width)), static_cast<LONG>(std::lround(height))});
  return SetWindowPos(
      window_handle_,
      nullptr,
      0,
      0,
      frame.x,
      frame.y,
      SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOZORDER) != FALSE;
}

bool WinUI3NativeLeaf::Confirm(
    std::wstring_view title,
    std::wstring_view message,
    std::wstring_view confirm_label) {
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;
  xaml::Window dialog;
  dialog.Title(winrt::hstring(title));

  controls::StackPanel root;
  root.Spacing(16);
  root.Margin(xaml::Thickness{20, 20, 20, 20});

  controls::TextBlock body;
  body.FontSize(kUIFontSize);
  body.Text(winrt::hstring(message));
  body.TextWrapping(xaml::TextWrapping::Wrap);
  root.Children().Append(body);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.FontSize(kUIFontSize);
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  controls::Button confirm;
  confirm.FontSize(kUIFontSize);
  confirm.Content(winrt::box_value(winrt::hstring(
      confirm_label.empty() ? Localized("ok", L"OK") : std::wstring(confirm_label))));
  actions.Children().Append(cancel);
  actions.Children().Append(confirm);
  root.Children().Append(actions);
  dialog.Content(root);

  bool finished = false;
  bool accepted = false;
  cancel.Click([dialog](auto const&, auto const&) { dialog.Close(); });
  confirm.Click([dialog, &accepted](auto const&, auto const&) {
    accepted = true;
    dialog.Close();
  });
  dialog.Closed([&finished](auto const&, auto const&) { finished = true; });
  return RunOwnedModalWindow(dialog, window_handle_, {440, 220}, finished) && accepted;
}

void WinUI3NativeLeaf::ShowReadOnlyText(
    std::wstring_view title,
    std::wstring_view text,
    std::wstring_view close_label,
    std::wstring_view language,
    std::wstring_view html) {
  if (html.empty() || html.size() > 4 * 1024 * 1024 ||
      text.size() > 2 * 1024 * 1024 ||
      (language != L"json" && language != L"toml" && language != L"text")) return;
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  auto state = std::make_shared<ReadOnlyCodeViewerState>();
  xaml::Window dialog;
  state->dialog = dialog;
  dialog.Title(winrt::hstring(title));

  controls::Grid root;
  root.RowDefinitions().Append(controls::RowDefinition());
  controls::RowDefinition action_row;
  action_row.Height(xaml::GridLengthHelper::Auto());
  root.RowDefinitions().Append(action_row);

  controls::WebView2 viewer;
  state->webview = viewer;
  viewer.Margin(xaml::Thickness{16, 16, 16, 12});
  winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(
      viewer, Localized("logOriginal", L"Original log record"));
  state->navigation_completed_token = viewer.NavigationCompleted(
      [weak_state = std::weak_ptr<ReadOnlyCodeViewerState>(state)](auto const&, auto const& args) {
        if (!args.IsSuccess()) FailReadOnlyCodeViewer(weak_state);
      });
  root.Children().Append(viewer);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Margin(xaml::Thickness{16, 0, 16, 16});
  actions.SetValue(controls::Grid::RowProperty(), winrt::box_value(1));
  controls::Button close;
  close.FontSize(kUIFontSize);
  close.Content(winrt::box_value(winrt::hstring(close_label)));
  actions.Children().Append(close);
  root.Children().Append(actions);
  dialog.Content(root);

  auto weak_state = std::weak_ptr<ReadOnlyCodeViewerState>(state);
  close.Click([weak_state](auto const&, auto const&) {
    if (auto current = weak_state.lock(); current && !current->finished) current->dialog.Close();
  });
  dialog.Closed([weak_state](auto const&, auto const&) {
    if (auto current = weak_state.lock()) current->finished = true;
  });
  const auto viewer_html = winrt::hstring(html);
  const auto viewer_text = winrt::hstring(text);
  const auto viewer_language = winrt::hstring(language);
  state->activated_token = dialog.Activated(
      [weak_state, viewer_html, viewer_text, viewer_language](auto const&, auto const&) {
        auto current = weak_state.lock();
        if (!current || current->finished || current->started) return;
        current->started = true;
        InitializeReadOnlyCodeViewer(weak_state, viewer_html, viewer_text, viewer_language);
      });

  const bool completed = RunOwnedModalWindow(dialog, window_handle_, {760, 520}, state->finished);
  try {
    if (state->core && state->web_message_token.value != 0) {
      state->core.WebMessageReceived(state->web_message_token);
    }
  } catch (...) {
  }
  try {
    if (state->webview && state->navigation_completed_token.value != 0) {
      state->webview.NavigationCompleted(state->navigation_completed_token);
    }
  } catch (...) {
  }
  try {
    if (state->dialog && state->activated_token.value != 0) {
      state->dialog.Activated(state->activated_token);
    }
  } catch (...) {
  }
  try {
    if (state->core) state->core.Stop();
  } catch (...) {
  }
  state->core = nullptr;
  state->webview = nullptr;
  state->dialog = nullptr;
  (void)completed;
}

std::optional<std::pair<size_t, size_t>> WinUI3NativeLeaf::ShowGroupedActionMenu(
    std::wstring_view title,
    std::vector<NativeMenuGroup> const& groups,
    NativeMenuAnchor anchor) {
  if (!window_handle_ || title.empty() || groups.empty() || groups.size() > 32) return std::nullopt;
  if (!std::isfinite(anchor.x) || !std::isfinite(anchor.y) || !std::isfinite(anchor.width) || !std::isfinite(anchor.height) ||
      anchor.x < 0 || anchor.y < 0 || anchor.width <= 0 || anchor.height <= 0 ||
      anchor.width > 8192 || anchor.height > 8192) return std::nullopt;
  RECT client{};
  if (!GetClientRect(window_handle_, &client) || anchor.x + anchor.width > client.right + 1 ||
      anchor.y + anchor.height > client.bottom + 1) return std::nullopt;
  HMENU menu = CreatePopupMenu();
  if (!menu) return std::nullopt;
  for (size_t group_index = 0; group_index < groups.size(); ++group_index) {
    auto const& group = groups[group_index];
    if (group.title.empty() || group.title.size() > 240 || group.items.empty() || group.items.size() > 64) {
      DestroyMenu(menu);
      return std::nullopt;
    }
    // The provider is the group's own caption: a disabled row keeps the list
    // one menu deep, with its models directly underneath.
    AppendMenuW(menu, MF_STRING | MF_DISABLED, 0, group.title.c_str());
    for (size_t item_index = 0; item_index < group.items.size(); ++item_index) {
      auto const& item = group.items[item_index];
      if (item.empty() || item.size() > 240) {
        DestroyMenu(menu);
        return std::nullopt;
      }
      AppendMenuW(menu, MF_STRING, static_cast<UINT_PTR>(group_index * 1'000 + item_index + 1), item.c_str());
    }
  }
  // React Native reports window-local DIPs from the top-left. Convert to
  // physical client pixels and anchor below the button, independent of the
  // current mouse position.
  const UINT dpi = std::max<UINT>(GetDpiForWindow(window_handle_), USER_DEFAULT_SCREEN_DPI);
  POINT point{
      MulDiv(static_cast<int>(std::lround(anchor.x)), static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI),
      MulDiv(static_cast<int>(std::lround(anchor.y + anchor.height)), static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI),
  };
  ClientToScreen(window_handle_, &point);
  const UINT selected = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, point.x, point.y, 0, window_handle_, nullptr);
  DestroyMenu(menu);
  if (selected == 0) return std::nullopt;
  const size_t tag = static_cast<size_t>(selected - 1);
  return std::make_pair(tag / 1'000, tag % 1'000);
}

std::optional<size_t> WinUI3NativeLeaf::ShowActionMenu(
    std::wstring_view title,
    std::vector<std::wstring> const& items,
    NativeMenuAnchor anchor) {
  if (!window_handle_ || title.empty() || items.empty() || items.size() > 32) return std::nullopt;
  if (!std::isfinite(anchor.x) || !std::isfinite(anchor.y) || !std::isfinite(anchor.width) || !std::isfinite(anchor.height) ||
      anchor.x < 0 || anchor.y < 0 || anchor.width <= 0 || anchor.height <= 0 ||
      anchor.width > 8192 || anchor.height > 8192) return std::nullopt;
  RECT client{};
  if (!GetClientRect(window_handle_, &client) || anchor.x + anchor.width > client.right + 1 || anchor.y + anchor.height > client.bottom + 1) return std::nullopt;
  HMENU menu = CreatePopupMenu();
  if (!menu) return std::nullopt;
  for (size_t index = 0; index < items.size(); ++index) {
    if (items[index].empty() || items[index].size() > 240) {
      DestroyMenu(menu);
      return std::nullopt;
    }
    AppendMenuW(menu, MF_STRING, static_cast<UINT_PTR>(index + 1), items[index].c_str());
  }
  // React Native reports window-local DIPs from the top-left. Convert to
  // physical client pixels and anchor below the button, independent of the
  // current mouse position.
  const UINT dpi = std::max<UINT>(GetDpiForWindow(window_handle_), USER_DEFAULT_SCREEN_DPI);
  POINT point{
      MulDiv(static_cast<int>(std::lround(anchor.x)), static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI),
      MulDiv(static_cast<int>(std::lround(anchor.y + anchor.height)), static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI),
  };
  ClientToScreen(window_handle_, &point);
  const UINT selected = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, point.x, point.y, 0, window_handle_, nullptr);
  DestroyMenu(menu);
  return selected > 0 && selected <= items.size() ? std::optional<size_t>(selected - 1) : std::nullopt;
}

std::optional<std::vector<std::wstring>> WinUI3NativeLeaf::ChooseModelsToAdd(
    std::vector<std::wstring> models,
    std::wstring provider_name,
    std::wstring key_name) {
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  auto format_template = [](std::wstring text, std::wstring_view key, std::wstring_view value) {
    size_t position = 0;
    while ((position = text.find(key, position)) != std::wstring::npos) {
      text.replace(position, key.size(), value);
      position += value.size();
    }
    return text;
  };
  struct ChooserState {
    std::vector<std::wstring> models;
    std::vector<bool> selected;
    std::wstring query;
    controls::ListView list{nullptr};
    controls::TextBlock summary{nullptr};
    controls::Button all{nullptr};
    controls::Button invert{nullptr};
    controls::Button add{nullptr};
    controls::TextBlock empty_state{nullptr};
    xaml::Window dialog{nullptr};
    bool finished = false;
    bool accepted = false;
  };
  auto state = std::make_shared<ChooserState>();
  state->models = std::move(models);
  state->selected.assign(state->models.size(), false);

  xaml::Window dialog;
  state->dialog = dialog;
  dialog.Title(Localized("modelChooserTitle", L"Choose Models to Add"));

  controls::StackPanel root;
  root.Spacing(8);
  root.Margin(xaml::Thickness{20, 16, 20, 16});

  controls::TextBlock title;
  title.Text(Localized("modelChooserHeading", L"Choose models to add"));
  title.FontSize(kUIFontSize);
  root.Children().Append(title);

  controls::TextBlock subtitle;
  subtitle.FontSize(kUIFontSize);
  subtitle.Text(Localized("modelChooserProvider", L"Provider") + L": " + provider_name +
                L"    " + Localized("modelChooserKey", L"Key") + L": " + key_name);
  subtitle.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
  root.Children().Append(subtitle);

  controls::TextBox search;
  search.FontSize(kUIFontSize);
  search.PlaceholderText(Localized("modelChooserSearch", L"Search models"));
  root.Children().Append(search);

  controls::Grid controls_row;
  controls_row.ColumnDefinitions().Append(controls::ColumnDefinition());
  controls::ColumnDefinition summary_column;
  summary_column.Width(xaml::GridLengthHelper::Auto());
  controls_row.ColumnDefinitions().Append(summary_column);
  controls::StackPanel selection_buttons;
  selection_buttons.Orientation(controls::Orientation::Horizontal);
  selection_buttons.Spacing(8);
  controls::Button all;
  all.FontSize(kUIFontSize);
  all.Content(winrt::box_value(Localized("modelChooserAll", L"All")));
  winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetHelpText(
      all, Localized("modelChooserSelectAllVisible", L"Select all visible models"));
  controls::Button invert;
  invert.FontSize(kUIFontSize);
  invert.Content(winrt::box_value(Localized("modelChooserInvert", L"Invert")));
  winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetHelpText(
      invert, Localized("modelChooserInvertVisible", L"Invert visible model selection"));
  state->all = all;
  state->invert = invert;
  selection_buttons.Children().Append(all);
  selection_buttons.Children().Append(invert);
  controls_row.Children().Append(selection_buttons);
  controls::TextBlock summary;
  summary.FontSize(kUIFontSize);
  state->summary = summary;
  summary.VerticalAlignment(xaml::VerticalAlignment::Center);
  summary.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  summary.SetValue(controls::Grid::ColumnProperty(), winrt::box_value(1));
  controls_row.Children().Append(summary);
  root.Children().Append(controls_row);

  controls::ListView list;
  state->list = list;
  list.SelectionMode(controls::ListViewSelectionMode::None);
  list.MinHeight(220);
  list.MaxHeight(480);
  list.Height(420);
  list.HorizontalAlignment(xaml::HorizontalAlignment::Stretch);
  list.VerticalAlignment(xaml::VerticalAlignment::Stretch);
  controls::Grid list_host;
  list_host.MinHeight(220);
  list_host.MaxHeight(480);
  list_host.Height(420);
  list_host.Children().Append(list);
  controls::TextBlock empty_state;
  empty_state.FontSize(kUIFontSize);
  empty_state.HorizontalAlignment(xaml::HorizontalAlignment::Center);
  empty_state.VerticalAlignment(xaml::VerticalAlignment::Center);
  empty_state.TextAlignment(xaml::TextAlignment::Center);
  empty_state.TextWrapping(xaml::TextWrapping::Wrap);
  empty_state.IsHitTestVisible(false);
  empty_state.Visibility(xaml::Visibility::Collapsed);
  state->empty_state = empty_state;
  list_host.Children().Append(empty_state);
  root.Children().Append(list_host);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.FontSize(kUIFontSize);
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  controls::Button add;
  add.FontSize(kUIFontSize);
  add.Content(winrt::box_value(Localized("modelChooserAddSelected", L"Add Selected")));
  add.IsEnabled(false);
  state->add = add;
  actions.Children().Append(cancel);
  actions.Children().Append(add);
  root.Children().Append(actions);
  dialog.Content(root);

  auto weak_state = std::weak_ptr<ChooserState>(state);
  auto const count_template = Localized("modelChooserCount", L"{count} models");
  auto const filtered_count_template = Localized("modelChooserCountFiltered", L"{visible} of {total} models");
  auto const selected_count_template = Localized("modelChooserCountSelected", L"{count} selected");
  auto const empty_label = Localized("modelChooserEmpty", L"No models available");
  auto const no_matches_label = Localized("modelChooserNoMatches", L"No matching models");
  auto refresh = [weak_state, format_template, count_template, filtered_count_template, selected_count_template, empty_label, no_matches_label] {
    auto state = weak_state.lock();
    if (!state) return;
    state->list.Items().Clear();
    auto query = FoldModelSearchText(state->query);
    size_t visible = 0;
    size_t selected = 0;
    for (size_t index = 0; index < state->models.size(); ++index) {
      if (state->selected[index]) ++selected;
      if (!query.empty() && FoldModelSearchText(state->models[index]).find(query) == std::wstring::npos) continue;
      controls::CheckBox item;
      item.FontSize(kUIFontSize);
      item.Content(winrt::box_value(state->models[index]));
      item.IsChecked(state->selected[index]);
      item.HorizontalAlignment(xaml::HorizontalAlignment::Stretch);
      item.Click([weak_state, index, format_template, count_template, filtered_count_template, selected_count_template](auto const& sender, auto const&) {
        if (auto current = weak_state.lock(); current && index < current->selected.size()) {
          auto checked = sender.as<controls::CheckBox>().IsChecked();
          current->selected[index] = checked && checked.Value();
          current->add.IsEnabled(std::any_of(current->selected.begin(), current->selected.end(), [](bool value) { return value; }));
          size_t visible_count = 0;
          auto query_text = FoldModelSearchText(current->query);
          for (auto const& model : current->models) {
            if (query_text.empty() || FoldModelSearchText(model).find(query_text) != std::wstring::npos) ++visible_count;
          }
          size_t selected_count = static_cast<size_t>(std::count(current->selected.begin(), current->selected.end(), true));
          std::wstring label = query_text.empty()
              ? format_template(count_template, L"{count}", std::to_wstring(current->models.size()))
              : format_template(
                    format_template(filtered_count_template, L"{visible}", std::to_wstring(visible_count)),
                    L"{total}", std::to_wstring(current->models.size()));
          if (selected_count > 0) {
            label += L"  |  " + format_template(selected_count_template, L"{count}", std::to_wstring(selected_count));
          }
          current->summary.Text(label);
        }
      });
      state->list.Items().Append(item);
      ++visible;
    }
    state->all.IsEnabled(visible > 0);
    state->invert.IsEnabled(visible > 0);
    state->add.IsEnabled(selected > 0);
    const auto& empty_message = state->models.empty() ? empty_label : no_matches_label;
    state->empty_state.Text(empty_message);
    state->empty_state.Visibility(
        visible == 0 ? xaml::Visibility::Visible : xaml::Visibility::Collapsed);
    std::wstring label = query.empty()
        ? format_template(count_template, L"{count}", std::to_wstring(state->models.size()))
        : format_template(
              format_template(filtered_count_template, L"{visible}", std::to_wstring(visible)),
              L"{total}", std::to_wstring(state->models.size()));
    if (selected > 0) {
      label += L"  |  " + format_template(selected_count_template, L"{count}", std::to_wstring(selected));
    }
    state->summary.Text(label);
  };

  search.TextChanged([weak_state, refresh](auto const& sender, auto const&) {
    auto state = weak_state.lock();
    if (!state) return;
    state->query = std::wstring(sender.as<controls::TextBox>().Text());
    refresh();
  });
  all.Click([weak_state, refresh](auto const&, auto const&) {
    auto state = weak_state.lock();
    if (!state) return;
    auto query = FoldModelSearchText(state->query);
    for (size_t index = 0; index < state->models.size(); ++index) {
      if (query.empty() || FoldModelSearchText(state->models[index]).find(query) != std::wstring::npos) state->selected[index] = true;
    }
    refresh();
  });
  invert.Click([weak_state, refresh](auto const&, auto const&) {
    auto state = weak_state.lock();
    if (!state) return;
    auto query = FoldModelSearchText(state->query);
    for (size_t index = 0; index < state->models.size(); ++index) {
      if (query.empty() || FoldModelSearchText(state->models[index]).find(query) != std::wstring::npos) state->selected[index] = !state->selected[index];
    }
    refresh();
  });
  cancel.Click([dialog](auto const&, auto const&) { dialog.Close(); });
  add.Click([weak_state](auto const&, auto const&) {
    if (auto state = weak_state.lock()) {
      state->accepted = true;
      state->dialog.Close();
    }
  });
  dialog.Closed([weak_state](auto const&, auto const&) {
    if (auto state = weak_state.lock()) state->finished = true;
  });
  refresh();

  if (!RunOwnedModalWindow(dialog, window_handle_, {560, 650}, state->finished) || !state->accepted) {
    return std::nullopt;
  }
  std::vector<std::wstring> selected;
  for (size_t index = 0; index < state->models.size(); ++index) {
    if (state->selected[index]) selected.push_back(state->models[index]);
  }
  return selected;
}

std::optional<NativeSecretEditResult> WinUI3NativeLeaf::EditSecret(
    std::wstring_view title,
    bool allow_clear,
    bool present) {
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  xaml::Window dialog;
  dialog.Title(winrt::hstring(title));
  controls::StackPanel root;
  root.Spacing(12);
  root.Margin(xaml::Thickness{20, 20, 20, 20});
  controls::PasswordBox input;
  input.FontSize(kUIFontSize);
  input.MaxLength(16384);
  root.Children().Append(input);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.FontSize(kUIFontSize);
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  actions.Children().Append(cancel);
  controls::Button clear;
  clear.FontSize(kUIFontSize);
  if (allow_clear && present) {
    clear.Content(winrt::box_value(winrt::hstring(Localized("clear", L"Clear"))));
    actions.Children().Append(clear);
  }
  controls::Button set;
  set.FontSize(kUIFontSize);
  set.Content(winrt::box_value(winrt::hstring(Localized("set", L"Set"))));
  set.IsEnabled(false);
  actions.Children().Append(set);
  root.Children().Append(actions);
  dialog.Content(root);

  bool finished = false;
  std::optional<NativeSecretEditResult> result;
  input.PasswordChanged([set](auto const& sender, auto const&) {
    set.IsEnabled(!sender.as<controls::PasswordBox>().Password().empty());
  });
  cancel.Click([dialog](auto const&, auto const&) { dialog.Close(); });
  if (allow_clear && present) {
    clear.Click([dialog, &result](auto const&, auto const&) {
      result = NativeSecretEditResult{true, {}};
      dialog.Close();
    });
  }
  set.Click([dialog, input, &result](auto const&, auto const&) {
    auto value = std::wstring(input.Password());
    if (value.empty()) return;
    result = NativeSecretEditResult{false, std::move(value)};
    dialog.Close();
  });
  dialog.Closed([&finished](auto const&, auto const&) { finished = true; });
  if (!RunOwnedModalWindow(dialog, window_handle_, {520, 190}, finished)) result.reset();
  input.Password(L"");
  return result;
}

std::optional<GroupManagerResult> WinUI3NativeLeaf::ShowGroupManager(
    std::wstring title,
    std::wstring account_label,
    std::wstring account_id,
    std::vector<GroupManagerGroup> groups,
    std::vector<GroupManagerKey> keys,
    GroupManagerLabels labels,
    bool auto_grouping) {
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  struct SheetRow {
    std::wstring id;
    std::wstring name;
    std::wstring group_id;
    std::wstring group_label;
    std::wstring multiplier;
    // The station's credential-presence sentinel and the models it reports for
    // the key; the plaintext value stays behind Core's capability.
    std::wstring hint;
    std::vector<std::wstring> models;
    std::wstring original_name;
    std::wstring original_group_id;
    bool original_enabled = true;
    bool enabled = true;
    bool deleted = false;
    bool draft = false;
  };

  auto rows = std::make_shared<std::vector<SheetRow>>();
  for (auto const& key : keys) {
    SheetRow row;
    row.id = key.id;
    row.name = key.name;
    row.group_id = key.group_id;
    row.group_label = key.group_label;
    row.multiplier = key.multiplier;
    row.hint = key.hint;
    row.models = SplitLines(key.models);
    row.original_name = key.name;
    row.original_group_id = key.group_id;
    rows->push_back(std::move(row));
  }  auto syncing = std::make_shared<bool>(false);
  auto applied = std::make_shared<bool>(false);

  auto theme_brush = [](wchar_t const* resource, winrt::Windows::UI::Color fallback) {
    try {
      auto resources = winrt::Microsoft::UI::Xaml::Application::Current().Resources();
      auto value = resources.Lookup(winrt::box_value(winrt::hstring(resource)));
      if (auto brush = value.try_as<winrt::Microsoft::UI::Xaml::Media::SolidColorBrush>()) return brush;
    } catch (...) {
    }
    return winrt::Microsoft::UI::Xaml::Media::SolidColorBrush(fallback);
  };
  auto group_index_for = [&groups](std::wstring const& id) -> int32_t {
    for (size_t index = 0; index < groups.size(); ++index) {
      if (groups[index].id == id) return static_cast<int32_t>(index);
    }
    return -1;
  };

  xaml::Window dialog;
  dialog.Title(winrt::hstring(title));
  controls::StackPanel root;
  root.Spacing(8);
  root.Margin(xaml::Thickness{20, 16, 20, 16});

  controls::TextBlock account;
  account.FontSize(kUIFontSize);
  account.Text(winrt::hstring(account_label));
  account.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
  root.Children().Append(account);

  // Master-detail body: the key list with the ＋ / － toolbar on the left, the
  // selected key on the right, and Close / Apply in the footer.
  controls::Grid layout;
  controls::ColumnDefinition left_column;
  left_column.Width(xaml::GridLengthHelper::FromPixels(360));
  controls::ColumnDefinition right_column;
  // The detail column states the key's facts at half the key list's width, so
  // the sheet carries no empty right half.
  right_column.Width(xaml::GridLengthHelper::FromPixels(190));
  layout.ColumnDefinitions().Append(left_column);
  layout.ColumnDefinitions().Append(right_column);
  controls::RowDefinition body_row;
  controls::RowDefinition footer_row;
  footer_row.Height(xaml::GridLengthHelper::Auto());
  layout.RowDefinitions().Append(body_row);
  layout.RowDefinitions().Append(footer_row);

  controls::Grid left;
  controls::RowDefinition list_header_row;
  list_header_row.Height(xaml::GridLengthHelper::Auto());
  left.RowDefinitions().Append(list_header_row);
  left.RowDefinitions().Append(controls::RowDefinition());
  controls::Grid list_header;
  controls::ColumnDefinition list_title_column;
  controls::ColumnDefinition remove_column;
  remove_column.Width(xaml::GridLengthHelper::Auto());
  controls::ColumnDefinition add_column;
  add_column.Width(xaml::GridLengthHelper::Auto());
  list_header.ColumnDefinitions().Append(list_title_column);
  list_header.ColumnDefinitions().Append(remove_column);
  list_header.ColumnDefinitions().Append(add_column);
  controls::TextBlock list_title;
  list_title.FontSize(kUIFontSize);
  list_title.FontWeight(winrt::Windows::UI::Text::FontWeights::SemiBold());
  list_title.Text(winrt::hstring(labels.list_label));
  list_title.VerticalAlignment(xaml::VerticalAlignment::Center);
  controls::Grid::SetColumn(list_title, 0);
  list_header.Children().Append(list_title);
  // ± ride the list header as the app's compact icon buttons do: one square
  // small control each, four points apart.
  controls::Button add_button;
  add_button.FontSize(kUIFontSize);
  add_button.Content(winrt::box_value(winrt::hstring(L"+")));
  add_button.MinWidth(22);
  add_button.Width(22);
  add_button.Margin(xaml::Thickness{4, 0, 0, 0});
  // ＋ sits left of － at the key list's top-right, like the macOS sheet.
  controls::Grid::SetColumn(add_button, 1);
  list_header.Children().Append(add_button);
  controls::Button remove_button;
  remove_button.FontSize(kUIFontSize);
  remove_button.Content(winrt::box_value(winrt::hstring(L"\u2212")));
  remove_button.MinWidth(22);
  remove_button.Width(22);
  remove_button.Margin(xaml::Thickness{4, 0, 0, 0});
  controls::Grid::SetColumn(remove_button, 2);
  list_header.Children().Append(remove_button);
  left.Children().Append(list_header);

  controls::Border list_frame;
  list_frame.BorderThickness(xaml::Thickness{1, 1, 1, 1});
  list_frame.BorderBrush(theme_brush(
      L"ControlStrokeColorDefaultBrush", winrt::Windows::UI::Color{255, 140, 140, 140}));
  list_frame.Background(theme_brush(
      L"ControlFillColorDefaultBrush", winrt::Windows::UI::Color{255, 255, 255, 255}));
  // Column labels head the list so 名称 / 分组 / 倍率 stay readable while
  // 自动分组 disables the detail pane.  The group and multiplier columns keep
  // fixed widths in the header and in every row so both stay aligned.
  constexpr double kNameColumnWidth = 160;
  constexpr double kGroupColumnWidth = 110;
  constexpr double kMultiplierColumnWidth = 74;
  controls::Grid list_body;
  controls::RowDefinition list_columns_row;
  list_columns_row.Height(xaml::GridLengthHelper::Auto());
  list_body.RowDefinitions().Append(list_columns_row);
  list_body.RowDefinitions().Append(controls::RowDefinition());
  controls::Grid list_columns;
  controls::ColumnDefinition columns_name;
  controls::ColumnDefinition columns_group;
  controls::ColumnDefinition columns_multiplier;
  columns_name.Width(xaml::GridLengthHelper::FromPixels(kNameColumnWidth));
  columns_group.Width(xaml::GridLengthHelper::FromPixels(kGroupColumnWidth));
  columns_multiplier.Width(xaml::GridLengthHelper::FromPixels(kMultiplierColumnWidth));
  list_columns.ColumnDefinitions().Append(columns_name);
  list_columns.ColumnDefinitions().Append(columns_group);
  list_columns.ColumnDefinitions().Append(columns_multiplier);
  auto append_column_label = [&](int32_t column, std::wstring const& text) {
    controls::TextBlock label;
    label.FontSize(kUIFontSize);
    label.VerticalAlignment(xaml::VerticalAlignment::Center);
    label.Text(winrt::hstring(text));
    label.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
    label.Margin(column == 0 ? xaml::Thickness{8, 4, 8, 4} : xaml::Thickness{0, 4, 8, 4});
    label.Foreground(theme_brush(
        L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115}));
    controls::Grid::SetColumn(label, column);
    list_columns.Children().Append(label);
  };
  append_column_label(0, labels.name_label);
  append_column_label(1, labels.group_label);
  append_column_label(2, labels.multiplier_label);
  list_body.Children().Append(list_columns);
  controls::ListView list;
  list.SelectionMode(controls::ListViewSelectionMode::Single);
  list.IsItemClickEnabled(true);
  list.Height(300);
  list.Padding(xaml::Thickness{0, 0, 0, 0});
  list.BorderThickness(xaml::Thickness{0, 0, 0, 0});
  list.Background(theme_brush(
      L"ControlFillColorDefaultBrush", winrt::Windows::UI::Color{255, 255, 255, 255}));
  controls::ScrollViewer::SetVerticalScrollBarVisibility(list, controls::ScrollBarVisibility::Auto);
  controls::ScrollViewer::SetHorizontalScrollBarVisibility(list, controls::ScrollBarVisibility::Disabled);
  controls::Grid::SetRow(list, 1);
  list_body.Children().Append(list);
  list_frame.Child(list_body);
  controls::Grid::SetRow(list_frame, 1);
  left.Children().Append(list_frame);
  controls::Grid::SetColumn(left, 0);
  layout.Children().Append(left);

  // The detail column reports one fact per row, so the selected key is never
  // a half-filled form: the group's rate, the station's key with its copy
  // action, and the models the key provides.  The captions share one width,
  // which fits every localized label.
  constexpr double kDetailCaptionWidth = 90;
  controls::StackPanel detail;
  detail.Spacing(6);
  auto detail_row = [&detail, &theme_brush](std::wstring_view caption) -> controls::Grid {
    controls::Grid row;
    controls::ColumnDefinition caption_column;
    caption_column.Width(xaml::GridLengthHelper::FromPixels(kDetailCaptionWidth));
    row.ColumnDefinitions().Append(caption_column);
    row.ColumnDefinitions().Append(controls::ColumnDefinition());
    controls::TextBlock caption_text;
    caption_text.FontSize(kUIFontSize);
    caption_text.Text(winrt::hstring(caption));
    caption_text.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
    caption_text.VerticalAlignment(xaml::VerticalAlignment::Center);
    caption_text.Foreground(theme_brush(
        L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115}));
    controls::Grid::SetColumn(caption_text, 0);
    row.Children().Append(caption_text);
    detail.Children().Append(row);
    return row;
  };
  auto detail_value = [&theme_brush](controls::Grid const& row) -> controls::TextBlock {
    controls::TextBlock value;
    value.FontSize(kUIFontSize);
    value.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
    value.VerticalAlignment(xaml::VerticalAlignment::Center);
    value.Foreground(theme_brush(L"TextFillColorPrimaryBrush", winrt::Windows::UI::Color{255, 30, 30, 30}));
    controls::Grid::SetColumn(value, 1);
    row.Children().Append(value);
    return value;
  };
  // 模型列表 sits in the detail column under the key's own facts: one aligned
  // line per model, sized for the longest list any key carries so switching
  // groups never resizes the window.  Longer lists scroll.
  constexpr double kModelRowHeight = 17;
  controls::TextBlock models_title;
  models_title.FontSize(kUIFontSize);
  models_title.FontWeight(winrt::Windows::UI::Text::FontWeights::SemiBold());
  models_title.Text(winrt::hstring(labels.models_label));
  controls::ScrollViewer models_scroll;
  controls::ScrollViewer::SetVerticalScrollBarVisibility(models_scroll, controls::ScrollBarVisibility::Auto);
  controls::ScrollViewer::SetHorizontalScrollBarVisibility(models_scroll, controls::ScrollBarVisibility::Disabled);
  models_scroll.BorderThickness(xaml::Thickness{0, 0, 0, 0});
  models_scroll.Background(nullptr);
  controls::StackPanel models_stack;
  models_scroll.Content(models_stack);
  auto rebuild_models = [&models_stack, &theme_brush, &labels](std::vector<std::wstring> const& models) {
    models_stack.Children().Clear();
    if (models.empty()) {
      controls::TextBlock empty;
      empty.FontSize(kUIFontSize);
      empty.Text(winrt::hstring(labels.empty_label));
      empty.Foreground(theme_brush(
          L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115}));
      models_stack.Children().Append(empty);
      return;
    }
    for (auto const& name : models) {
      controls::TextBlock field;
      field.FontSize(kUIFontSize);
      field.Text(winrt::hstring(name));
      field.TextWrapping(xaml::TextWrapping::Wrap);
      field.Foreground(theme_brush(L"TextFillColorPrimaryBrush", winrt::Windows::UI::Color{255, 30, 30, 30}));
      controls::ToolTipService::SetToolTip(field, winrt::box_value(winrt::hstring(name)));
      models_stack.Children().Append(field);
    }
  };

  controls::CheckBox enabled_box;
  enabled_box.FontSize(kUIFontSize);
  enabled_box.Content(winrt::box_value(winrt::hstring(labels.enabled_label)));
  detail.Children().Append(enabled_box);
  controls::Grid name_row = detail_row(labels.name_label);
  controls::TextBox name_box;
  name_box.FontSize(kUIFontSize);
  controls::Grid::SetColumn(name_box, 1);
  name_row.Children().Append(name_box);
  controls::Grid group_row = detail_row(labels.group_label);
  controls::ComboBox group_picker;
  group_picker.FontSize(kUIFontSize);
  for (auto const& entry : groups) group_picker.Items().Append(winrt::box_value(winrt::hstring(entry.label)));
  controls::Grid::SetColumn(group_picker, 1);
  group_row.Children().Append(group_picker);
  controls::Grid multiplier_row = detail_row(labels.multiplier_label);
  controls::TextBlock multiplier_value = detail_value(multiplier_row);
  controls::Grid value_row = detail_row(labels.value_label);
  controls::Grid value_field;
  controls::ColumnDefinition value_column;
  controls::ColumnDefinition copy_column;
  copy_column.Width(xaml::GridLengthHelper::Auto());
  value_field.ColumnDefinitions().Append(value_column);
  value_field.ColumnDefinitions().Append(copy_column);
  controls::TextBlock value_text = detail_value(value_field);
  // 密钥值 keeps one line and shows the head, an ellipsis, and the tail of the
  // key; selecting a shortened line would hand over a broken key, so the copy
  // action stays the one way to take the whole value.
  // The value shares its row with the copy action, so it takes the leading
  // column of the inner grid instead of the row's value column.
  controls::Grid::SetColumn(value_text, 0);
  controls::Button copy_button;
  copy_button.FontSize(kUIFontSize);
  copy_button.MinWidth(22);
  copy_button.Width(22);
  copy_button.Margin(xaml::Thickness{6, 0, 0, 0});
  // The copy is an icon button beside the value, the way every icon action in
  // the app reads: the words ride it as its tooltip and its automation name
  // instead of taking a text button's width.
  auto copy_glyph = controls::FontIcon{};
  copy_glyph.FontFamily(winrt::Microsoft::UI::Xaml::Media::FontFamily(L"Segoe MDL2 Assets"));
  copy_glyph.FontSize(kUIFontSize);
  copy_glyph.Glyph(L"\xE8C8");
  copy_button.Content(copy_glyph);
  controls::ToolTipService::SetToolTip(copy_button, winrt::box_value(winrt::hstring(labels.copy_label)));
  winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(copy_button, winrt::hstring(labels.copy_label));
  controls::Grid::SetColumn(copy_button, 1);
  value_field.Children().Append(copy_button);
  controls::Grid::SetColumn(value_field, 1);
  value_row.Children().Append(value_field);
  detail.Children().Append(models_title);
  detail.Children().Append(models_scroll);
  controls::TextBlock copy_status;
  copy_status.FontSize(kUIFontSize);
  copy_status.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
  copy_status.Foreground(theme_brush(
      L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115}));
  detail.Children().Append(copy_status);
  controls::Grid::SetColumn(detail, 1);
  layout.Children().Append(detail);

  controls::Grid footer;
  controls::ColumnDefinition footer_toggle;
  controls::ColumnDefinition footer_actions;
  footer_actions.Width(xaml::GridLengthHelper::Auto());
  footer.ColumnDefinitions().Append(footer_toggle);
  footer.ColumnDefinitions().Append(footer_actions);
  controls::CheckBox toggle;
  toggle.FontSize(kUIFontSize);
  toggle.Content(winrt::box_value(winrt::hstring(labels.auto_grouping_label)));
  toggle.IsChecked(auto_grouping);
  toggle.VerticalAlignment(xaml::VerticalAlignment::Center);
  auto toggle_on = [&toggle]() {
    auto checked = toggle.IsChecked();
    return checked && checked.Value();
  };
  controls::Grid::SetColumn(toggle, 0);
  footer.Children().Append(toggle);
  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button close;
  close.FontSize(kUIFontSize);
  close.Content(winrt::box_value(winrt::hstring(labels.close_label)));
  controls::Button apply;
  apply.FontSize(kUIFontSize);
  apply.Content(winrt::box_value(winrt::hstring(labels.apply_label)));
  apply.IsEnabled(false);
  actions.Children().Append(close);
  actions.Children().Append(apply);
  controls::Grid::SetColumn(actions, 1);
  footer.Children().Append(actions);
  controls::Grid::SetRow(footer, 1);
  controls::Grid::SetColumnSpan(footer, 2);
  layout.Children().Append(footer);

  root.Children().Append(layout);
  dialog.Content(root);

  auto selected_index = [&list]() -> int32_t { return list.SelectedIndex(); };

  // Save and Close stays disabled until the draft would change the account:
  // the auto-grouping switch, a staged create, update, or delete.
  auto staged_deletes = std::make_shared<size_t>(0);
  auto has_staged_changes = [&rows, &toggle_on, initial_auto_grouping = auto_grouping, staged_deletes]() {
    if (toggle_on() != initial_auto_grouping) return true;
    if (*staged_deletes > 0) return true;
    for (auto const& row : *rows) {
      if (row.draft) {
        if (!row.name.empty()) return true;
        continue;
      }
      if (row.name != row.original_name || row.group_id != row.original_group_id || row.enabled != row.original_enabled) return true;
    }
    return false;
  };
  auto refresh_apply = [&apply, &has_staged_changes]() { apply.IsEnabled(has_staged_changes()); };
  // Group names the account still offers, keyed by group id, so the list's
  // group column names the group while its 倍率 column carries the rate.
  auto current_group_text = [&groups](SheetRow const& row, GroupManagerLabels const& labels) -> std::wstring {
    if (row.group_id.empty()) return labels.ungrouped_label;
    for (auto const& entry : groups) {
      if (entry.id == row.group_id) return entry.name;
    }
    return row.group_label.empty() ? labels.ungrouped_label : row.group_label;
  };
  // The key's rate while it belongs to a group the store still offers.  An
  // ungrouped key has no rate to report, and the group column already names
  // it, so the cell stays empty instead of repeating 未分组.
  auto multiplier_text = [&groups](SheetRow const& row, GroupManagerLabels const&) -> std::wstring {
    if (row.group_id.empty()) return std::wstring{};
    for (auto const& entry : groups) {
      if (entry.id == row.group_id) return row.multiplier;
    }
    return std::wstring{};
  };
  // The 倍率 column reports the rate the row costs right now, on the same
  // terms whether or not 自动分组 owns the layout; 倍率 carries a rate and
  // nothing else, so a staged create or delete dims the row instead.
  auto presentation = [&multiplier_text](SheetRow const& row, GroupManagerLabels const& labels) -> std::wstring {
    return multiplier_text(row, labels);
  };
  // A row the draft will create or delete; the list shows it dimmed.
  auto is_staged = [](SheetRow const& row) { return row.deleted || row.draft; };

  // The station's presence sentinel is the only key material the descriptor
  // carries, and a draft key has no key on the station yet.
  auto can_copy = [](SheetRow const& row) { return !row.hint.empty() && !row.draft && !row.id.empty(); };
  // Plaintext keys the sheet already revealed while it is open, keyed by key
  // id.  Core's lease is read-once, so a re-selection reuses what the sheet
  // already read instead of asking for another one, and the values are dropped
  // with the window.
  auto revealed_values = std::make_shared<std::map<std::wstring, std::wstring>>();
  // A key this app already read is on screen when the sheet opens: the
  // remembered value is what the row shows until the sheet reads the current
  // one, so the value row is never empty for a known key.
  for (auto const& row : *rows) {
    if (row.draft || row.hint.empty()) continue;
    const std::wstring remembered = RememberedRelayKey(account_id, row.id);
    if (!remembered.empty()) (*revealed_values)[row.id] = remembered;
  }
  // One transient status line: every message restarts the same timer, and only
  // the newest one clears it.  The XAML objects are captured by value so a
  // reply that arrives after the sheet closes cannot touch freed stack state.
  auto status_timer = std::make_shared<xaml::DispatcherTimer>();
  status_timer->Interval(std::chrono::milliseconds(4000));
  status_timer->Tick([status_timer, copy_status](auto const&, auto const&) {
    status_timer->Stop();
    copy_status.Text(L"");
  });
  auto show_copy_status = [copy_status, status_timer](std::wstring const& message) {
    copy_status.Text(winrt::hstring(message));
    status_timer->Stop();
    if (!message.empty()) status_timer->Start();
  };
  copy_button.Click([rows, list, account_id, copy_button, show_copy_status, can_copy, revealed_values,
                     copied_label = labels.copied_label, failed_label = labels.failed_label](auto const&, auto const&) {
    const int32_t selected = list.SelectedIndex();
    if (selected < 0 || static_cast<size_t>(selected) >= rows->size()) return;
    auto row = std::make_shared<SheetRow>((*rows)[static_cast<size_t>(selected)]);
    if (!can_copy(*row)) return;
    // A key the sheet already revealed copies from what it holds, so the
    // read-once lease is not spent twice on the same key.
    auto revealed = revealed_values->find(row->id);
    if (revealed != revealed_values->end()) {
      try {
        using namespace winrt::Windows::ApplicationModel::DataTransfer;
        DataPackage package;
        package.SetText(winrt::hstring(revealed->second));
        Clipboard::SetContent(package);
        Clipboard::Flush();
        show_copy_status(copied_label);
      } catch (...) {
        show_copy_status(failed_label);
      }
      return;
    }
    copy_button.IsEnabled(false);
    show_copy_status(L"");
    auto target = std::make_shared<std::wstring>(account_id + L":" + row->id);
    auto dispatcher = winrt::Microsoft::UI::Dispatching::DispatcherQueue::GetForCurrentThread();
    if (!dispatcher) {
      copy_button.IsEnabled(true);
      return;
    }
    // Core owns the plaintext; read it off the UI thread through the same
    // capability the provider workspace uses, then copy it back on the UI
    // thread because WinUI's clipboard requires it.  The read retries a lease
    // that lost a revision race, exactly like the value row's own read.
    std::thread([target, row, dispatcher, copy_button, show_copy_status, can_copy, copied_label, failed_label, account_id] {
      std::optional<std::wstring> value;
      for (int attempt = 0; attempt < 3 && !value; ++attempt) {
        value = CoreIPCBridge::Shared().ReadPlainTextSecret("relay_accounts", "api_key", winrt::to_string(*target));
        if (value && value->empty()) value.reset();
        if (!value && attempt < 2) std::this_thread::sleep_for(std::chrono::milliseconds(250));
      }
      bool copied = false;
      if (value) {
        try {
          using namespace winrt::Windows::ApplicationModel::DataTransfer;
          DataPackage package;
          package.SetText(winrt::hstring(*value));
          Clipboard::SetContent(package);
          Clipboard::Flush();
          copied = true;
        } catch (...) {
        }
        if (copied) RememberRelayKey(account_id, row->id, *value);
        value->clear();
      }
      dispatcher.TryEnqueue([row, copy_button, show_copy_status, can_copy,
                             message = copied ? copied_label : failed_label] {
        copy_button.IsEnabled(can_copy(*row));
        show_copy_status(message);
      });
    }).detach();
  });

  // Show the selected key in plaintext: the value is read through Core's native
  // capability and stays in this window.  A draft key has no key on the station
  // yet, and a row with no credential states that instead of a stand-in label
  // that reads like the value.
  //
  // The sheet reads every row's key, the selected row first: Core answers a key
  // it already holds at once, and a cold account costs one station read for the
  // whole pass, so a row carries its key when it is selected instead of staying
  // empty while its own read runs.
  auto reveal_queue = std::make_shared<std::vector<std::wstring>>();
  auto reveal_in_flight = std::make_shared<std::set<std::wstring>>();
  auto reveal_pass_running = std::make_shared<bool>(false);
  auto drain_reveal_queue = std::make_shared<std::function<void()>>();
  *drain_reveal_queue = [reveal_queue, reveal_in_flight, reveal_pass_running, drain_reveal_queue,
                         account_id, rows, list, value_text, revealed_values,
                         failed_label = labels.failed_label, show_copy_status]() {
    if (*reveal_pass_running || reveal_queue->empty()) return;
    const std::wstring key_id = reveal_queue->front();
    reveal_queue->erase(reveal_queue->begin());
    reveal_in_flight->insert(key_id);
    *reveal_pass_running = true;
    auto dispatcher = winrt::Microsoft::UI::Dispatching::DispatcherQueue::GetForCurrentThread();
    if (!dispatcher) {
      *reveal_pass_running = false;
      return;
    }
    auto target = std::make_shared<std::wstring>(account_id + L":" + key_id);
    // The key behind one relay resource, through Core's one-time plaintext
    // lease. A Core revision that moves between the lease and the read fails
    // that pair rather than the key, so a lost race is retried on a fresh lease
    // instead of leaving the row without its value.  One read runs at a time:
    // this is background work for rows the user may not look at.
    std::thread([target, key_id, dispatcher, value_text, revealed_values, reveal_queue, reveal_in_flight,
                 reveal_pass_running, drain_reveal_queue, rows, list, account_id,
                 failed_label, show_copy_status] {
      std::optional<std::wstring> value;
      for (int attempt = 0; attempt < 3 && !value; ++attempt) {
        value = CoreIPCBridge::Shared().ReadPlainTextSecret("relay_accounts", "api_key", winrt::to_string(*target));
        if (value && value->empty()) value.reset();
        if (!value && attempt < 2) std::this_thread::sleep_for(std::chrono::milliseconds(250));
      }
      auto revealed_value = value ? std::make_shared<std::wstring>(*value) : std::shared_ptr<std::wstring>();
      if (value) value->clear();
      dispatcher.TryEnqueue([key_id, revealed_value, value_text, revealed_values, reveal_queue, reveal_in_flight,
                             reveal_pass_running, drain_reveal_queue, rows, list, account_id,
                             failed_label, show_copy_status] {
        reveal_in_flight->erase(key_id);
        *reveal_pass_running = false;
        const int32_t selected = list.SelectedIndex();
        const bool is_selected = selected >= 0 && static_cast<size_t>(selected) < rows->size() &&
            (*rows)[static_cast<size_t>(selected)].id == key_id;
        if (revealed_value) {
          (*revealed_values)[key_id] = *revealed_value;
          RememberRelayKey(account_id, key_id, *revealed_value);
          // The row being looked at shows what this pass read.
          if (is_selected) value_text.Text(winrt::hstring(EllipsizeMiddle(*revealed_value)));
        } else if (is_selected) {
          show_copy_status(failed_label);
        }
        (*drain_reveal_queue)();
      });
    }).detach();
  };
  auto fill_row_keys = [rows, revealed_values, reveal_queue, reveal_in_flight, drain_reveal_queue,
                        can_copy, selected_index]() {
    const int32_t selected = selected_index();
    std::wstring selected_id;
    if (selected >= 0 && static_cast<size_t>(selected) < rows->size()) {
      selected_id = (*rows)[static_cast<size_t>(selected)].id;
    }
    std::vector<std::wstring> pending;
    for (auto const& row : *rows) {
      if (!can_copy(row)) continue;
      if (revealed_values->count(row.id) > 0) continue;
      if (reveal_in_flight->count(row.id) > 0) continue;
      if (std::find(reveal_queue->begin(), reveal_queue->end(), row.id) != reveal_queue->end()) continue;
      pending.push_back(row.id);
    }
    if (pending.empty()) return;
    if (!selected_id.empty()) {
      auto found = std::find(pending.begin(), pending.end(), selected_id);
      if (found != pending.end()) {
        const std::wstring first = *found;
        pending.erase(found);
        pending.insert(pending.begin(), first);
      }
    }
    reveal_queue->insert(reveal_queue->end(), pending.begin(), pending.end());
    (*drain_reveal_queue)();
  };
  auto reveal_value = [value_text, revealed_values, can_copy, empty_label = labels.empty_label,
                       fill_row_keys](SheetRow const& row) {
    const std::wstring key_id = row.id;
    if (!can_copy(row)) {
      // 密钥值 carries the key itself: a row whose station has no key says
      // 未提供, and a row the sheet cannot read states nothing.
      value_text.Text(winrt::hstring(row.hint.empty() ? empty_label : std::wstring{}));
      return;
    }
    auto revealed = revealed_values->find(key_id);
    if (revealed != revealed_values->end()) {
      value_text.Text(winrt::hstring(EllipsizeMiddle(revealed->second)));
      return;
    }
    // The key is read by the pass, which starts with this row: the sheet shows
    // what it can read rather than a stand-in label.
    value_text.Text(L"");
    fill_row_keys();
  };

  auto load_detail = [&]() {
    const int32_t selected = selected_index();
    const bool has_row = selected >= 0 && static_cast<size_t>(selected) < rows->size();
    const bool editable = !toggle_on() && has_row;
    // The list header keeps only the +/- it can perform: no group to place a
    // key in, or no selected key, hides the control instead of greying it out.
    const bool can_add_key = !groups.empty() && !toggle_on();
    add_button.IsEnabled(can_add_key);
    add_button.Visibility(can_add_key ? xaml::Visibility::Visible : xaml::Visibility::Collapsed);
    remove_button.IsEnabled(editable);
    remove_button.Visibility(editable ? xaml::Visibility::Visible : xaml::Visibility::Collapsed);
    enabled_box.IsEnabled(editable);
    name_box.IsEnabled(editable);
    group_picker.IsEnabled(editable && !groups.empty());
    *syncing = true;
    if (!has_row) {
      enabled_box.IsChecked(false);
      name_box.Text(L"");
      group_picker.SelectedIndex(-1);
      multiplier_value.Text(winrt::hstring(labels.empty_label));
      // Nothing is selected, so no read may write into the value row.
      value_text.Text(winrt::hstring(labels.empty_label));
      rebuild_models({});
      copy_button.IsEnabled(false);
      show_copy_status(L"");
    } else {
      auto const& row = (*rows)[static_cast<size_t>(selected)];
      enabled_box.IsChecked(row.enabled);
      name_box.Text(winrt::hstring(row.name));
      group_picker.SelectedIndex(group_index_for(row.group_id));
      multiplier_value.Text(winrt::hstring(row.multiplier.empty() ? labels.empty_label : row.multiplier));
      // Core reports only whether a credential exists; the value row shows the
      // real key, which the sheet reads through Core's native capability.
      reveal_value(row);
      rebuild_models(row.models);
      copy_button.IsEnabled(can_copy(row));
    }
    *syncing = false;
  };

  auto rebuild = [&]() {
    *syncing = true;
    const int32_t selected = selected_index();
    list.Items().Clear();
    for (size_t index = 0; index < rows->size(); ++index) {
      auto const& row = (*rows)[index];
      controls::Grid grid;
      grid.MinHeight(22);
      grid.Background(index % 2 == 1
          ? theme_brush(L"SubtleFillColorTransparentBrush", winrt::Windows::UI::Color{20, 128, 128, 128})
          : theme_brush(L"ControlFillColorDefaultBrush", winrt::Windows::UI::Color{255, 255, 255, 255}));
      controls::ColumnDefinition row_name;
      controls::ColumnDefinition row_group;
      controls::ColumnDefinition row_detail;
      row_group.Width(xaml::GridLengthHelper::FromPixels(kGroupColumnWidth));
      row_detail.Width(xaml::GridLengthHelper::FromPixels(kMultiplierColumnWidth));
      grid.ColumnDefinitions().Append(row_name);
      grid.ColumnDefinitions().Append(row_group);
      grid.ColumnDefinitions().Append(row_detail);
      controls::TextBlock name;
      name.FontSize(kUIFontSize);
      name.Margin(xaml::Thickness{8, 0, 8, 0});
      name.VerticalAlignment(xaml::VerticalAlignment::Center);
      name.Text(winrt::hstring(row.name));
      name.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
      // The list reports the store as it stands: a column keeps its own value,
      // and a row the draft will create or delete is dimmed instead of carrying
      // status text in 倍率.
      name.Foreground(is_staged(row)
          ? theme_brush(L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115})
          : theme_brush(L"TextFillColorPrimaryBrush", winrt::Windows::UI::Color{255, 30, 30, 30}));
      controls::Grid::SetColumn(name, 0);
      grid.Children().Append(name);
      controls::TextBlock group;
      group.FontSize(kUIFontSize);
      group.Margin(xaml::Thickness{0, 0, 8, 0});
      group.VerticalAlignment(xaml::VerticalAlignment::Center);
      group.Text(winrt::hstring(current_group_text(row, labels)));
      group.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
      group.Foreground(is_staged(row)
          ? theme_brush(L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115})
          : theme_brush(L"TextFillColorPrimaryBrush", winrt::Windows::UI::Color{255, 30, 30, 30}));
      controls::Grid::SetColumn(group, 1);
      grid.Children().Append(group);
      controls::TextBlock detail_text;
      detail_text.FontSize(kUIFontSize);
      detail_text.Margin(xaml::Thickness{0, 0, 8, 0});
      detail_text.VerticalAlignment(xaml::VerticalAlignment::Center);
      auto const detail_presentation = presentation(row, labels);
      detail_text.FontSize(kUIFontSize);
      detail_text.Margin(xaml::Thickness{0, 0, 8, 0});
      detail_text.VerticalAlignment(xaml::VerticalAlignment::Center);
      detail_text.Text(winrt::hstring(detail_presentation));
      detail_text.Foreground(is_staged(row)
          ? theme_brush(L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115})
          : theme_brush(L"TextFillColorPrimaryBrush", winrt::Windows::UI::Color{255, 30, 30, 30}));
      controls::Grid::SetColumn(detail_text, 2);
      grid.Children().Append(detail_text);
      list.Items().Append(grid);
    }
    if (selected >= 0 && static_cast<size_t>(selected) < rows->size()) {
      list.SelectedIndex(selected);
    } else {
      list.SelectedIndex(-1);
    }
    *syncing = false;
    load_detail();
    refresh_apply();
  };

  auto commit_name = [&]() {
    const int32_t selected = selected_index();
    if (selected < 0 || static_cast<size_t>(selected) >= rows->size()) return;
    std::wstring value(name_box.Text());
    const auto first = value.find_first_not_of(L" \t");
    const auto last = value.find_last_not_of(L" \t");
    value = first == std::wstring::npos ? std::wstring{} : value.substr(first, last - first + 1);
    if (value.empty()) {
      *syncing = true;
      name_box.Text(winrt::hstring((*rows)[static_cast<size_t>(selected)].name));
      *syncing = false;
      return;
    }
    if (value == (*rows)[static_cast<size_t>(selected)].name) return;
    (*rows)[static_cast<size_t>(selected)].name = value;
    rebuild();
  };

  list.SelectionChanged([&](auto const&, auto const&) {
    if (*syncing) return;
    load_detail();
  });
  add_button.Click([&](auto const&, auto const&) {
    if (groups.empty()) return;
    std::wstring base = labels.new_key_name.empty() ? std::wstring(L"New key") : labels.new_key_name;
    std::wstring name = base;
    int suffix = 2;
    auto taken = [&rows](std::wstring const& candidate) {
      for (auto const& row : *rows) {
        if (row.name == candidate) return true;
      }
      return false;
    };
    while (taken(name)) {
      name = base + L" " + std::to_wstring(suffix);
      ++suffix;
    }
    SheetRow row;
    row.id = L"draft";
    row.name = name;
    row.group_id = groups.front().id;
    row.group_label = groups.front().name;
    row.multiplier = groups.front().rate;
    row.original_group_id = row.group_id;
    row.draft = true;
    rows->push_back(std::move(row));
    rebuild();
    list.SelectedIndex(static_cast<int32_t>(rows->size()) - 1);
    name_box.Focus(winrt::Microsoft::UI::Xaml::FocusState::Programmatic);
  });
  remove_button.Click([&](auto const&, auto const&) {
    const int32_t selected = selected_index();
    if (selected < 0 || static_cast<size_t>(selected) >= rows->size()) return;
    if ((*rows)[static_cast<size_t>(selected)].draft) {
      rows->erase(rows->begin() + selected);
      rebuild();
      list.SelectedIndex(-1);
      load_detail();
    } else {
      (*rows)[static_cast<size_t>(selected)].deleted = !(*rows)[static_cast<size_t>(selected)].deleted;
      *staged_deletes = 0;
      for (auto const& row : *rows) {
        if (!row.draft && row.deleted) ++*staged_deletes;
      }
      rebuild();
      list.SelectedIndex(selected);
      load_detail();
    }
  });
  enabled_box.Click([&](auto const& sender, auto const&) {
    if (*syncing) return;
    const int32_t selected = selected_index();
    if (selected < 0 || static_cast<size_t>(selected) >= rows->size()) return;
    auto checked = sender.as<controls::CheckBox>().IsChecked();
    (*rows)[static_cast<size_t>(selected)].enabled = checked && checked.Value();
    rebuild();
    list.SelectedIndex(selected);
  });
  group_picker.SelectionChanged([&](auto const&, auto const&) {
    if (*syncing) return;
    const int32_t selected = selected_index();
    const auto group_index = group_picker.SelectedIndex();
    if (selected < 0 || static_cast<size_t>(selected) >= rows->size()) return;
    if (group_index < 0 || static_cast<size_t>(group_index) >= groups.size()) return;
    (*rows)[static_cast<size_t>(selected)].group_id = groups[static_cast<size_t>(group_index)].id;
    (*rows)[static_cast<size_t>(selected)].group_label = groups[static_cast<size_t>(group_index)].name;
    // Re-grouping changes what the key costs, so 倍率 follows the choice.
    (*rows)[static_cast<size_t>(selected)].multiplier = groups[static_cast<size_t>(group_index)].rate;
    rebuild();
    list.SelectedIndex(selected);
  });
  name_box.LostFocus([&](auto const&, auto const&) {
    if (*syncing) return;
    commit_name();
  });
  name_box.KeyDown([&](auto const&, winrt::Microsoft::UI::Xaml::Input::KeyRoutedEventArgs const& args) {
    if (args.Key() != winrt::Windows::System::VirtualKey::Enter) return;
    commit_name();
  });
  toggle.Click([&](auto const&, auto const&) {
    // Returning to 自动分组 restores the staged deletes the automatic layout had
    // marked, so the list never shows them while it is on.
    if (toggle_on() && *staged_deletes > 0) {
      for (auto& row : *rows) row.deleted = false;
      *staged_deletes = 0;
      rebuild();
    }
    load_detail();
    refresh_apply();
  });
  close.Click([&](auto const&, auto const&) {
    // Close drops the staged draft, so a sheet that would lose edits asks first.
    if (has_staged_changes() && !Confirm(labels.discard_title, labels.discard_body, labels.discard_confirm)) return;
    *applied = false;
    dialog.Close();
  });
  apply.Click([&](auto const&, auto const&) {
    if (!has_staged_changes()) return;
    commit_name();
    *applied = true;
    dialog.Close();
  });

  std::optional<GroupManagerResult> outcome;
  bool finished = false;
  dialog.Closed([&](auto const&, auto const&) {
    finished = true;
    if (!*applied || !has_staged_changes()) return;
    GroupManagerResult result;
    auto checked = toggle.IsChecked();
    result.auto_grouping = checked && checked.Value();
    for (auto const& row : *rows) {
      if (row.draft) {
        if (!row.name.empty()) result.creates.emplace_back(row.name, row.group_id);
        continue;
      }
      if (row.deleted) {
        result.deletes.push_back(row.id);
        continue;
      }
      if (row.name != row.original_name || row.group_id != row.original_group_id || row.enabled != row.original_enabled) {
        result.updates.push_back(GroupManagerUpdate{row.id, row.name, row.group_id, row.enabled});
      }
    }
    outcome = std::move(result);
  });

  rebuild();
  // The selection drives the detail column, so the sheet opens on the first
  // key instead of a blank form.
  if (!rows->empty()) list.SelectedIndex(0);
  load_detail();
  refresh_apply();
  // The list is sized for the longest model list any key carries (bounded to
  // what the sheet shows at once), so switching groups scrolls instead of
  // resizing the window under the pointer.
  size_t longest_models = 0;
  for (auto const& row : *rows) longest_models = std::max(longest_models, row.models.size());
  const double models_height = static_cast<double>(std::max<size_t>(3, std::min<size_t>(12, longest_models))) * kModelRowHeight;
  models_scroll.Height(models_height);
  const double detail_height = 256.0 + models_height;
  const double window_height = 560 + std::max(0.0, detail_height - 300.0);
  if (!RunOwnedModalWindow(dialog, window_handle_, {590, window_height}, finished)) return std::nullopt;
  return outcome;
}

bool WinUI3NativeLeaf::SetLaunchAtLogin(bool enabled) {
  std::wstring executable = ModulePath();
  if (executable.empty()) return false;
  HKEY key = nullptr;
  if (RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, nullptr, 0,
      KEY_QUERY_VALUE | KEY_SET_VALUE, nullptr, &key, nullptr) != ERROR_SUCCESS) return false;
  DWORD type = 0;
  DWORD size = 0;
  LONG existing = RegQueryValueExW(key, L"YoungRouter", nullptr, &type, nullptr, &size);
  LONG result = ERROR_SUCCESS;
  if (enabled) {
    std::wstring command = L"\"" + executable + L"\"";
    result = RegSetValueExW(key, L"YoungRouter", 0, REG_SZ,
        reinterpret_cast<BYTE const*>(command.c_str()), static_cast<DWORD>((command.size() + 1) * sizeof(wchar_t)));
  } else if (existing == ERROR_SUCCESS) {
    result = RegDeleteValueW(key, L"YoungRouter");
  } else if (existing != ERROR_FILE_NOT_FOUND) {
    result = existing;
  }
  RegCloseKey(key);
  return result == ERROR_SUCCESS;
}

void WinUI3NativeLeaf::ShowVersion() const {
  std::wstring text = VersionText();
  MessageBoxW(window_handle_, text.c_str(), Localized("appTitle", L"Young Router").c_str(), MB_OK | MB_ICONINFORMATION);
}

WinUI3NativeLeaf::VersionInfoResult WinUI3NativeLeaf::VersionInfo() const {
  VersionInfoResult result;
  try {
    auto version = winrt::Windows::ApplicationModel::Package::Current().Id().Version();
    wchar_t packaged[64]{};
    swprintf_s(packaged, L"%u.%u.%u", version.Major, version.Minor, version.Build);
    result.app = packaged;
  } catch (...) {
    DWORD size = GetFileVersionInfoSizeW(ModulePath().c_str(), nullptr);
    if (size > 0) {
      std::vector<BYTE> data(size);
      VS_FIXEDFILEINFO* info = nullptr;
      UINT info_size = 0;
      if (GetFileVersionInfoW(ModulePath().c_str(), 0, size, data.data()) &&
          VerQueryValueW(data.data(), L"\\", reinterpret_cast<void**>(&info), &info_size) && info != nullptr) {
        wchar_t buffer[64]{};
        swprintf_s(buffer, L"%u.%u.%u",
            HIWORD(info->dwFileVersionMS), LOWORD(info->dwFileVersionMS), HIWORD(info->dwFileVersionLS));
        result.app = buffer;
      }
    }
  }
  try {
    // The About pane shows the LiteLLM runtime version recorded beside the
    // bundled Core runtime.
    std::filesystem::path path(ModulePath());
    path = path.parent_path() / L"Core" / L"runtime" / L"LITELLM_VERSION";
    std::wifstream stream(path);
    std::wstring line;
    if (stream && std::getline(stream, line)) {
      while (!line.empty() && (line.back() == L'\r' || line.back() == L' ')) line.pop_back();
      result.litellm = line;
    }
  } catch (...) {
  }
  return result;
}

void WinUI3NativeLeaf::OpenExternalURL(std::wstring_view url) {
  if (url.empty() || url.size() > 8192) return;
  try {
    winrt::Windows::Foundation::Uri parsed{std::wstring(url)};
    auto scheme = std::wstring(parsed.SchemeName());
    std::transform(scheme.begin(), scheme.end(), scheme.begin(), ::towlower);
    if (scheme != L"http" && scheme != L"https") return;
  } catch (...) {
    return;
  }
  std::wstring command(url);
  ShellExecuteW(nullptr, L"open", command.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void WinUI3NativeLeaf::RevealFile(std::wstring_view path) {
  if (path.empty() || path.size() > 4096) return;
  std::wstring target(path);
  if (target.find_first_of(L"\r\n") != std::wstring::npos) return;
  // Only an absolute drive-letter or UNC path is revealable; the settings pane
  // never hands over a relative path, and a relative one would resolve against
  // the app's working directory.
  const bool absolute =
      (target.size() >= 3 && target[1] == L':' && (target[2] == L'\\' || target[2] == L'/')) ||
      (target.size() >= 2 && target[0] == L'\\' && target[1] == L'\\');
  if (!absolute) return;
  try {
    std::error_code error;
    const bool exists = std::filesystem::exists(std::filesystem::path(target), error) && !error;
    std::wstring arguments;
    if (exists) {
      arguments = L"/select,\"" + target + L"\"";
    } else {
      // The pane lists files the user may not have created yet; open the
      // nearest existing parent directory instead of doing nothing.
      std::filesystem::path directory = std::filesystem::path(target).parent_path();
      while (!directory.empty()) {
        error.clear();
        if (std::filesystem::exists(directory, error) && !error) break;
        if (error) return;
        const std::filesystem::path parent = directory.parent_path();
        if (parent == directory) return;
        directory = parent;
      }
      if (directory.empty()) return;
      arguments = L"\"" + directory.wstring() + L"\"";
    }
    ShellExecuteW(nullptr, L"open", L"explorer.exe", arguments.c_str(), nullptr, SW_SHOWNORMAL);
  } catch (...) {
  }
}

void WinUI3NativeLeaf::Quit() {
  if (quit_in_progress_ || quitting_) return;
  quit_in_progress_ = true;
  auto window = window_handle_;
  if (window == nullptr) return;
  std::thread([window] {
    try {
      CoreIPCBridge::Shared().Stop();
    } catch (...) {
    }
    PostMessageW(window, kQuitMessage, 0, 0);
  }).detach();
}

void WinUI3NativeLeaf::EnsureTray() {
  if (tray_visible_ || window_handle_ == nullptr) return;
  tray_.cbSize = sizeof(NOTIFYICONDATAW);
  tray_.hWnd = window_handle_;
  tray_.uID = 1;
  tray_.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE;
  tray_.uCallbackMessage = kTrayMessage;
  tray_.hIcon = LoadIconW(nullptr, IDI_APPLICATION);
  wcsncpy_s(tray_.szTip, status_title_.empty() ? L"Young Router" : status_title_.c_str(), _TRUNCATE);
  tray_visible_ = Shell_NotifyIconW(NIM_ADD, &tray_) == TRUE;
}

void WinUI3NativeLeaf::DispatchDefaultTrayAction() {
  auto route = std::find_if(actions_.begin(), actions_.end(), [](auto const& action) {
    return action.enabled && action.id.rfind(L"open-", 0) == 0;
  });
  if (route == actions_.end()) return;
  DispatchTrayAction(static_cast<size_t>(route - actions_.begin()));
}

void WinUI3NativeLeaf::DispatchTrayAction(size_t index) {
  if (index >= actions_.size()) return;
  auto const& item = actions_[index];
  if (!item.enabled) return;
  if (item.id == L"toggle-autostart") {
    DispatchAction(WideToUtf8(item.id));
    return;
  }
  if (item.id == L"show-version") {
    ShowVersion();
    return;
  }
  if (item.id == L"quit") {
    Quit();
    return;
  }
  DispatchAction(WideToUtf8(item.id));
}

void WinUI3NativeLeaf::ShowTrayMenu() {
  if (window_handle_ == nullptr || actions_.empty()) return;
  HMENU menu = CreatePopupMenu();
  if (!menu) return;
  if (!status_title_.empty()) {
    AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status_title_.c_str());
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
  }
  bool needs_separator = false;
  auto add_separator = [&menu, &needs_separator]() {
    if (needs_separator) AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    needs_separator = false;
  };
  HMENU language_menu = CreatePopupMenu();
  for (size_t index = 0; index < actions_.size(); ++index) {
    auto const& action = actions_[index];
    const bool is_language_choice = action.id == L"set-language-system" ||
        action.id == L"set-language-en" || action.id == L"set-language-zh-Hans";
    if (action.id == L"language-picker") continue;
    UINT flags = MF_STRING | (action.enabled ? MF_ENABLED : MF_GRAYED);
    if (action.checked) flags |= MF_CHECKED;
    if (is_language_choice) {
      if (language_menu != nullptr) {
        AppendMenuW(language_menu, flags,
            kTrayMenuFirstCommand + static_cast<UINT>(index), action.title.c_str());
      }
      continue;
    }
    if (action.id == L"open-general-settings" || action.id == L"open-providers-models" ||
        action.id == L"webdav-status" || action.id == L"open-data-management" ||
        action.id == L"open-logs" || action.id == L"show-version") {
      add_separator();
    }
    AppendMenuW(menu, flags,
        kTrayMenuFirstCommand + static_cast<UINT>(index), action.title.c_str());
    if (action.id == L"toggle-autostart" ||
        action.id == L"open-data-management" || action.id == L"open-logs") {
      needs_separator = true;
    }
  }
  if (language_menu != nullptr && GetMenuItemCount(language_menu) > 0) {
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    auto label = std::find_if(actions_.begin(), actions_.end(), [](auto const& action) {
      return action.id == L"language-picker";
    });
    AppendMenuW(menu, MF_POPUP, reinterpret_cast<UINT_PTR>(language_menu),
        label == actions_.end() ? L"Language" : label->title.c_str());
  } else if (language_menu != nullptr) {
    DestroyMenu(language_menu);
  }
  POINT tray_point{};
  GetCursorPos(&tray_point);
  SetForegroundWindow(window_handle_);
  TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, tray_point.x, tray_point.y, 0, window_handle_, nullptr);
  DestroyMenu(menu);
}

void WinUI3NativeLeaf::InstallWindowHook() {
  if (window_handle_ == nullptr || previous_window_proc_ != nullptr) return;
  SetPropW(window_handle_, L"YoungRouter.NativeLeaf", reinterpret_cast<HANDLE>(this));
  previous_window_proc_ = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(window_handle_, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(TrayWindowProc)));
  if (previous_window_proc_ != nullptr) SetPropW(window_handle_, L"YoungRouter.PreviousWindowProc", reinterpret_cast<HANDLE>(previous_window_proc_));
}

std::wstring WinUI3NativeLeaf::VersionText() const {
  try {
    auto version = winrt::Windows::ApplicationModel::Package::Current().Id().Version();
    wchar_t packaged[256]{};
    swprintf_s(packaged, L"%s %u.%u.%u.%u", Localized("version", L"Version").c_str(),
        version.Major, version.Minor, version.Build, version.Revision);
    return packaged;
  } catch (...) {
  }
  wchar_t buffer[256]{};
  DWORD size = GetFileVersionInfoSizeW(ModulePath().c_str(), nullptr);
  if (size > 0) {
    std::vector<BYTE> data(size);
    VS_FIXEDFILEINFO* info = nullptr;
    UINT info_size = 0;
    if (GetFileVersionInfoW(ModulePath().c_str(), 0, size, data.data()) &&
        VerQueryValueW(data.data(), L"\\", reinterpret_cast<void**>(&info), &info_size) && info != nullptr) {
      swprintf_s(buffer, L"%s %u.%u.%u.%u", Localized("version", L"Version").c_str(),
          HIWORD(info->dwFileVersionMS), LOWORD(info->dwFileVersionMS),
          HIWORD(info->dwFileVersionLS), LOWORD(info->dwFileVersionLS));
      return buffer;
    }
  }
  return Localized("appTitle", L"Young Router");
}

std::wstring WinUI3NativeLeaf::Localized(std::string const& key, std::wstring_view fallback) const {
  auto found = strings_.find(key);
  return found == strings_.end() || found->second.empty() ? std::wstring(fallback) : found->second;
}

std::wstring WinUI3NativeLeaf::RouteTitle(std::wstring_view route) const {
  // Settings panes share one window, so its title is the app name while the
  // sidebar selection names the active pane (System Settings style).
  if (route == L"home" || route == L"general-settings" || route == L"providers-models" ||
      route == L"codex-settings" || route == L"claude-settings" || route == L"runtime-settings" ||
      route == L"data-management" || route == L"logs") {
    return Localized("appTitle", L"Young Router");
  }
  if (route == L"provider-wizard") return Localized("routeProviderWizard", L"Add Provider");
  if (route == L"file-editor") return Localized("routeFileEditor", L"Edit File");
  return Localized("appTitle", L"Young Router");
}

void WinUI3NativeLeaf::RemoveTray() {
  if (!tray_visible_) return;
  Shell_NotifyIconW(NIM_DELETE, &tray_);
  tray_visible_ = false;
}

}
