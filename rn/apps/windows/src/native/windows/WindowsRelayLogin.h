#pragma once

#include <windows.h>
#include <optional>
#include <string>

namespace LiteLLMMenu {

struct WindowsRelayLoginOptions {
  std::string account_id;
  std::string account_type;
  std::string label;
  std::string origin;
  std::optional<std::string> username;
  bool remember_password = false;
  std::string language = "system";
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

}  // namespace LiteLLMMenu
