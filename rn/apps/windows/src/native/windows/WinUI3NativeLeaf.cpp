#include "pch.h"
#include "WinUI3NativeLeaf.h"
#include "CoreIPCBridge.h"

#include <windows.h>
#include <algorithm>
#include <cmath>
#include <cwctype>
#include <thread>
#include <winreg.h>
#include <winver.h>
#include <winrt/Windows.ApplicationModel.h>
#include <winrt/Microsoft.UI.Interop.h>
#include <winrt/Microsoft.UI.Windowing.h>
#include <winrt/Microsoft.UI.Xaml.Input.h>

namespace {
constexpr UINT kTrayMessage = WM_APP + 31;
constexpr UINT kQuitMessage = WM_APP + 32;
constexpr UINT kTrayMenuFirstCommand = 41000;
constexpr size_t kMaxEditorBytes = 2 * 1024 * 1024;

struct ContentSize {
  LONG width;
  LONG height;
};

ContentSize RouteMinimumContentSize(std::wstring_view route) {
  // These are the legacy window content sizes in 96-DPI logical pixels. They
  // deliberately live at the native window boundary: React owns the shared
  // page, while Win32 owns frame constraints and DPI conversion.
  if (route == L"providers-models") return {1052, 560};
  if (route == L"codex-settings" || route == L"claude-settings") return {1020, 620};
  if (route == L"runtime-settings") return {760, 500};
  if (route == L"configuration-package") return {420, 132};
  if (route == L"webdav-settings") return {680, 386};
  if (route == L"logs") return {640, 420};
  // The hidden menu-bar host has no route surface. Keep its fallback small so
  // it never inherits a settings window's minimum size before a route opens.
  return {320, 160};
}

ContentSize RouteInitialContentSize(std::wstring_view route) {
  if (route == L"providers-models") return {1052, 600};
  if (route == L"codex-settings" || route == L"claude-settings") return {1120, 680};
  if (route == L"runtime-settings") return {1080, 620};
  if (route == L"configuration-package") return {420, 208};
  if (route == L"webdav-settings") return {680, 386};
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

void SelectNext(
    winrt::Microsoft::UI::Xaml::Controls::TextBox const& editor,
    winrt::Microsoft::UI::Xaml::Controls::TextBox const& find) {
  std::wstring needle(find.Text());
  if (needle.empty()) return;
  std::wstring text(editor.Text());
  size_t start = static_cast<size_t>(std::max(0, editor.SelectionStart() + editor.SelectionLength()));
  auto found = text.find(needle, start);
  if (found == std::wstring::npos) found = text.find(needle);
  if (found == std::wstring::npos) return;
  editor.Select(static_cast<int>(found), static_cast<int>(needle.size()));
  editor.Focus(winrt::Microsoft::UI::Xaml::FocusState::Programmatic);
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
    winrt::Windows::Graphics::SizeInt32 size,
    bool& finished) {
  HWND dialog_handle = nullptr;
  winrt::check_hresult(dialog.as<::IWindowNative>()->get_WindowHandle(&dialog_handle));
  const bool disable_owner = owner != nullptr && IsWindow(owner);
  if (disable_owner) {
    SetWindowLongPtrW(dialog_handle, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(owner));
    EnableWindow(owner, FALSE);
  }

  auto window_id = winrt::Microsoft::UI::GetWindowIdFromWindow(dialog_handle);
  winrt::Microsoft::UI::Windowing::AppWindow::GetFromWindowId(window_id).Resize(size);
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
}  // namespace

namespace LiteLLMMenu {

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
  InstallWindowHook();
  EnsureTray();
}

void WinUI3NativeLeaf::Initialize(HWND window_handle) {
  window_handle_ = window_handle;
  InstallWindowHook();
  EnsureTray();
}

void WinUI3NativeLeaf::SetStatus(std::wstring_view title, bool running) {
  status_title_ = title;
  service_running_ = running;
  EnsureTray();
  if (tray_visible_) {
    wcsncpy_s(tray_.szTip, status_title_.c_str(), _TRUNCATE);
    Shell_NotifyIconW(NIM_MODIFY, &tray_);
  }
}

void WinUI3NativeLeaf::SetActions(std::vector<NativeMenuAction> const& actions) {
  actions_ = actions;
}

void WinUI3NativeLeaf::SetLocalization(std::map<std::string, std::wstring> strings) {
  for (auto& [key, value] : strings) {
    if (!value.empty()) strings_.insert_or_assign(std::move(key), std::move(value));
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

bool WinUI3NativeLeaf::SetWindowContentSize(double width, double height) {
  constexpr double kMinimumContentExtent = 128.0;
  constexpr double kMaximumContentExtent = 8192.0;
  if (window_handle_ == nullptr || !std::isfinite(width) || !std::isfinite(height) ||
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
  body.Text(winrt::hstring(message));
  body.TextWrapping(xaml::TextWrapping::Wrap);
  root.Children().Append(body);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  controls::Button confirm;
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

std::optional<std::vector<std::wstring>> WinUI3NativeLeaf::ChooseModelsToAdd(
    std::vector<std::wstring> models,
    std::wstring provider_name,
    std::wstring key_name) {
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  struct ChooserState {
    std::vector<std::wstring> models;
    std::vector<bool> selected;
    std::wstring query;
    controls::ListView list{nullptr};
    controls::TextBlock summary{nullptr};
    controls::Button all{nullptr};
    controls::Button invert{nullptr};
    controls::Button add{nullptr};
    xaml::Window dialog{nullptr};
    bool finished = false;
    bool accepted = false;
  };
  auto state = std::make_shared<ChooserState>();
  state->models = std::move(models);
  state->selected.assign(state->models.size(), false);

  xaml::Window dialog;
  state->dialog = dialog;
  dialog.Title(L"Choose Models to Add");

  controls::StackPanel root;
  root.Spacing(8);
  root.Margin(xaml::Thickness{20, 16, 20, 16});

  controls::TextBlock title;
  title.Text(L"Choose models to add");
  title.FontSize(16);
  root.Children().Append(title);

  controls::TextBlock subtitle;
  subtitle.Text(L"Provider: " + provider_name + L"    Key: " + key_name);
  subtitle.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
  root.Children().Append(subtitle);

  controls::TextBox search;
  search.PlaceholderText(L"Search models");
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
  all.Content(winrt::box_value(L"All"));
  controls::Button invert;
  invert.Content(winrt::box_value(L"Invert"));
  state->all = all;
  state->invert = invert;
  selection_buttons.Children().Append(all);
  selection_buttons.Children().Append(invert);
  controls_row.Children().Append(selection_buttons);
  controls::TextBlock summary;
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
  root.Children().Append(list);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  controls::Button add;
  add.Content(winrt::box_value(L"Add Selected"));
  add.IsEnabled(false);
  state->add = add;
  actions.Children().Append(cancel);
  actions.Children().Append(add);
  root.Children().Append(actions);
  dialog.Content(root);

  auto weak_state = std::weak_ptr<ChooserState>(state);
  auto refresh = [weak_state] {
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
      item.Content(winrt::box_value(state->models[index]));
      item.IsChecked(state->selected[index]);
      item.HorizontalAlignment(xaml::HorizontalAlignment::Stretch);
      item.Click([weak_state, index](auto const& sender, auto const&) {
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
              ? std::to_wstring(current->models.size()) + L" models"
              : std::to_wstring(visible_count) + L" of " + std::to_wstring(current->models.size()) + L" models";
          if (selected_count > 0) label += L"  |  " + std::to_wstring(selected_count) + L" selected";
          current->summary.Text(label);
        }
      });
      state->list.Items().Append(item);
      ++visible;
    }
    state->all.IsEnabled(visible > 0);
    state->invert.IsEnabled(visible > 0);
    state->add.IsEnabled(selected > 0);
    std::wstring label = query.empty()
        ? std::to_wstring(state->models.size()) + L" models"
        : std::to_wstring(visible) + L" of " + std::to_wstring(state->models.size()) + L" models";
    if (selected > 0) label += L"  |  " + std::to_wstring(selected) + L" selected";
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
  input.MaxLength(16384);
  root.Children().Append(input);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  controls::Button cancel;
  cancel.Content(winrt::box_value(winrt::hstring(Localized("cancel", L"Cancel"))));
  actions.Children().Append(cancel);
  controls::Button clear;
  if (allow_clear && present) {
    clear.Content(winrt::box_value(winrt::hstring(Localized("clear", L"Clear"))));
    actions.Children().Append(clear);
  }
  controls::Button set;
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

std::optional<std::string> WinUI3NativeLeaf::EditNativeText(
    std::string const& content,
    std::string const& language,
    std::wstring const& title) {
  if (content.size() > kMaxEditorBytes) return std::nullopt;
  namespace xaml = winrt::Microsoft::UI::Xaml;
  namespace controls = winrt::Microsoft::UI::Xaml::Controls;

  xaml::Window dialog;
  dialog.Title(title.empty() ? Localized("appTitle", L"LiteLLM Menu") : title);

  controls::Grid root;
  controls::RowDefinition menu_row;
  menu_row.Height(xaml::GridLengthHelper::Auto());
  root.RowDefinitions().Append(menu_row);
  root.RowDefinitions().Append(controls::RowDefinition());
  controls::RowDefinition action_row;
  action_row.Height(xaml::GridLengthHelper::Auto());
  root.RowDefinitions().Append(action_row);

  auto editor = CreateTextEditor();
  editor.Text(Utf8ToWide(content));
  editor.MaxLength(static_cast<int>(kMaxEditorBytes));

  auto find = controls::TextBox();
  find.PlaceholderText(Localized("find", L"Find"));
  find.Margin(xaml::Thickness{8, 8, 8, 4});
  auto find_next = controls::Button();
  find_next.Content(winrt::box_value(Localized("findNext", L"Find Next")));
  find_next.Margin(xaml::Thickness{8, 4, 8, 8});
  find_next.HorizontalAlignment(xaml::HorizontalAlignment::Stretch);
  find_next.Click([editor, find](auto const&, auto const&) { SelectNext(editor, find); });

  auto selector = CreateSelector();
  selector.Header(winrt::box_value(Localized("edit", L"Edit")));
  selector.Items().Append(winrt::box_value(Utf8ToWide(language)));
  selector.SelectedIndex(0);
  selector.Margin(xaml::Thickness{8, 8, 8, 4});

  controls::StackPanel pane;
  pane.Children().Append(selector);
  pane.Children().Append(find);
  pane.Children().Append(find_next);

  auto split = CreateSplitView();
  split.OpenPaneLength(190);
  split.CompactPaneLength(48);
  split.Pane(pane);
  split.Content(editor);
  split.Margin(xaml::Thickness{12, 4, 12, 8});
  split.SetValue(controls::Grid::RowProperty(), winrt::box_value(1));
  root.Children().Append(split);

  controls::MenuBar menu;
  controls::MenuBarItem edit_menu;
  edit_menu.Title(Localized("edit", L"Edit"));
  auto add_edit_item = [&](std::string const& key, std::wstring_view fallback, auto handler) {
    controls::MenuFlyoutItem item;
    item.Text(Localized(key, fallback));
    item.Click(handler);
    edit_menu.Items().Append(item);
  };
  add_edit_item("undo", L"Undo", [editor](auto const&, auto const&) { editor.Undo(); });
  add_edit_item("redo", L"Redo", [editor](auto const&, auto const&) { editor.Redo(); });
  add_edit_item("cut", L"Cut", [editor](auto const&, auto const&) { editor.CutSelectionToClipboard(); });
  add_edit_item("copy", L"Copy", [editor](auto const&, auto const&) { editor.CopySelectionToClipboard(); });
  add_edit_item("paste", L"Paste", [editor](auto const&, auto const&) { editor.PasteFromClipboard(); });
  add_edit_item("selectAll", L"Select All", [editor](auto const&, auto const&) { editor.SelectAll(); });
  add_edit_item("find", L"Find", [find](auto const&, auto const&) {
    find.Focus(xaml::FocusState::Programmatic);
  });
  add_edit_item("findNext", L"Find Next", [editor, find](auto const&, auto const&) {
    SelectNext(editor, find);
  });
  menu.Items().Append(edit_menu);
  root.Children().Append(menu);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.HorizontalAlignment(xaml::HorizontalAlignment::Right);
  actions.Spacing(8);
  actions.Margin(xaml::Thickness{12, 0, 12, 12});
  actions.SetValue(controls::Grid::RowProperty(), winrt::box_value(2));
  auto cancel = controls::Button();
  cancel.Content(winrt::box_value(Localized("cancel", L"Cancel")));
  auto stage = controls::Button();
  stage.Content(winrt::box_value(Localized("stage", L"Stage")));
  actions.Children().Append(cancel);
  actions.Children().Append(stage);
  root.Children().Append(actions);

  struct EditorResult {
    bool finished = false;
    bool accepted = false;
    std::string staged;
  };
  auto result = std::make_shared<EditorResult>();
  cancel.Click([dialog](auto const&, auto const&) { dialog.Close(); });
  stage.Click([dialog, editor, result, this](auto const&, auto const&) {
    std::wstring value(editor.Text());
    std::string encoded = WideToUtf8(value);
    if ((!value.empty() && encoded.empty()) || encoded.size() > kMaxEditorBytes) {
      auto invalid = controls::ContentDialog();
      invalid.Title(winrt::box_value(Localized("appTitle", L"LiteLLM Menu")));
      invalid.Content(winrt::box_value(Localized("invalidText", L"The document is too large or contains invalid text.")));
      invalid.CloseButtonText(Localized("ok", L"OK"));
      invalid.XamlRoot(dialog.Content().as<controls::Grid>().XamlRoot());
      invalid.ShowAsync();
      return;
    }
    result->staged = std::move(encoded);
    result->accepted = true;
    dialog.Close();
  });
  dialog.Closed([result](auto const&, auto const&) { result->finished = true; });
  dialog.Content(root);

  HWND dialog_handle = nullptr;
  winrt::check_hresult(dialog.as<::IWindowNative>()->get_WindowHandle(&dialog_handle));
  if (window_handle_ != nullptr) {
    SetWindowLongPtrW(dialog_handle, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(window_handle_));
    EnableWindow(window_handle_, FALSE);
  }
  auto window_id = winrt::Microsoft::UI::GetWindowIdFromWindow(dialog_handle);
  winrt::Microsoft::UI::Windowing::AppWindow::GetFromWindowId(window_id).Resize({840, 620});
  dialog.Activate();
  editor.Focus(xaml::FocusState::Programmatic);

  MSG message{};
  BOOL message_result = TRUE;
  while (!result->finished && (message_result = GetMessageW(&message, nullptr, 0, 0)) > 0) {
    if (message.message == WM_KEYDOWN && GetAncestor(message.hwnd, GA_ROOT) == dialog_handle) {
      if (GetKeyState(VK_CONTROL) < 0 && (message.wParam == 'F' || message.wParam == 'f')) {
        find.Focus(xaml::FocusState::Programmatic);
        continue;
      }
      if (message.wParam == VK_F3) {
        SelectNext(editor, find);
        continue;
      }
    }
    TranslateMessage(&message);
    DispatchMessageW(&message);
  }
  if (window_handle_ != nullptr) {
    EnableWindow(window_handle_, TRUE);
    SetForegroundWindow(window_handle_);
  }
  if (message_result == 0) PostQuitMessage(static_cast<int>(message.wParam));
  return result->accepted ? std::optional<std::string>(std::move(result->staged)) : std::nullopt;
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

winrt::Microsoft::UI::Xaml::Controls::SplitView WinUI3NativeLeaf::CreateSplitView() const {
  winrt::Microsoft::UI::Xaml::Controls::SplitView view;
  view.DisplayMode(winrt::Microsoft::UI::Xaml::Controls::SplitViewDisplayMode::Inline);
  view.IsPaneOpen(true);
  return view;
}

winrt::Microsoft::UI::Xaml::Controls::TextBox WinUI3NativeLeaf::CreateTextEditor() const {
  winrt::Microsoft::UI::Xaml::Controls::TextBox editor;
  editor.AcceptsReturn(true);
  editor.AcceptsTab(true);
  editor.TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
  editor.FontFamily(winrt::Microsoft::UI::Xaml::Media::FontFamily(L"Consolas"));
  editor.HorizontalScrollBarVisibility(winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
  editor.VerticalScrollBarVisibility(winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
  return editor;
}

winrt::Microsoft::UI::Xaml::Controls::ComboBox WinUI3NativeLeaf::CreateSelector() const {
  return winrt::Microsoft::UI::Xaml::Controls::ComboBox();
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
  HMENU language_menu = nullptr;
  auto language_root = std::find_if(actions_.begin(), actions_.end(), [](auto const& action) {
    return action.id == L"language-menu";
  });
  if (language_root != actions_.end()) {
    language_menu = CreatePopupMenu();
  }
  for (size_t index = 0; index < actions_.size(); ++index) {
    auto const& action = actions_[index];
    if (action.id == L"language-menu") {
      continue;
    }
    const bool is_language_choice = action.id == L"set-language-system" ||
        action.id == L"set-language-en" || action.id == L"set-language-zh-Hans";
    UINT flags = MF_STRING | (action.enabled ? MF_ENABLED : MF_GRAYED);
    if (action.checked) flags |= MF_CHECKED;
    if (is_language_choice && language_menu != nullptr) {
      AppendMenuW(language_menu, flags,
          kTrayMenuFirstCommand + static_cast<UINT>(index), action.title.c_str());
      continue;
    }
    AppendMenuW(menu, flags,
        kTrayMenuFirstCommand + static_cast<UINT>(index), action.title.c_str());
  }
  if (language_menu != nullptr && language_root != actions_.end() && GetMenuItemCount(language_menu) > 0) {
    UINT flags = MF_POPUP | (language_root->enabled ? MF_ENABLED : MF_GRAYED);
    AppendMenuW(menu, flags, reinterpret_cast<UINT_PTR>(language_menu), language_root->title.c_str());
  } else if (language_menu != nullptr) {
    DestroyMenu(language_menu);
    language_menu = nullptr;
  }
  POINT point{};
  GetCursorPos(&point);
  SetForegroundWindow(window_handle_);
  TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, point.x, point.y, 0, window_handle_, nullptr);
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
  if (route == L"codex-settings") return Localized("routeCodexSettings", L"Codex Settings");
  if (route == L"claude-settings") return Localized("routeClaudeSettings", L"Claude Settings");
  if (route == L"runtime-settings") return Localized("routeRuntimeSettings", L"Runtime Settings");
  if (route == L"configuration-package") return Localized("routeConfigurationPackage", L"Configuration Package");
  if (route == L"webdav-settings") return Localized("routeWebdavSettings", L"WebDAV Settings");
  if (route == L"logs") return Localized("routeLogs", L"Logs");
  return Localized("appTitle", L"LiteLLM Menu");
}

void WinUI3NativeLeaf::RemoveTray() {
  if (!tray_visible_) return;
  Shell_NotifyIconW(NIM_DELETE, &tray_);
  tray_visible_ = false;
}

}
