#pragma once

#include <shellapi.h>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include <winrt/Microsoft.UI.Xaml.h>
#include <winrt/Microsoft.UI.Xaml.Controls.h>
#include <winrt/Microsoft.UI.Xaml.Media.h>

namespace LiteLLMMenu {

struct NativeMenuAction {
  std::wstring id;
  std::wstring title;
  bool enabled = true;
  bool checked = false;
};

struct NativeSecretEditResult {
  bool clear = false;
  std::wstring value;
};

struct NativeMenuAnchor {
  double x = 0;
  double y = 0;
  double width = 0;
  double height = 0;
};

// Convert a content size expressed in 96-DPI DIPs to the physical outer-frame
// size required by AppWindow::Resize for a particular native window.
POINT FrameTrackSizeForContentDips(HWND window, LONG width, LONG height);

class WinUI3NativeLeaf : public std::enable_shared_from_this<WinUI3NativeLeaf> {
 public:
  static std::shared_ptr<WinUI3NativeLeaf> Shared();
  ~WinUI3NativeLeaf();

  void Initialize(winrt::Microsoft::UI::Xaml::Window const& window);
  void Initialize(HWND window_handle);
  void SetStatus(std::wstring_view title, bool running);
  void SetActions(std::vector<NativeMenuAction> const& actions);
  void SetLocalization(std::map<std::string, std::wstring> strings);
  std::wstring Localized(std::string const& key, std::wstring_view fallback) const;
  void SetActionHandler(std::function<void(std::string const&)> handler);
  void DispatchAction(std::string const& action);
  bool HandleWindowMessage(UINT message, WPARAM wparam, LPARAM lparam);
  void OpenRoute(std::wstring_view route);
  void CloseRoute(std::wstring_view route);
  // WM_GETMINMAXINFO uses physical frame pixels.  Keep the route specifications
  // in 96-DPI content DIPs and convert them at the native window boundary.
  POINT MinimumTrackSizeForActiveRoute() const;
  bool SetWindowContentSize(double width, double height);
  bool Confirm(
      std::wstring_view title,
      std::wstring_view message,
      std::wstring_view confirm_label);
  void ShowReadOnlyText(
      std::wstring_view title,
      std::wstring_view text,
      std::wstring_view close_label);
  std::optional<size_t> ShowActionMenu(std::wstring_view title, std::vector<std::wstring> const& items, NativeMenuAnchor anchor);
  std::optional<std::vector<std::wstring>> ChooseModelsToAdd(
      std::vector<std::wstring> models,
      std::wstring provider_name,
      std::wstring key_name);
  std::optional<NativeSecretEditResult> EditSecret(
      std::wstring_view title,
      bool allow_clear,
      bool present);
  std::optional<std::string> EditNativeText(
      std::string const& content,
      std::string const& language,
      std::wstring const& title);
  bool SetLaunchAtLogin(bool enabled);
  void ShowVersion() const;
  void Quit();
  winrt::Microsoft::UI::Xaml::Controls::SplitView CreateSplitView() const;
  winrt::Microsoft::UI::Xaml::Controls::TextBox CreateTextEditor() const;
  winrt::Microsoft::UI::Xaml::Controls::ComboBox CreateSelector() const;

 private:
  void EnsureTray();
  void RemoveTray();
  void DispatchDefaultTrayAction();
  void DispatchTrayAction(size_t index);
  void ShowTrayMenu();
  void InstallWindowHook();
  std::wstring RouteTitle(std::wstring_view route) const;
  std::wstring VersionText() const;

  std::wstring status_title_;
  std::wstring active_route_;
  HWND window_handle_ = nullptr;
  NOTIFYICONDATAW tray_{};
  bool tray_visible_ = false;
  bool service_running_ = false;
  bool quit_in_progress_ = false;
  bool quitting_ = false;
  WNDPROC previous_window_proc_ = nullptr;
  std::vector<NativeMenuAction> actions_;
  std::map<std::string, std::wstring> strings_;
  mutable std::mutex action_mutex_;
  std::vector<std::string> pending_actions_;
  std::function<void(std::string const&)> action_handler_;
};

}
