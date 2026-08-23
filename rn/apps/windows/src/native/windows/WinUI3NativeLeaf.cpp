#include "pch.h"
#include "WinUI3NativeLeaf.h"
#include "CoreIPCBridge.h"

#include <windows.h>
#include <dwmapi.h>
#include <shlobj.h>
#include <algorithm>
#include <cmath>
#include <cwctype>
#include <filesystem>
#include <thread>
#include <winreg.h>
#include <winver.h>
#include <winrt/Windows.ApplicationModel.h>
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
  if (route == L"providers-models") return {780, 560};
  if (route == L"relay-accounts") return {780, 440};
  if (route == L"relay-add") return {540, 420};
  if (route == L"provider-wizard") return {540, 420};
  if (route == L"codex-settings" || route == L"claude-settings") return {1100, 640};
  if (route == L"runtime-settings") return {800, 520};
  if (route == L"data-management") return {500, 180};
  if (route == L"logs") return {640, 420};
  // The hidden menu-bar host has no route surface. Keep its fallback small so
  // it never inherits a settings window's minimum size before a route opens.
  return {320, 160};
}

ContentSize RouteInitialContentSize(std::wstring_view route) {
  if (route == L"providers-models") return {780, 560};
  if (route == L"relay-accounts") return {820, 480};
  if (route == L"relay-add") return {620, 460};
  if (route == L"provider-wizard") return {620, 460};
  if (route == L"codex-settings" || route == L"claude-settings") return {1160, 700};
  if (route == L"runtime-settings") return {1080, 620};
  if (route == L"data-management") return {620, 220};
  if (route == L"logs") return {900, 580};
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
  auto leaf = reinterpret_cast<LiteLLMMenu::WinUI3NativeLeaf*>(GetPropW(window, L"LiteLLMMenu.NativeLeaf"));
  auto previous = leaf ? reinterpret_cast<WNDPROC>(GetPropW(window, L"LiteLLMMenu.PreviousWindowProc")) : nullptr;
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
  path /= L"LiteLLM Menu";
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
  LiteLLMMenu::DisableWindowTransitions(dialog_handle);
  const bool disable_owner = owner != nullptr && IsWindow(owner);
  if (disable_owner) {
    SetWindowLongPtrW(dialog_handle, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(owner));
    EnableWindow(owner, FALSE);
  }

  auto window_id = winrt::Microsoft::UI::GetWindowIdFromWindow(dialog_handle);
  const auto frame = LiteLLMMenu::FrameTrackSizeForContentDips(
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

namespace LiteLLMMenu {

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
    RemovePropW(window_handle_, L"LiteLLMMenu.PreviousWindowProc");
    RemovePropW(window_handle_, L"LiteLLMMenu.NativeLeaf");
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
    auto tooltip = status_title_.empty() ? Localized("appTitle", L"LiteLLM Menu") : status_title_;
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

bool WinUI3NativeLeaf::SetLaunchAtLogin(bool enabled) {
  std::wstring executable = ModulePath();
  if (executable.empty()) return false;
  HKEY key = nullptr;
  if (RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, nullptr, 0,
      KEY_QUERY_VALUE | KEY_SET_VALUE, nullptr, &key, nullptr) != ERROR_SUCCESS) return false;
  DWORD type = 0;
  DWORD size = 0;
  LONG existing = RegQueryValueExW(key, L"LiteLLMMenu", nullptr, &type, nullptr, &size);
  LONG result = ERROR_SUCCESS;
  if (enabled) {
    std::wstring command = L"\"" + executable + L"\"";
    result = RegSetValueExW(key, L"LiteLLMMenu", 0, REG_SZ,
        reinterpret_cast<BYTE const*>(command.c_str()), static_cast<DWORD>((command.size() + 1) * sizeof(wchar_t)));
  } else if (existing == ERROR_SUCCESS) {
    result = RegDeleteValueW(key, L"LiteLLMMenu");
  } else if (existing != ERROR_FILE_NOT_FOUND) {
    result = existing;
  }
  RegCloseKey(key);
  return result == ERROR_SUCCESS;
}

void WinUI3NativeLeaf::ShowVersion() const {
  std::wstring text = VersionText();
  MessageBoxW(window_handle_, text.c_str(), Localized("appTitle", L"LiteLLM Menu").c_str(), MB_OK | MB_ICONINFORMATION);
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
  wcsncpy_s(tray_.szTip, status_title_.empty() ? L"LiteLLM Menu" : status_title_.c_str(), _TRUNCATE);
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
    if (action.id == L"language-menu") continue;
    UINT flags = MF_STRING | (action.enabled ? MF_ENABLED : MF_GRAYED);
    if (action.checked) flags |= MF_CHECKED;
    if (is_language_choice) {
      if (language_menu != nullptr) {
        AppendMenuW(language_menu, flags,
            kTrayMenuFirstCommand + static_cast<UINT>(index), action.title.c_str());
      }
      continue;
    }
    if (action.id == L"open-providers-models" || action.id == L"webdav-status" ||
        action.id == L"open-data-management" || action.id == L"open-logs" ||
        action.id == L"show-version") {
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
      return action.id == L"language-menu";
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
  SetPropW(window_handle_, L"LiteLLMMenu.NativeLeaf", reinterpret_cast<HANDLE>(this));
  previous_window_proc_ = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(window_handle_, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(TrayWindowProc)));
  if (previous_window_proc_ != nullptr) SetPropW(window_handle_, L"LiteLLMMenu.PreviousWindowProc", reinterpret_cast<HANDLE>(previous_window_proc_));
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
  return Localized("appTitle", L"LiteLLM Menu");
}

std::wstring WinUI3NativeLeaf::Localized(std::string const& key, std::wstring_view fallback) const {
  auto found = strings_.find(key);
  return found == strings_.end() || found->second.empty() ? std::wstring(fallback) : found->second;
}

std::wstring WinUI3NativeLeaf::RouteTitle(std::wstring_view route) const {
  if (route == L"home") return Localized("appTitle", L"LiteLLM Menu");
  if (route == L"providers-models") return Localized("routeProvidersModels", L"Providers & Models");
  if (route == L"relay-accounts") return Localized("routeRelayAccounts", L"Service Provider Management");
  if (route == L"relay-add") return Localized("routeRelayAdd", L"Add Relay Account");
  if (route == L"provider-wizard") return Localized("routeProviderWizard", L"Add Provider");
  if (route == L"codex-settings" || route == L"claude-settings") {
    return Localized("routeCodexSettings", L"Codex / Claude Settings");
  }
  if (route == L"runtime-settings") return Localized("routeRuntimeSettings", L"Runtime Settings");
  if (route == L"data-management") return Localized("routeDataManagement", L"Data Management");
  if (route == L"logs") return Localized("routeLogs", L"Logs");
  return Localized("appTitle", L"LiteLLM Menu");
}

void WinUI3NativeLeaf::RemoveTray() {
  if (!tray_visible_) return;
  Shell_NotifyIconW(NIM_DELETE, &tray_);
  tray_visible_ = false;
}

}
