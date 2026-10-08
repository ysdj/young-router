#pragma once

#include <windows.h>
#include <functional>
#include <optional>
#include <string>

namespace YoungRouter {

struct WindowsRelayLoginOptions {
  std::string account_id;
  std::string account_type;
  std::string label;
  std::string origin;
  std::optional<std::string> username;
  std::string language = "system";
  // Pending login: Core creates the account shell only after sign-in
  // succeeds, so a cancelled login leaves no reserved slot behind.
  bool pending_account = false;
  std::optional<std::string> station_id;
  std::optional<std::string> station_name;
  std::optional<std::string> station_type;
  std::optional<std::string> station_origin;
  // The host's own decision surface: the sign-in surface asks its post-login
  // question (keep the typed password?) through the one decision window the app
  // uses for every question, instead of a ContentDialog of its own.  Empty when
  // the host has none, and then the question answers "session only".
  std::function<bool(std::wstring const& title, std::wstring const& message,
                     std::wstring const& primary_label, std::wstring const& secondary_label)> decide;
};

struct WindowsRelayLoginResult {
  double revision = 0;
  std::string username;
};

struct WindowsRelaySessionRestoreResult {
  double revision = 0;
  std::string login_status;
  std::string username;
};

std::optional<WindowsRelayLoginResult> RunWindowsRelayLogin(
    HWND owner,
    WindowsRelayLoginOptions const& options);

// The station families this app can sign in to.  An empty value (or "auto")
// means the caller did not say: the flow then settles the family from whichever
// probe the page's own session answers, so the sign-in window can open at once
// instead of waiting on a pair of network probes that ask the same questions
// before it is allowed to appear.
bool IsRelayAccountType(std::string const& value);

std::optional<WindowsRelaySessionRestoreResult> RestoreWindowsRelaySession(
    WindowsRelayLoginOptions const& options);

bool ClearWindowsRelayCredentials(std::string const& account_id);
bool ClearWindowsRelayPassword(std::string const& account_id);

}  // namespace YoungRouter
