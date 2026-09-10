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

namespace YoungRouter {

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

// Applies the Win32 half of the immediate-presentation policy to an owned
// window, including future route and modal windows.
void DisableWindowTransitions(HWND window) noexcept;

// One relay API key as the group manager sheet sees it.
struct GroupManagerKey {
  std::wstring id;
  std::wstring name;
  std::wstring group_id;
  std::wstring group_label;
  std::wstring multiplier;
};

// Localized labels for the native group manager sheet.
struct GroupManagerLabels {
  std::wstring list_label;
  std::wstring add_label;
  std::wstring remove_label;
  std::wstring name_label;
  std::wstring group_label;
  std::wstring enabled_label;
  std::wstring new_key_name;
  std::wstring draft_label;
  std::wstring deleted_label;
  std::wstring auto_grouping_label;
  std::wstring close_label;
  std::wstring apply_label;
  std::wstring hint;
};

// One staged edit of an existing key.
struct GroupManagerUpdate {
  std::wstring key_id;
  std::wstring name;
  std::wstring group_id;
  bool enabled = true;
};

// What the sheet returns: the auto-grouping switch plus the key edits the user
// staged in the master-detail editor.
struct GroupManagerResult {
  bool auto_grouping = false;
  std::vector<std::pair<std::wstring, std::wstring>> creates;  // name, group id
  std::vector<GroupManagerUpdate> updates;
  std::vector<std::wstring> deletes;                           // key ids
};

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
  bool SetWindowContentSize(std::wstring_view route, double width, double height);
  bool Confirm(
      std::wstring_view title,
      std::wstring_view message,
      std::wstring_view confirm_label);
  void ShowReadOnlyText(
      std::wstring_view title,
      std::wstring_view text,
      std::wstring_view close_label,
      std::wstring_view language,
      std::wstring_view html);
  std::optional<size_t> ShowActionMenu(std::wstring_view title, std::vector<std::wstring> const& items, NativeMenuAnchor anchor);
  std::optional<std::vector<std::wstring>> ChooseModelsToAdd(
      std::vector<std::wstring> models,
      std::wstring provider_name,
      std::wstring key_name);
  std::optional<NativeSecretEditResult> EditSecret(
      std::wstring_view title,
      bool allow_clear,
      bool present);
  std::optional<GroupManagerResult> ShowGroupManager(
      std::wstring title,
      std::wstring account_label,
      std::vector<std::pair<std::wstring, std::wstring>> groups,
      std::vector<GroupManagerKey> keys,
      GroupManagerLabels labels,
      bool auto_grouping);
  bool SetLaunchAtLogin(bool enabled);
  void ShowVersion() const;
  struct VersionInfoResult {
    std::wstring app;
    std::wstring litellm;
  };
  VersionInfoResult VersionInfo() const;
  void OpenExternalURL(std::wstring_view url);
  void Quit();

 private:
  void EnsureTray();
  void RemoveTray();
  void DispatchDefaultTrayAction();
  void DispatchTrayAction(size_t index);
  void ShowTrayMenu();
  void InstallWindowHook();
  std::wstring RouteTitle(std::wstring_view route) const;
  std::wstring VersionText() const;

  std::wstring status_title_ = L"Status: Starting";
  bool status_title_is_bootstrap_ = true;
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
