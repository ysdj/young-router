#pragma once

#include <windows.h>
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

std::optional<WindowsRelaySessionRestoreResult> RestoreWindowsRelaySession(
    WindowsRelayLoginOptions const& options);

bool ClearWindowsRelayCredentials(std::string const& account_id);
bool ClearWindowsRelayPassword(std::string const& account_id);

}  // namespace YoungRouter
