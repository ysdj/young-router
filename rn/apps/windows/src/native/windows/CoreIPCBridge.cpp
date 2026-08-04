#include "pch.h"
#include "CoreIPCBridge.h"

#include <Windows.h>
#include <winhttp.h>
#include <algorithm>
#include <cmath>
#include <cwctype>
#include <filesystem>
#include <fstream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <vector>
#include <winrt/Windows.Data.Json.h>

namespace LiteLLMMenu {
namespace {

constexpr size_t kMaxIpcMessageBytes = 4 * 1024 * 1024;

std::wstring Utf8ToWide(std::string const& value) {
  if (value.empty()) return {};
  int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
  if (size <= 0) throw std::runtime_error("invalid text");
  std::wstring result(static_cast<size_t>(size), L'\0');
  MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size);
  return result;
}

std::string WideToUtf8(std::wstring const& value) {
  if (value.empty()) return {};
  int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  if (size <= 0) throw std::runtime_error("invalid text");
  std::string result(static_cast<size_t>(size), '\0');
  WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
  return result;
}

std::wstring Quote(std::wstring const& value) {
  std::wstring escaped = value;
  size_t offset = 0;
  while ((offset = escaped.find(L'"', offset)) != std::wstring::npos) {
    escaped.insert(offset, 1, L'\\');
    offset += 2;
  }
  return L"\"" + escaped + L"\"";
}

std::wstring Environment(std::wstring const& name) {
  DWORD size = GetEnvironmentVariableW(name.c_str(), nullptr, 0);
  if (size == 0) return {};
  std::wstring value(size, L'\0');
  GetEnvironmentVariableW(name.c_str(), value.data(), size);
  value.resize(wcslen(value.c_str()));
  return value;
}

std::wstring PrivateRuntimeDirectory() {
  wchar_t root[MAX_PATH]{};
  if (GetTempPathW(MAX_PATH, root) == 0) throw std::runtime_error("core unavailable");
  wchar_t temporary[MAX_PATH]{};
  if (GetTempFileNameW(root, L"llm", 0, temporary) == 0) throw std::runtime_error("core unavailable");
  DeleteFileW(temporary);
  if (!CreateDirectoryW(temporary, nullptr)) throw std::runtime_error("core unavailable");
  return temporary;
}

std::string ReadSmallFile(std::wstring const& path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) return {};
  std::ostringstream output;
  output << stream.rdbuf();
  std::string value = output.str();
  return value.size() <= 65536 ? value : std::string{};
}

void RemoveRuntimeDirectory(std::wstring const& directory) {
  if (directory.empty()) return;
  DeleteFileW((directory + L"\\endpoint.json").c_str());
  RemoveDirectoryW(directory.c_str());
}

void StopCoreProcess(HANDLE process, std::wstring const& directory) {
  if (process) {
    DWORD const grace_result = WaitForSingleObject(process, 8000);
    if (grace_result != WAIT_OBJECT_0) {
      TerminateProcess(process, 0);
      WaitForSingleObject(process, INFINITE);
    }
    CloseHandle(process);
  }
  RemoveRuntimeDirectory(directory);
}

std::wstring ExecutableDirectory() {
  std::vector<wchar_t> path(32768, L'\0');
  DWORD length = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
  if (length == 0 || length >= path.size()) throw std::runtime_error("core unavailable");
  std::wstring value(path.data(), length);
  auto separator = value.find_last_of(L"\\/");
  if (separator == std::wstring::npos) throw std::runtime_error("core unavailable");
  value.resize(separator);
  return value;
}

bool IsExecutableFile(std::wstring const& path) {
  DWORD attributes = GetFileAttributesW(path.c_str());
  return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

bool IsEnvironmentName(std::wstring_view entry, std::wstring_view name) {
  if (entry.size() <= name.size() || entry[name.size()] != L'=') return false;
  for (size_t index = 0; index < name.size(); ++index) {
    if (std::towupper(entry[index]) != std::towupper(name[index])) return false;
  }
  return true;
}

std::vector<wchar_t> ChildEnvironment(std::wstring const& core_directory) {
  LPWCH source = GetEnvironmentStringsW();
  if (!source) throw std::runtime_error("core unavailable");
  std::vector<std::wstring> entries;
  for (LPCWCH current = source; *current != L'\0'; current += wcslen(current) + 1) {
    std::wstring entry(current);
    if (!IsEnvironmentName(entry, L"PYTHONPATH") && !IsEnvironmentName(entry, L"LITELLM_BIN")) {
      entries.push_back(std::move(entry));
    }
  }
  FreeEnvironmentStringsW(source);
  entries.push_back(L"PYTHONPATH=" + core_directory);
  entries.push_back(L"LITELLM_BIN=" + core_directory + L"\\runtime\\bin\\litellm.cmd");
  std::sort(entries.begin(), entries.end(), [](std::wstring const& left, std::wstring const& right) {
    return _wcsicmp(left.c_str(), right.c_str()) < 0;
  });
  std::vector<wchar_t> result;
  for (auto const& entry : entries) {
    result.insert(result.end(), entry.begin(), entry.end());
    result.push_back(L'\0');
  }
  result.push_back(L'\0');
  return result;
}

bool HasRequiredCoreFiles(std::wstring const& core_directory) {
  namespace fs = std::filesystem;
  std::error_code error;
  std::vector<fs::path> required{
      fs::path(core_directory) / L"litellm_menu" / L"core" / L"__main__.py",
      fs::path(core_directory) / L"config_editor_core" / L"api.py",
      fs::path(core_directory) / L"webdav" / L"core.py",
      fs::path(core_directory) / L"codex_config.py",
      fs::path(core_directory) / L"runtime_settings_io.py",
      fs::path(core_directory) / L"sitecustomize.py",
      fs::path(core_directory) / L"runtime" / L"bin" / L"litellm.cmd",
  };
  for (auto const& path : required) {
    error.clear();
    if (!fs::is_regular_file(path, error) || error) return false;
  }
  return true;
}

std::string RequestMethod(std::string const& request_json) {
  try {
    auto request = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(request_json));
    return WideToUtf8(request.GetNamedString(L"method", L"").c_str());
  } catch (...) {
    return {};
  }
}

struct WinHttpHandleCloser {
  void operator()(HINTERNET handle) const noexcept {
    if (handle) WinHttpCloseHandle(handle);
  }
};

using WinHttpHandle = std::unique_ptr<std::remove_pointer_t<HINTERNET>, WinHttpHandleCloser>;

}  // namespace

CoreIPCBridge& CoreIPCBridge::Shared() {
  static CoreIPCBridge instance;
  return instance;
}

CoreIPCBridge::~CoreIPCBridge() {
  Stop();
}

void CoreIPCBridge::SetEventHandler(std::function<void(std::string const&)> handler) {
  std::lock_guard guard(mutex_);
  event_handler_ = std::move(handler);
}

std::string CoreIPCBridge::Send(std::string const& request_json) {
  if (stopping_) throw std::runtime_error("core unavailable");
  const bool is_subscription = RequestMethod(request_json) == "subscribe";
  for (int attempt = 0; attempt < 2; ++attempt) {
    auto [endpoint, session, generation] = EnsureSession();
    try {
      auto result = Request(endpoint, L"", L"POST", request_json, session);
      if (IsSessionFailure(result.status)) {
        InvalidateCoreIfGeneration(generation, true);
        continue;
      }
      // Core operations deliberately use typed 4xx response envelopes. Leave
      // those intact so the shared protocol client can surface the safe error.
      if ((result.status != 200 && (result.status < 400 || result.status >= 500)) || result.body.empty()) {
        throw std::runtime_error("core unavailable");
      }
      if (is_subscription) {
        std::lock_guard guard(mutex_);
        subscription_request_ = request_json;
      }
      if (RequestMethod(request_json) == "editor") {
        RememberEditorCapability(request_json, result.body);
      }
      StartPollingIfSubscription(result.body, generation);
      bool recover_subscription = false;
      {
        std::lock_guard guard(mutex_);
        recover_subscription = !is_subscription && !subscription_request_.empty() && subscription_id_.empty();
      }
      if (recover_subscription) RecoverSubscription();
      return result.body;
    } catch (...) {
      InvalidateCoreIfGeneration(generation, true);
      if (attempt != 0) throw;
    }
  }
  throw std::runtime_error("core unavailable");
}

std::optional<std::string> CoreIPCBridge::RegisterFileCapability(std::wstring const& path, std::string const& purpose) {
  if (purpose != "import" && purpose != "export" && purpose != "claude-profile") return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"purpose", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(purpose)));
    payload.SetNamedValue(L"path", winrt::Windows::Data::Json::JsonValue::CreateStringValue(path));
    auto [endpoint, session, generation] = EnsureSession();
    auto result = Request(endpoint, L"host/file-capability", L"POST", WideToUtf8(payload.Stringify().c_str()), session);
    if (IsSessionFailure(result.status)) {
      InvalidateCoreIfGeneration(generation, true);
      return std::nullopt;
    }
    if (result.status != 200 || result.body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result.body));
    if (response.GetNamedNumber(L"protocol_version", 0) != 1) return std::nullopt;
    std::wstring token = response.GetNamedString(L"token", L"").c_str();
    return token.empty() ? std::nullopt : std::optional<std::string>(WideToUtf8(token));
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> CoreIPCBridge::ReadEditorDocument(std::string const& editor_token) {
  if (editor_token.empty() || editor_token.size() > 256) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"editor_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(editor_token)));
    auto result = HostRequest(L"host/editor/read", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 2 || !response.HasKey(L"protocol_version") || !response.HasKey(L"text") ||
        response.GetNamedNumber(L"protocol_version", 0) != 1) return std::nullopt;
    std::wstring text = response.GetNamedString(L"text", L"").c_str();
    std::string utf8_text = WideToUtf8(text);
    if (utf8_text.size() > 2 * 1024 * 1024) return std::nullopt;
    return utf8_text;
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<double> CoreIPCBridge::StageEditorDocument(
    std::string const& editor_token,
    std::string const& text) {
  auto result = StageEditorDocumentWithReplacement(editor_token, text);
  return result ? std::optional<double>(result->revision) : std::nullopt;
}

std::optional<CoreIPCBridge::EditorStageResult> CoreIPCBridge::StageEditorDocumentWithReplacement(
    std::string const& editor_token,
    std::string const& text) {
  if (editor_token.empty() || editor_token.size() > 256 || text.size() > 2 * 1024 * 1024) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"editor_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(editor_token)));
    payload.SetNamedValue(L"text", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(text)));
    auto result = HostRequest(L"host/editor/stage", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 3 || !response.HasKey(L"protocol_version") || !response.HasKey(L"revision") ||
        !response.HasKey(L"editor_token") ||
        response.GetNamedNumber(L"protocol_version", 0) != 1) return std::nullopt;
    double revision = response.GetNamedNumber(L"revision", -1);
    if (revision < 0 || revision != floor(revision)) return std::nullopt;
    std::string replacement_token = WideToUtf8(response.GetNamedString(L"editor_token", L"").c_str());
    if (replacement_token.empty() || replacement_token.size() > 256) return std::nullopt;
    RotateEditorCapability(editor_token, replacement_token);
    RecoverSubscription();
    return EditorStageResult{revision, std::move(replacement_token)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::RefreshedEditorDocument> CoreIPCBridge::RefreshEditorDocument(
    std::string const& editor_token) {
  if (editor_token.empty() || editor_token.size() > 256) return std::nullopt;
  auto identity = EditorIdentityFor(editor_token);
  if (!identity) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject params;
    params.SetNamedValue(L"domain", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(identity->domain)));
    params.SetNamedValue(L"document", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(identity->document)));
    winrt::Windows::Data::Json::JsonObject request;
    request.SetNamedValue(L"protocol_version", winrt::Windows::Data::Json::JsonValue::CreateNumberValue(1));
    static std::atomic<unsigned long long> refresh_request_serial{0};
    std::wstring request_id = L"native-editor-refresh-" + std::to_wstring(GetCurrentProcessId()) +
        L"-" + std::to_wstring(GetTickCount64()) + L"-" +
        std::to_wstring(refresh_request_serial.fetch_add(1, std::memory_order_relaxed));
    request.SetNamedValue(
        L"request_id",
        winrt::Windows::Data::Json::JsonValue::CreateStringValue(request_id));
    request.SetNamedValue(L"method", winrt::Windows::Data::Json::JsonValue::CreateStringValue(L"editor"));
    request.SetNamedValue(L"params", params);
    auto response_json = Send(WideToUtf8(request.Stringify().c_str()));
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(response_json));
    if (response.Size() != 4 || !response.HasKey(L"protocol_version") ||
        !response.HasKey(L"request_id") || !response.HasKey(L"ok") ||
        !response.HasKey(L"result") || response.GetNamedNumber(L"protocol_version", 0) != 1 ||
        response.GetNamedString(L"request_id", L"") != request_id ||
        !response.GetNamedBoolean(L"ok", false)) {
      return std::nullopt;
    }
    auto result = response.GetNamedObject(L"result", nullptr);
    if (!result || result.Size() != 4 || !result.HasKey(L"domain") ||
        !result.HasKey(L"document") || !result.HasKey(L"editor_token") ||
        !result.HasKey(L"revision") ||
        result.GetNamedString(L"domain", L"") != Utf8ToWide(identity->domain) ||
        result.GetNamedString(L"document", L"") != Utf8ToWide(identity->document)) {
      return std::nullopt;
    }
    double revision = result.GetNamedNumber(L"revision", -1);
    if (revision < 0 || revision != std::floor(revision)) return std::nullopt;
    std::string replacement_token = WideToUtf8(result.GetNamedString(L"editor_token", L"").c_str());
    if (replacement_token.empty() || replacement_token.size() > 256) return std::nullopt;
    auto text = ReadEditorDocument(replacement_token);
    if (!text) return std::nullopt;
    ReplaceEditorCapability(editor_token, replacement_token, *identity);
    return RefreshedEditorDocument{std::move(replacement_token), std::move(*text)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::SecretCapability> CoreIPCBridge::CreateSecretCapability(
    std::string const& domain,
    std::string const& field,
    std::optional<std::string> const& target,
    std::string const& purpose) {
  if (domain.empty() || domain.size() > 64 || field.empty() || field.size() > 64 ||
      (target && target->size() > 256) || purpose != "settings") return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"domain", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(domain)));
    payload.SetNamedValue(L"field", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(field)));
    payload.SetNamedValue(L"purpose", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(purpose)));
    if (target && !target->empty()) {
      payload.SetNamedValue(L"target", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*target)));
    }
    auto result = HostRequest(L"host/secret/capability", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 4 || response.GetNamedNumber(L"protocol_version", 0) != 1 ||
        !response.HasKey(L"secret_token") || !response.HasKey(L"revision") || !response.HasKey(L"present")) {
      return std::nullopt;
    }
    std::string token = WideToUtf8(response.GetNamedString(L"secret_token", L"").c_str());
    if (token.empty() || token.size() > 256) return std::nullopt;
    return SecretCapability{std::move(token), response.GetNamedBoolean(L"present", false)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::SecretStageResult> CoreIPCBridge::StageSecret(
    std::string const& secret_token,
    std::optional<std::string> const& value,
    bool clear) {
  if (secret_token.empty() || secret_token.size() > 256 || (!clear && !value) ||
      (value && value->size() > 16 * 1024)) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"secret_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(secret_token)));
    if (clear) {
      payload.SetNamedValue(L"clear", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(true));
    } else {
      payload.SetNamedValue(L"value", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(value.value_or(""))));
    }
    auto result = HostRequest(L"host/secret/stage", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 3 || response.GetNamedNumber(L"protocol_version", 0) != 1 ||
        !response.HasKey(L"revision") || !response.HasKey(L"present")) return std::nullopt;
    double revision = response.GetNamedNumber(L"revision", -1);
    if (revision < 0 || revision != floor(revision)) return std::nullopt;
    RecoverSubscription();
    return SecretStageResult{revision, response.GetNamedBoolean(L"present", false)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::SecretReadCapability> CoreIPCBridge::CreateSecretReadCapability(
    std::string const& target) {
  if (target.empty() || target.size() > 256) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"domain", winrt::Windows::Data::Json::JsonValue::CreateStringValue(L"providers_models"));
    payload.SetNamedValue(L"field", winrt::Windows::Data::Json::JsonValue::CreateStringValue(L"api_key"));
    payload.SetNamedValue(L"target", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(target)));
    auto result = HostRequest(L"host/secret/read-capability", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 4 || response.GetNamedNumber(L"protocol_version", 0) != 1 ||
        !response.HasKey(L"secret_read_token") || !response.HasKey(L"revision") || !response.HasKey(L"present")) {
      return std::nullopt;
    }
    double revision = response.GetNamedNumber(L"revision", -1);
    std::string token = WideToUtf8(response.GetNamedString(L"secret_read_token", L"").c_str());
    if (revision < 0 || revision != std::floor(revision) || token.empty() || token.size() > 256) {
      return std::nullopt;
    }
    return SecretReadCapability{std::move(token)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> CoreIPCBridge::ReadSecret(std::string const& secret_read_token) {
  if (secret_read_token.empty() || secret_read_token.size() > 256) return std::nullopt;
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(
        L"secret_read_token",
        winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(secret_read_token)));
    auto result = HostRequest(L"host/secret/read", WideToUtf8(payload.Stringify().c_str()), false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 2 || response.GetNamedNumber(L"protocol_version", 0) != 1 || !response.HasKey(L"value")) {
      return std::nullopt;
    }
    std::string value = WideToUtf8(response.GetNamedString(L"value", L"").c_str());
    if (value.size() > 16 * 1024 || value.find('\0') != std::string::npos ||
        value.find('\r') != std::string::npos || value.find('\n') != std::string::npos) {
      return std::nullopt;
    }
    return value;
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> CoreIPCBridge::ReadProviderAPIKey(std::string const& target) {
  auto capability = CreateSecretReadCapability(target);
  if (!capability) return std::nullopt;
  return ReadSecret(capability->token);
}

std::optional<CoreIPCBridge::RelayLoginResult> CoreIPCBridge::AcceptRelayLogin(
    std::string const& account_id,
    std::string const& account_type,
    std::string const& label,
    std::string const& origin,
    std::string const& username,
    std::optional<std::string> const& cookie,
    std::optional<std::string> const& access_token,
    std::optional<std::string> const& refresh_token) {
  if (account_id.empty() || account_id.size() > 96 ||
      (account_type != "newapi" && account_type != "sub2api") ||
      label.empty() || label.size() > 160 || origin.empty() || origin.size() > 2048 ||
      username.empty() || username.size() > 320 ||
      (cookie && cookie->size() > 32768) ||
      (access_token && access_token->size() > 32768) ||
      (refresh_token && refresh_token->size() > 32768) ||
      ((!cookie || cookie->empty()) && (!access_token || access_token->empty()))) {
    return std::nullopt;
  }
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"account_id", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(account_id)));
    payload.SetNamedValue(L"type", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(account_type)));
    payload.SetNamedValue(L"label", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(label)));
    payload.SetNamedValue(L"origin", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(origin)));
    payload.SetNamedValue(L"username", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(username)));
    if (cookie && !cookie->empty()) {
      payload.SetNamedValue(L"cookie", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*cookie)));
    }
    if (access_token && !access_token->empty()) {
      payload.SetNamedValue(L"access_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*access_token)));
    }
    if (refresh_token && !refresh_token->empty()) {
      payload.SetNamedValue(L"refresh_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*refresh_token)));
    }
    auto body = WideToUtf8(payload.Stringify().c_str());
    if (body.size() > 96 * 1024) return std::nullopt;
    auto result = HostRequest(L"host/relay/login", body, false, 60000);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 4 || !response.HasKey(L"protocol_version") ||
        !response.HasKey(L"revision") || !response.HasKey(L"login_status") ||
        !response.HasKey(L"username") ||
        response.GetNamedNumber(L"protocol_version", 0) != 1 ||
        response.GetNamedString(L"login_status", L"") != L"signed_in") {
      return std::nullopt;
    }
    double revision = response.GetNamedNumber(L"revision", -1);
    auto accepted_username = WideToUtf8(response.GetNamedString(L"username", L"").c_str());
    if (revision < 0 || revision != floor(revision) || accepted_username.empty() || accepted_username.size() > 320) {
      return std::nullopt;
    }
    RecoverSubscription();
    return RelayLoginResult{revision, std::move(accepted_username)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::RelaySessionRestoreResult> CoreIPCBridge::RestoreRelaySession(
    std::string const& account_id,
    std::string const& account_type,
    std::string const& label,
    std::string const& origin,
    std::string const& login_status,
    std::optional<std::string> const& username,
    std::optional<std::string> const& cookie,
    std::optional<std::string> const& access_token,
    std::optional<std::string> const& refresh_token) {
  bool signed_in = login_status == "signed_in";
  bool terminal_status = login_status == "signed_out" || login_status == "expired";
  if (account_id.empty() || account_id.size() > 96 ||
      (account_type != "newapi" && account_type != "sub2api") ||
      label.empty() || label.size() > 160 || origin.empty() || origin.size() > 2048 ||
      !signed_in && !terminal_status ||
      (username && username->size() > 320) ||
      (cookie && cookie->size() > 32768) ||
      (access_token && access_token->size() > 32768) ||
      (refresh_token && refresh_token->size() > 32768) ||
      (signed_in && (!username || username->empty() || ((!cookie || cookie->empty()) && (!access_token || access_token->empty()))) ||
      (!signed_in && ((cookie && !cookie->empty()) || (access_token && !access_token->empty()) || (refresh_token && !refresh_token->empty())))) {
    return std::nullopt;
  }
  try {
    winrt::Windows::Data::Json::JsonObject payload;
    payload.SetNamedValue(L"account_id", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(account_id)));
    payload.SetNamedValue(L"type", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(account_type)));
    payload.SetNamedValue(L"label", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(label)));
    payload.SetNamedValue(L"origin", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(origin)));
    payload.SetNamedValue(L"login_status", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(login_status)));
    if (username && !username->empty()) {
      payload.SetNamedValue(L"username", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*username)));
    }
    if (cookie && !cookie->empty()) {
      payload.SetNamedValue(L"cookie", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*cookie)));
    }
    if (access_token && !access_token->empty()) {
      payload.SetNamedValue(L"access_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*access_token)));
    }
    if (refresh_token && !refresh_token->empty()) {
      payload.SetNamedValue(L"refresh_token", winrt::Windows::Data::Json::JsonValue::CreateStringValue(Utf8ToWide(*refresh_token)));
    }
    auto body = WideToUtf8(payload.Stringify().c_str());
    if (body.size() > 96 * 1024) return std::nullopt;
    auto result = HostRequest(L"host/relay/restore", body, false);
    if (!result || result->status != 200 || result->body.empty()) return std::nullopt;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result->body));
    if (response.Size() != 4 || !response.HasKey(L"protocol_version") ||
        !response.HasKey(L"revision") || !response.HasKey(L"login_status") ||
        !response.HasKey(L"username") || response.GetNamedNumber(L"protocol_version", 0) != 1) {
      return std::nullopt;
    }
    double revision = response.GetNamedNumber(L"revision", -1);
    auto restored_status = WideToUtf8(response.GetNamedString(L"login_status", L"").c_str());
    auto restored_username = WideToUtf8(response.GetNamedString(L"username", L"").c_str());
    if (revision < 0 || revision != floor(revision) ||
        (restored_status != "signed_in" && restored_status != "signed_out" && restored_status != "expired") ||
        restored_username.size() > 320) {
      return std::nullopt;
    }
    RecoverSubscription();
    return RelaySessionRestoreResult{revision, std::move(restored_status), std::move(restored_username)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CoreIPCBridge::HttpResult> CoreIPCBridge::HostRequest(
    std::wstring const& route,
    std::string const& body,
    bool retry_session,
    int receive_timeout_ms) {
  const int attempts = retry_session ? 2 : 1;
  for (int attempt = 0; attempt < attempts; ++attempt) {
    unsigned long generation = 0;
    try {
      auto active = EnsureSession();
      generation = active.generation;
      auto const& endpoint = active.endpoint;
      auto const& session = active.token;
      auto result = Request(endpoint, route, L"POST", body, session, receive_timeout_ms);
      if (IsSessionFailure(result.status)) {
        InvalidateCoreIfGeneration(generation, true);
        continue;
      }
      if (result.status != 200) return std::nullopt;
      return result;
    } catch (...) {
      if (generation != 0) InvalidateCoreIfGeneration(generation, true);
    }
  }
  return std::nullopt;
}

CoreIPCBridge::Session CoreIPCBridge::EnsureSession() {
  if (stopping_) throw std::runtime_error("core unavailable");
  auto hello_expiry = [](CoreIPCBridge::HttpResult const& hello) -> double {
    if (hello.status != 200 || hello.session.empty()) throw std::runtime_error("core unavailable");
    auto envelope = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(hello.body));
    auto session = envelope.GetNamedObject(L"session", nullptr);
    auto expires_in = session ? session.GetNamedNumber(L"expires_in", 0) : 0;
    if (!envelope.GetNamedBoolean(L"ok", false) || expires_in <= 0 || expires_in > 86400) {
      throw std::runtime_error("core unavailable");
    }
    return expires_in;
  };
  std::vector<void*> retired_processes;
  std::vector<std::wstring> retired_directories;
  std::vector<void*> failed_processes;
  std::vector<std::wstring> failed_directories;
  bool hello_registered = false;
  std::unique_lock lock(mutex_);
  while (establishing_session_) {
    session_condition_.wait(lock);
    if (endpoint_ && !session_token_.empty() &&
        static_cast<LONG>(session_expires_at_tick_ - GetTickCount()) > 15000) {
      return {*endpoint_, session_token_, core_generation_};
    }
    session_error_ = nullptr;
  }
  if (endpoint_ && !session_token_.empty() &&
      static_cast<LONG>(session_expires_at_tick_ - GetTickCount()) > 15000) {
    return {*endpoint_, session_token_, core_generation_};
  }
  if (endpoint_ && !session_token_.empty()) {
    Endpoint renewal_endpoint = *endpoint_;
    std::wstring renewal_token = session_token_;
    const unsigned long renewal_generation = core_generation_;
    establishing_session_ = true;
    session_error_ = nullptr;
    lock.unlock();
    try {
      auto hello = Request(renewal_endpoint, L"hello", L"POST", "", renewal_token);
      auto expires_in = hello_expiry(hello);
      lock.lock();
      if (core_generation_ != renewal_generation || !endpoint_) {
        establishing_session_ = false;
        session_error_ = std::make_exception_ptr(std::runtime_error("core unavailable"));
        session_condition_.notify_all();
        lock.unlock();
        throw std::runtime_error("core unavailable");
      }
      session_token_ = hello.session;
      session_expires_at_tick_ = GetTickCount() + static_cast<unsigned long>(expires_in * 1000);
      establishing_session_ = false;
      session_error_ = nullptr;
      session_condition_.notify_all();
      return {renewal_endpoint, session_token_, core_generation_};
    } catch (...) {
      if (!lock.owns_lock()) lock.lock();
      if (core_generation_ == renewal_generation) {
        TakeCoreLocked(retired_processes, retired_directories, true);
      }
      establishing_session_ = false;
      session_error_ = nullptr;
      session_condition_.notify_all();
      lock.unlock();
      for (size_t index = 0; index < retired_processes.size(); ++index) {
        StopCoreProcess(static_cast<HANDLE>(retired_processes[index]), retired_directories[index]);
      }
      throw;
    }
  }
  establishing_session_ = true;
  session_error_ = nullptr;
  TakeCoreLocked(retired_processes, retired_directories, true);
  const unsigned long attempt_generation = core_generation_;
  lock.unlock();
  for (size_t index = 0; index < retired_processes.size(); ++index) {
    StopCoreProcess(static_cast<HANDLE>(retired_processes[index]), retired_directories[index]);
  }
  lock.lock();

  Endpoint endpoint;
  try {
    endpoint = StartCoreLocked();
    hello_registered = true;
  } catch (...) {
    establishing_session_ = false;
    session_error_ = std::current_exception();
    session_condition_.notify_all();
    lock.unlock();
    throw;
  }
  lock.unlock();

  try {
    auto hello = Request(endpoint, L"hello", L"POST", "", endpoint.bootstrap_token);
    auto expires_in = hello_expiry(hello);
    lock.lock();
    if (core_generation_ != attempt_generation) {
      establishing_session_ = false;
      session_error_ = std::make_exception_ptr(std::runtime_error("core unavailable"));
      session_condition_.notify_all();
      lock.unlock();
      throw std::runtime_error("core unavailable");
    }
    endpoint_ = endpoint;
    session_token_ = hello.session;
    session_expires_at_tick_ = GetTickCount() + static_cast<unsigned long>(expires_in * 1000);
    establishing_session_ = false;
    session_error_ = nullptr;
    session_condition_.notify_all();
    return {endpoint, session_token_, core_generation_};
  } catch (...) {
    if (!lock.owns_lock()) lock.lock();
    if (hello_registered && core_generation_ == attempt_generation) {
      TakeCoreLocked(failed_processes, failed_directories, true);
    }
    establishing_session_ = false;
    session_error_ = std::current_exception();
    session_condition_.notify_all();
    lock.unlock();
    for (size_t index = 0; index < failed_processes.size(); ++index) {
      StopCoreProcess(static_cast<HANDLE>(failed_processes[index]), failed_directories[index]);
    }
    throw;
  }
}

CoreIPCBridge::Endpoint CoreIPCBridge::StartCoreLocked() {
  if (endpoint_) return *endpoint_;
  std::wstring directory = PrivateRuntimeDirectory();
  std::wstring descriptor = directory + L"\\endpoint.json";
  std::wstring core_directory = ExecutableDirectory() + L"\\Core";
  std::wstring python = Environment(L"LITELLM_MENU_CORE_PYTHON");
  if (python.empty()) python = core_directory + L"\\runtime\\bin\\python.exe";
  if (!IsExecutableFile(python) || !HasRequiredCoreFiles(core_directory)) {
    RemoveDirectoryW(directory.c_str());
    throw std::runtime_error("core unavailable");
  }
  std::wstring command = Quote(python) + L" -m litellm_menu.core --endpoint-file " + Quote(descriptor) +
      L" --parent-pid " + std::to_wstring(GetCurrentProcessId());
  std::vector<wchar_t> mutable_command(command.begin(), command.end());
  mutable_command.push_back(L'\0');

  STARTUPINFOW startup{};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process{};
  std::vector<wchar_t> environment = ChildEnvironment(core_directory);
  BOOL created = CreateProcessW(nullptr, mutable_command.data(), nullptr, nullptr, FALSE,
      CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
      environment.data(), core_directory.c_str(), &startup, &process);
  if (!created) {
    RemoveDirectoryW(directory.c_str());
    throw std::runtime_error("core unavailable");
  }
  CloseHandle(process.hThread);

  for (int attempt = 0; attempt < 100; ++attempt) {
    std::string text = ReadSmallFile(descriptor);
    if (!text.empty()) {
      try {
        auto raw = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(text));
        std::wstring kind = raw.GetNamedString(L"kind", L"").c_str();
        std::wstring address = raw.GetNamedString(L"address", L"").c_str();
        double port = raw.GetNamedNumber(L"port", 0);
        std::wstring bootstrap = raw.GetNamedString(L"bootstrap_token", L"").c_str();
        if (kind == L"loopback" && (address == L"127.0.0.1" || address == L"localhost") && port > 0 && port <= 65535 && !bootstrap.empty()) {
          if (process_handle_) {
            retired_process_handles_.push_back(process_handle_);
            retired_runtime_directories_.push_back(std::move(runtime_directory_));
          }
          process_handle_ = process.hProcess;
          runtime_directory_ = directory;
          return Endpoint{address, static_cast<unsigned short>(port), bootstrap};
        }
      } catch (...) {
      }
    }
    Sleep(50);
  }
  StopCoreProcess(process.hProcess, directory);
  throw std::runtime_error("core unavailable");
}

CoreIPCBridge::HttpResult CoreIPCBridge::Request(
    Endpoint const& endpoint,
    std::wstring const& route,
    std::wstring const& method,
    std::string const& body,
    std::wstring const& token,
    int receive_timeout_ms) {
  WinHttpHandle session{WinHttpOpen(L"LiteLLMMenuCore/1", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0)};
  if (!session) throw std::runtime_error("core unavailable");
  // Server long-poll is bounded at 20 seconds; 30 seconds keeps an ordinary
  // `event: null` heartbeat from invalidating the one-shot Core session.
  WinHttpSetTimeouts(session.get(), 5000, 5000, 5000, receive_timeout_ms);
  WinHttpHandle connection{WinHttpConnect(session.get(), endpoint.address.c_str(), endpoint.port, 0)};
  std::wstring target = L"/v1" + (route.empty() ? L"" : L"/" + route);
  WinHttpHandle request{connection ? WinHttpOpenRequest(connection.get(), method.c_str(), target.c_str(), nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0) : nullptr};
  if (!request) throw std::runtime_error("core unavailable");
  std::wstring headers = L"Authorization: Bearer " + token + L"\r\nContent-Type: application/json\r\n";
  BOOL sent = WinHttpSendRequest(request.get(), headers.c_str(), static_cast<DWORD>(-1L),
      body.empty() ? WINHTTP_NO_REQUEST_DATA : const_cast<char*>(body.data()), static_cast<DWORD>(body.size()), static_cast<DWORD>(body.size()), 0);
  if (!sent || !WinHttpReceiveResponse(request.get(), nullptr)) throw std::runtime_error("core unavailable");
  HttpResult result;
  DWORD status_size = sizeof(result.status);
  if (!WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &result.status, &status_size, WINHTTP_NO_HEADER_INDEX)) {
    throw std::runtime_error("core unavailable");
  }
  DWORD session_size = 0;
  WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_CUSTOM, L"X-LiteLLM-Core-Session", nullptr, &session_size, WINHTTP_NO_HEADER_INDEX);
  if (GetLastError() == ERROR_INSUFFICIENT_BUFFER && session_size > sizeof(wchar_t)) {
    std::wstring value(session_size / sizeof(wchar_t), L'\0');
    if (WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_CUSTOM, L"X-LiteLLM-Core-Session", value.data(), &session_size, WINHTTP_NO_HEADER_INDEX)) {
      value.resize(wcslen(value.c_str()));
      result.session = value;
    }
  }
  for (;;) {
    DWORD available = 0;
    if (!WinHttpQueryDataAvailable(request.get(), &available)) throw std::runtime_error("core unavailable");
    if (available == 0) break;
    if (result.body.size() + available > kMaxIpcMessageBytes) throw std::runtime_error("core unavailable");
    size_t offset = result.body.size();
    result.body.resize(offset + available);
    DWORD read = 0;
    if (!WinHttpReadData(request.get(), result.body.data() + offset, available, &read)) throw std::runtime_error("core unavailable");
    result.body.resize(offset + read);
  }
  return result;
}

void CoreIPCBridge::StartPollingIfSubscription(
    std::string const& response_json,
    unsigned long generation) {
  try {
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(response_json));
    if (!response.GetNamedBoolean(L"ok", false)) return;
    auto result = response.GetNamedObject(L"result", nullptr);
    if (!result) return;
    std::string subscription = WideToUtf8(result.GetNamedString(L"subscription_id", L"").c_str());
    if (subscription.empty()) return;
    {
      std::lock_guard guard(mutex_);
      if (core_generation_ != generation || !endpoint_ || session_token_.empty()) return;
      if (subscription_id_ == subscription) return;
      subscription_id_ = subscription;
    }
    JoinRetiredPollThread();
    std::lock_guard poll_guard(poll_mutex_);
    poll_thread_ = std::thread([this, subscription] { PollEvents(subscription); });
  } catch (...) {
  }
}

void CoreIPCBridge::RecoverSubscription() {
  std::string request;
  {
    std::lock_guard guard(mutex_);
    request = subscription_request_;
  }
  if (request.empty()) return;
  // Recovery is best effort and never changes the result of the user's
  // foreground operation. A later snapshot request will retry if Core exited.
  try {
    auto [endpoint, session, generation] = EnsureSession();
    auto result = Request(endpoint, L"", L"POST", request, session);
    if (IsSessionFailure(result.status)) {
      InvalidateCoreIfGeneration(generation, true);
    } else if (result.status == 200) {
      StartPollingIfSubscription(result.body, generation);
    }
  } catch (...) {
  }
}

void CoreIPCBridge::PollEvents(std::string subscription) {
  while (!stopping_) {
    Endpoint endpoint;
    std::wstring session;
    unsigned long generation = 0;
    {
      std::lock_guard guard(mutex_);
      if (!endpoint_ || session_token_.empty() || subscription_id_ != subscription) return;
      endpoint = *endpoint_;
      session = session_token_;
      generation = core_generation_;
    }
    try {
      std::wstring route = L"events?subscription_id=" + Utf8ToWide(subscription) + L"&timeout=20";
      auto result = Request(endpoint, route, L"GET", "", session);
      if (IsSessionFailure(result.status)) {
        InvalidateCoreIfGeneration(generation, true);
        return;
      }
      if (result.status != 200) continue;
      auto outer = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(result.body));
      // A missing/null event is the expected quiet heartbeat.
      auto event = outer.GetNamedObject(L"event", nullptr);
      std::function<void(std::string const&)> handler;
      {
        std::lock_guard guard(mutex_);
        if (core_generation_ != generation || subscription_id_ != subscription) return;
        handler = event_handler_;
      }
      if (event && handler) handler(WideToUtf8(event.Stringify().c_str()));
    } catch (...) {
      InvalidateCoreIfGeneration(generation, true);
      return;
    }
  }
}

void CoreIPCBridge::Stop() {
  bool expected = false;
  if (!stopping_.compare_exchange_strong(expected, true)) return;

  std::optional<Endpoint> shutdown_endpoint;
  std::wstring shutdown_token;
  {
    std::lock_guard guard(mutex_);
    shutdown_endpoint = endpoint_;
    shutdown_token = session_token_;
  }
  // Stop the managed LiteLLM service through the authenticated host route
  // before reaping Core. Direct process termination remains the final fallback.
  if (shutdown_endpoint && !shutdown_token.empty()) {
    try {
      Request(*shutdown_endpoint, L"host/shutdown", L"POST", "{}", shutdown_token, 4000);
    } catch (...) {
    }
  }
  InvalidateCore(false);
  JoinRetiredPollThread();
}

bool CoreIPCBridge::IsSessionFailure(unsigned long status) noexcept {
  return status == 401 || status == 403;
}

void CoreIPCBridge::JoinRetiredPollThread() {
  std::unique_lock poll_lock(poll_mutex_);
  if (!poll_thread_.joinable()) return;
  if (poll_thread_.get_id() == std::this_thread::get_id()) return;
  std::thread retired = std::move(poll_thread_);
  poll_lock.unlock();
  retired.join();
}

void CoreIPCBridge::InvalidateCore(bool preserve_subscription) {
  std::vector<void*> processes;
  std::vector<std::wstring> directories;
  {
    std::lock_guard guard(mutex_);
    TakeCoreLocked(processes, directories, preserve_subscription);
  }
  for (size_t index = 0; index < processes.size(); ++index) {
    StopCoreProcess(static_cast<HANDLE>(processes[index]), directories[index]);
  }
}

void CoreIPCBridge::InvalidateCoreIfGeneration(
    unsigned long generation,
    bool preserve_subscription) {
  std::vector<void*> processes;
  std::vector<std::wstring> directories;
  {
    std::lock_guard guard(mutex_);
    if (core_generation_ != generation) return;
    TakeCoreLocked(processes, directories, preserve_subscription);
  }
  for (size_t index = 0; index < processes.size(); ++index) {
    StopCoreProcess(static_cast<HANDLE>(processes[index]), directories[index]);
  }
}

void CoreIPCBridge::InvalidateCoreLocked(bool preserve_subscription) {
  endpoint_.reset();
  session_token_.clear();
  session_expires_at_tick_ = 0;
  subscription_id_.clear();
  if (!preserve_subscription) {
    subscription_request_.clear();
    event_handler_ = nullptr;
  }
  ++core_generation_;
}

void CoreIPCBridge::TakeCoreLocked(
    std::vector<void*>& processes,
    std::vector<std::wstring>& directories,
    bool preserve_subscription) {
  processes = std::move(retired_process_handles_);
  directories = std::move(retired_runtime_directories_);
  retired_process_handles_.clear();
  retired_runtime_directories_.clear();
  if (process_handle_) {
    processes.push_back(process_handle_);
    directories.push_back(std::move(runtime_directory_));
  }
  process_handle_ = nullptr;
  runtime_directory_.clear();
  InvalidateCoreLocked(preserve_subscription);
}

void CoreIPCBridge::RememberEditorCapability(
    std::string const& request_json,
    std::string const& response_json) {
  try {
    auto request = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(request_json));
    auto params = request.GetNamedObject(L"params", nullptr);
    if (!params) return;
    std::string domain = WideToUtf8(params.GetNamedString(L"domain", L"").c_str());
    std::string document = WideToUtf8(params.GetNamedString(L"document", L"").c_str());
    const bool valid_identity =
        (domain == "codex" && (document == "config" || document == "auth")) ||
        (domain == "claude" && document == "settings");
    if (!valid_identity) return;
    auto response = winrt::Windows::Data::Json::JsonObject::Parse(Utf8ToWide(response_json));
    if (!response.GetNamedBoolean(L"ok", false)) return;
    auto result = response.GetNamedObject(L"result", nullptr);
    if (!result ||
        result.GetNamedString(L"domain", L"") != Utf8ToWide(domain) ||
        result.GetNamedString(L"document", L"") != Utf8ToWide(document)) {
      return;
    }
    std::string token = WideToUtf8(result.GetNamedString(L"editor_token", L"").c_str());
    if (token.empty() || token.size() > 256) return;
    ReplaceEditorCapability(std::nullopt, token, EditorIdentity{std::move(domain), std::move(document)});
  } catch (...) {
  }
}

std::optional<CoreIPCBridge::EditorIdentity> CoreIPCBridge::EditorIdentityFor(
    std::string const& editor_token) {
  std::lock_guard guard(mutex_);
  auto found = editor_identities_.find(editor_token);
  if (found == editor_identities_.end()) return std::nullopt;
  return found->second;
}

void CoreIPCBridge::ReplaceEditorCapability(
    std::optional<std::string> const& old_token,
    std::string const& new_token,
    EditorIdentity const& identity) {
  std::lock_guard guard(mutex_);
  if (old_token) {
    editor_identities_.erase(*old_token);
    editor_identity_order_.erase(
        std::remove(editor_identity_order_.begin(), editor_identity_order_.end(), *old_token),
        editor_identity_order_.end());
  }
  editor_identities_[new_token] = identity;
  editor_identity_order_.erase(
      std::remove(editor_identity_order_.begin(), editor_identity_order_.end(), new_token),
      editor_identity_order_.end());
  editor_identity_order_.push_back(new_token);
  while (editor_identity_order_.size() > 128) {
    auto expired = std::move(editor_identity_order_.front());
    editor_identity_order_.erase(editor_identity_order_.begin());
    editor_identities_.erase(expired);
  }
}

void CoreIPCBridge::RotateEditorCapability(
    std::string const& old_token,
    std::string const& new_token) {
  auto identity = EditorIdentityFor(old_token);
  if (identity) ReplaceEditorCapability(old_token, new_token, *identity);
}

}  // namespace LiteLLMMenu
