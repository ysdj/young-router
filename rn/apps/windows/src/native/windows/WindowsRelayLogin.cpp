#include "pch.h"
#include "WindowsRelayLogin.h"
#include "CoreIPCBridge.h"
#include "WinUIControls.h"
#include "WinUI3NativeLeaf.h"

#include <wincred.h>
#include <winhttp.h>
#include <shlobj.h>
#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cwctype>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <vector>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.UI.Text.h>
#include <winrt/Microsoft.Web.WebView2.Core.h>
#include <winrt/Microsoft.UI.Interop.h>
#include <winrt/Microsoft.UI.Windowing.h>
#include <winrt/Microsoft.UI.Xaml.h>
#include <winrt/Microsoft.UI.Xaml.Controls.h>

namespace LiteLLMMenu {
namespace {

namespace json = winrt::Windows::Data::Json;
namespace web = winrt::Microsoft::Web::WebView2::Core;
namespace xaml = winrt::Microsoft::UI::Xaml;
namespace controls = winrt::Microsoft::UI::Xaml::Controls;

constexpr size_t kMaxSessionBytes = 96 * 1024;
constexpr size_t kMaxPasswordBytes = 4096;
constexpr size_t kCredentialChunkBytes = 2400;
constexpr size_t kMaxCredentialChunks = 48;
constexpr wchar_t kCredentialRoot[] = L"LiteLLM Menu/relay/";
constexpr wchar_t kImmediateWebPresentationScript[] = LR"JS((() => {
  const styleID = '__litellm_menu_immediate_presentation';
  const gradientPattern = /gradient[(]/i;
  const imageProperties = ['background-image', 'border-image-source', 'mask-image'];
  const splitImageLayers = (value) => {
    const layers = [];
    let depth = 0;
    let start = 0;
    for (let index = 0; index < value.length; index += 1) {
      const character = value[index];
      if (character === '(') depth += 1;
      else if (character === ')') depth = Math.max(0, depth - 1);
      else if (character === ',' && depth === 0) {
        layers.push(value.slice(start, index).trim());
        start = index + 1;
      }
    }
    layers.push(value.slice(start).trim());
    return layers;
  };
  const stripGradients = (root = document.documentElement) => {
    const elements = root instanceof Element
      ? [root, ...root.querySelectorAll('*')]
      : [...document.querySelectorAll('*')];
    for (const element of elements) {
      const computed = getComputedStyle(element);
      for (const property of imageProperties) {
        const image = computed.getPropertyValue(property);
        if (!gradientPattern.test(image)) continue;
        const retained = splitImageLayers(image).filter((layer) => !gradientPattern.test(layer));
        element.style.setProperty(property, retained.length ? retained.join(', ') : 'none', 'important');
      }
    }
  };
  const install = () => {
    if (document.getElementById(styleID)) return;
    const style = document.createElement('style');
    style.id = styleID;
    style.textContent = `
      html { scroll-behavior: auto !important; }
      html, body, * { scrollbar-width: none !important; }
      *::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
      *, *::before, *::after {
        animation: none !important;
        transition: none !important;
        scroll-behavior: auto !important;
        view-transition-name: none !important;
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  };
  install();
  stripGradients();
  document.addEventListener('DOMContentLoaded', () => { install(); stripGradients(); }, { once: true });
  new MutationObserver((records) => {
    install();
    records.forEach((record) => {
      if (record.type === 'attributes') stripGradients(record.target);
      else record.addedNodes.forEach((node) => { if (node.nodeType === Node.ELEMENT_NODE) stripGradients(node); });
    });
  }).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class', 'style'],
    childList: true,
    subtree: true,
  });
})())JS";
constexpr double kUIFontSize = 13.0;

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

std::string Trim(std::string value) {
  auto space = [](unsigned char character) { return std::isspace(character) != 0; };
  while (!value.empty() && space(static_cast<unsigned char>(value.front()))) value.erase(value.begin());
  while (!value.empty() && space(static_cast<unsigned char>(value.back()))) value.pop_back();
  return value;
}

bool ValidAccountID(std::string const& value) {
  if (value.empty() || value.size() > 96 || !std::isalnum(static_cast<unsigned char>(value.front()))) return false;
  return std::all_of(value.begin(), value.end(), [](unsigned char character) {
    return std::isalnum(character) || character == '.' || character == '_' || character == '-';
  });
}

uint64_t Fnv1a(std::string const& value) {
  uint64_t hash = 1469598103934665603ULL;
  for (unsigned char character : value) {
    hash ^= character;
    hash *= 1099511628211ULL;
  }
  return hash;
}

std::wstring Hex(uint64_t value) {
  std::wostringstream output;
  output << std::hex << value;
  return output.str();
}

std::wstring CredentialPrefix(std::string const& account_id, std::wstring_view kind) {
  return std::wstring(kCredentialRoot) + Utf8ToWide(account_id) + L"/" + std::wstring(kind);
}

std::optional<std::vector<uint8_t>> ReadCredential(std::wstring const& target) {
  PCREDENTIALW credential = nullptr;
  if (!CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &credential) || credential == nullptr) return std::nullopt;
  std::vector<uint8_t> value;
  if (credential->CredentialBlobSize > 0 && credential->CredentialBlob != nullptr) {
    value.assign(credential->CredentialBlob, credential->CredentialBlob + credential->CredentialBlobSize);
  }
  CredFree(credential);
  return value;
}

bool WriteCredential(std::wstring const& target, std::string const& account_id, std::vector<uint8_t> const& value) {
  if (target.empty() || value.empty() || value.size() > kCredentialChunkBytes) return false;
  auto username = Utf8ToWide(account_id);
  CREDENTIALW credential{};
  credential.Type = CRED_TYPE_GENERIC;
  credential.TargetName = const_cast<wchar_t*>(target.c_str());
  credential.CredentialBlobSize = static_cast<DWORD>(value.size());
  credential.CredentialBlob = const_cast<LPBYTE>(value.data());
  credential.Persist = CRED_PERSIST_LOCAL_MACHINE;
  credential.UserName = username.data();
  return CredWriteW(&credential, 0) != FALSE;
}

bool DeleteCredential(std::wstring const& target) {
  if (CredDeleteW(target.c_str(), CRED_TYPE_GENERIC, 0)) return true;
  return GetLastError() == ERROR_NOT_FOUND;
}

bool DeleteCredentialTree(std::wstring const& prefix) {
  DWORD count = 0;
  PCREDENTIALW* credentials = nullptr;
  auto filter = prefix + L"/*";
  if (!CredEnumerateW(filter.c_str(), 0, &count, &credentials)) {
    return GetLastError() == ERROR_NOT_FOUND;
  }
  bool removed = true;
  for (DWORD index = 0; index < count; ++index) {
    if (credentials[index] != nullptr && credentials[index]->TargetName != nullptr) {
      removed = DeleteCredential(credentials[index]->TargetName) && removed;
    }
  }
  CredFree(credentials);
  return removed;
}

struct CredentialMetadata {
  std::wstring generation;
  size_t chunks = 0;
  size_t size = 0;
  uint64_t hash = 0;
};

std::optional<CredentialMetadata> ReadMetadata(std::wstring const& prefix) {
  auto raw = ReadCredential(prefix + L"/meta");
  if (!raw || raw->empty() || raw->size() > 512) return std::nullopt;
  std::string text(raw->begin(), raw->end());
  std::vector<std::string> fields;
  size_t start = 0;
  while (start <= text.size()) {
    auto end = text.find('|', start);
    fields.push_back(text.substr(start, end == std::string::npos ? std::string::npos : end - start));
    if (end == std::string::npos) break;
    start = end + 1;
  }
  try {
    if (fields.size() != 4 || fields[0].empty() || fields[0].size() > 64 ||
        !std::all_of(fields[0].begin(), fields[0].end(), [](unsigned char c) { return std::isalnum(c); })) return std::nullopt;
    CredentialMetadata result;
    result.generation = Utf8ToWide(fields[0]);
    result.chunks = static_cast<size_t>(std::stoull(fields[1]));
    result.size = static_cast<size_t>(std::stoull(fields[2]));
    result.hash = std::stoull(fields[3], nullptr, 16);
    if (result.chunks == 0 || result.chunks > kMaxCredentialChunks || result.size == 0 || result.size > kMaxSessionBytes) {
      return std::nullopt;
    }
    return result;
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> ReadChunkedCredential(std::string const& account_id, std::wstring_view kind) {
  auto prefix = CredentialPrefix(account_id, kind);
  auto metadata = ReadMetadata(prefix);
  if (!metadata) return std::nullopt;
  std::string result;
  result.reserve(metadata->size);
  for (size_t index = 0; index < metadata->chunks; ++index) {
    auto chunk = ReadCredential(prefix + L"/" + metadata->generation + L"/" + std::to_wstring(index));
    if (!chunk || chunk->empty() || chunk->size() > kCredentialChunkBytes) return std::nullopt;
    result.append(reinterpret_cast<char const*>(chunk->data()), chunk->size());
  }
  if (result.size() != metadata->size || Fnv1a(result) != metadata->hash) return std::nullopt;
  return result;
}

void DeleteGeneration(std::wstring const& prefix, CredentialMetadata const& metadata) {
  for (size_t index = 0; index < metadata.chunks; ++index) {
    DeleteCredential(prefix + L"/" + metadata.generation + L"/" + std::to_wstring(index));
  }
}

bool ClearChunkedCredential(std::string const& account_id, std::wstring_view kind) {
  auto prefix = CredentialPrefix(account_id, kind);
  return DeleteCredentialTree(prefix);
}

bool WriteChunkedCredential(std::string const& account_id, std::wstring_view kind, std::optional<std::string> const& value) {
  if (!value || value->empty()) return ClearChunkedCredential(account_id, kind);
  if (value->size() > kMaxSessionBytes) return false;
  auto prefix = CredentialPrefix(account_id, kind);
  auto previous = ReadMetadata(prefix);
  static std::atomic_uint64_t sequence{0};
  auto generation = L"g" + Hex(GetTickCount64()) + L"p" + Hex(GetCurrentProcessId()) + L"n" + Hex(++sequence);
  size_t chunks = (value->size() + kCredentialChunkBytes - 1) / kCredentialChunkBytes;
  if (chunks == 0 || chunks > kMaxCredentialChunks) return false;
  size_t written = 0;
  for (; written < chunks; ++written) {
    size_t offset = written * kCredentialChunkBytes;
    size_t length = std::min(kCredentialChunkBytes, value->size() - offset);
    std::vector<uint8_t> bytes(value->begin() + static_cast<ptrdiff_t>(offset), value->begin() + static_cast<ptrdiff_t>(offset + length));
    if (!WriteCredential(prefix + L"/" + generation + L"/" + std::to_wstring(written), account_id, bytes)) break;
  }
  if (written != chunks) {
    for (size_t index = 0; index < written; ++index) DeleteCredential(prefix + L"/" + generation + L"/" + std::to_wstring(index));
    return false;
  }
  std::ostringstream metadata;
  metadata << WideToUtf8(generation) << '|' << chunks << '|' << value->size() << '|' << std::hex << Fnv1a(*value);
  auto metadata_text = metadata.str();
  std::vector<uint8_t> metadata_bytes(metadata_text.begin(), metadata_text.end());
  if (!WriteCredential(prefix + L"/meta", account_id, metadata_bytes)) {
    for (size_t index = 0; index < chunks; ++index) DeleteCredential(prefix + L"/" + generation + L"/" + std::to_wstring(index));
    return false;
  }
  if (previous) DeleteGeneration(prefix, *previous);
  return true;
}

struct StoredSession {
  std::string account_type;
  std::string origin;
  std::string cookie;
  std::string access_token;
  std::string refresh_token;
};

std::string EncodeSession(StoredSession const& session) {
  json::JsonObject object;
  object.SetNamedValue(L"accountType", json::JsonValue::CreateStringValue(Utf8ToWide(session.account_type)));
  object.SetNamedValue(L"origin", json::JsonValue::CreateStringValue(Utf8ToWide(session.origin)));
  object.SetNamedValue(L"cookie", json::JsonValue::CreateStringValue(Utf8ToWide(session.cookie)));
  object.SetNamedValue(L"accessToken", json::JsonValue::CreateStringValue(Utf8ToWide(session.access_token)));
  object.SetNamedValue(L"refreshToken", json::JsonValue::CreateStringValue(Utf8ToWide(session.refresh_token)));
  return WideToUtf8(object.Stringify().c_str());
}

std::optional<StoredSession> ReadSession(WindowsRelayLoginOptions const& options) {
  auto raw = ReadChunkedCredential(options.account_id, L"session");
  if (!raw || raw->size() > kMaxSessionBytes) return std::nullopt;
  try {
    auto object = json::JsonObject::Parse(Utf8ToWide(*raw));
    if (object.Size() != 5) return std::nullopt;
    StoredSession session{
        WideToUtf8(object.GetNamedString(L"accountType", L"").c_str()),
        WideToUtf8(object.GetNamedString(L"origin", L"").c_str()),
        WideToUtf8(object.GetNamedString(L"cookie", L"").c_str()),
        WideToUtf8(object.GetNamedString(L"accessToken", L"").c_str()),
        WideToUtf8(object.GetNamedString(L"refreshToken", L"").c_str()),
    };
    if (session.account_type != options.account_type || session.origin != options.origin ||
        session.cookie.size() > 32768 || session.access_token.size() > 32768 ||
        session.refresh_token.size() > 32768 ||
        (session.cookie.empty() && session.access_token.empty())) return std::nullopt;
    return session;
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> ReadPassword(WindowsRelayLoginOptions const& options) {
  auto raw = ReadChunkedCredential(options.account_id, L"password");
  if (!raw || raw->size() > 8192) return std::nullopt;
  try {
    auto object = json::JsonObject::Parse(Utf8ToWide(*raw));
    if (object.Size() != 3) return std::nullopt;
    if (WideToUtf8(object.GetNamedString(L"accountType", L"").c_str()) != options.account_type ||
        WideToUtf8(object.GetNamedString(L"origin", L"").c_str()) != options.origin) return std::nullopt;
    auto password = WideToUtf8(object.GetNamedString(L"password", L"").c_str());
    return !password.empty() && password.size() <= kMaxPasswordBytes ? std::optional<std::string>(password) : std::nullopt;
  } catch (...) {
    return std::nullopt;
  }
}

std::string EncodePassword(WindowsRelayLoginOptions const& options, std::string const& password) {
  json::JsonObject object;
  object.SetNamedValue(L"accountType", json::JsonValue::CreateStringValue(Utf8ToWide(options.account_type)));
  object.SetNamedValue(L"origin", json::JsonValue::CreateStringValue(Utf8ToWide(options.origin)));
  object.SetNamedValue(L"password", json::JsonValue::CreateStringValue(Utf8ToWide(password)));
  return WideToUtf8(object.Stringify().c_str());
}

std::wstring JsonLiteral(std::string const& value) {
  json::JsonArray array;
  array.Append(json::JsonValue::CreateStringValue(Utf8ToWide(value)));
  auto text = std::wstring(array.Stringify());
  return text.size() >= 2 ? text.substr(1, text.size() - 2) : L"\"\"";
}

bool ChineseLocale() {
  wchar_t locale[LOCALE_NAME_MAX_LENGTH]{};
  return GetUserDefaultLocaleName(locale, LOCALE_NAME_MAX_LENGTH) > 0 &&
      (_wcsnicmp(locale, L"zh", 2) == 0);
}

bool UseChinese(WindowsRelayLoginOptions const& options) {
  return options.language == "zh-Hans" ||
      (options.language == "system" && ChineseLocale());
}

std::wstring Text(WindowsRelayLoginOptions const& options, std::wstring_view english, std::wstring_view chinese) {
  return std::wstring(UseChinese(options) ? chinese : english);
}

struct ParsedOrigin {
  winrt::Windows::Foundation::Uri uri{nullptr};
  std::string value;
};

std::optional<ParsedOrigin> ParseOrigin(std::string value) {
  value = Trim(std::move(value));
  while (value.size() > 1 && value.back() == '/') value.pop_back();
  if (value.empty() || value.size() > 2048) return std::nullopt;
  try {
    winrt::Windows::Foundation::Uri uri(Utf8ToWide(value));
    auto scheme = std::wstring(uri.SchemeName());
    if ((_wcsicmp(scheme.c_str(), L"http") != 0 && _wcsicmp(scheme.c_str(), L"https") != 0) ||
        uri.Host().empty() || !uri.UserName().empty() || !uri.Password().empty() ||
        !uri.Query().empty() || !uri.Fragment().empty()) return std::nullopt;
    if (_wcsicmp(scheme.c_str(), L"https") != 0) {
      auto host = std::wstring(uri.Host());
      std::transform(host.begin(), host.end(), host.begin(), ::towlower);
      while (!host.empty() && host.back() == L'.') host.pop_back();
      bool loopback = host == L"localhost" ||
          (host.size() > 10 && host.compare(host.size() - 10, 10, L".localhost") == 0) ||
          host == L"127.0.0.1" || host == L"::1" || host == L"0:0:0:0:0:0:0:1";
      if (!loopback) return std::nullopt;
    }
    return ParsedOrigin{uri, std::move(value)};
  } catch (...) {
    return std::nullopt;
  }
}

bool SameOrigin(winrt::Windows::Foundation::Uri const& left, winrt::Windows::Foundation::Uri const& right) {
  auto effective_port = [](winrt::Windows::Foundation::Uri const& value) {
    auto port = value.Port();
    if (port > 0) return port;
    return _wcsicmp(value.SchemeName().c_str(), L"https") == 0 ? 443 : 80;
  };
  return _wcsicmp(left.SchemeName().c_str(), right.SchemeName().c_str()) == 0 &&
      _wcsicmp(left.Host().c_str(), right.Host().c_str()) == 0 &&
      effective_port(left) == effective_port(right);
}

struct WinHttpCloser {
  void operator()(HINTERNET handle) const noexcept {
    if (handle != nullptr) WinHttpCloseHandle(handle);
  }
};
using WinHttpHandle = std::unique_ptr<std::remove_pointer_t<HINTERNET>, WinHttpCloser>;

struct EndpointProbeResult {
  std::string username;
  std::string cookie;
  std::optional<std::string> access_token;
  std::optional<std::string> refresh_token;
};

std::optional<std::string> FirstJsonString(json::IJsonValue const& root, std::vector<std::vector<std::wstring>> const& paths) {
  for (auto const& path : paths) {
    try {
      json::IJsonValue current = root;
      for (auto const& key : path) current = current.GetObject().GetNamedValue(key);
      auto value = Trim(WideToUtf8(current.GetString().c_str()));
      if (!value.empty() && value.size() <= 32768) return value;
    } catch (...) {}
  }
  return std::nullopt;
}

std::string CookieHeader(std::map<std::string, std::string> const& values) {
  std::string result;
  for (auto const& [name, value] : values) {
    if (!result.empty()) result += "; ";
    result += name + "=" + value;
  }
  return result;
}

std::map<std::string, std::string> ParseCookieHeader(std::string const& header);

std::optional<EndpointProbeResult> ProbeEndpoint(
    WindowsRelayLoginOptions const& options,
    ParsedOrigin const& origin,
    std::string const& cookie_header,
    std::optional<std::string> const& captured_access,
    std::optional<std::string> const& captured_refresh,
    bool* confirmed_authentication_rejection = nullptr) {
  if (confirmed_authentication_rejection) *confirmed_authentication_rejection = false;
  if (cookie_header.find_first_of("\r\n") != std::string::npos ||
      (captured_access && captured_access->find_first_of("\r\n") != std::string::npos)) return std::nullopt;
  auto session = WinHttpHandle(WinHttpOpen(
      L"LiteLLM-Menu/1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
      WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
  if (!session) return std::nullopt;
  WinHttpSetTimeouts(session.get(), 12000, 12000, 12000, 12000);
  auto port = origin.uri.Port();
  if (port <= 0 || port > 65535) port = _wcsicmp(origin.uri.SchemeName().c_str(), L"https") == 0 ? 443 : 80;
  auto connection = WinHttpHandle(WinHttpConnect(
      session.get(), origin.uri.Host().c_str(), static_cast<INTERNET_PORT>(port), 0));
  if (!connection) return std::nullopt;
  auto base_path = std::wstring(origin.uri.Path());
  if (base_path.find(L"..") != std::wstring::npos || base_path.find(L'\\') != std::wstring::npos) return std::nullopt;
  while (!base_path.empty() && base_path.back() == L'/') base_path.pop_back();
  std::vector<std::pair<std::wstring, std::wstring>> probes = options.account_type == "newapi"
      ? std::vector<std::pair<std::wstring, std::wstring>>{{L"GET", L"api/user/self"}, {L"POST", L"api/user/auth/refresh"}}
      : std::vector<std::pair<std::wstring, std::wstring>>{{L"GET", L"api/v1/auth/me"}};
  bool saw_authentication_rejection = false;
  bool saw_non_authentication_failure = false;
  for (auto const& [method, suffix] : probes) {
    auto path = base_path + L"/" + suffix;
    DWORD flags = _wcsicmp(origin.uri.SchemeName().c_str(), L"https") == 0 ? WINHTTP_FLAG_SECURE : 0;
    auto request = WinHttpHandle(WinHttpOpenRequest(
        connection.get(), method.c_str(), path.c_str(), nullptr, WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES, flags));
    if (!request) {
      saw_non_authentication_failure = true;
      continue;
    }
    DWORD no_redirect = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
    WinHttpSetOption(request.get(), WINHTTP_OPTION_REDIRECT_POLICY, &no_redirect, sizeof(no_redirect));
    std::wstring headers = L"Accept: application/json\r\nContent-Type: application/json\r\n";
    std::wstring origin_header = origin.uri.SchemeName() + L"://" + origin.uri.Host();
    if (port != (_wcsicmp(origin.uri.SchemeName().c_str(), L"https") == 0 ? 443 : 80)) {
      origin_header += L":" + std::to_wstring(port);
    }
    headers += L"Origin: " + origin_header + L"\r\nReferer: " + origin_header + L"/\r\n";
    if (!cookie_header.empty()) headers += L"Cookie: " + Utf8ToWide(cookie_header) + L"\r\n";
    if (captured_access && !captured_access->empty()) headers += L"Authorization: Bearer " + Utf8ToWide(*captured_access) + L"\r\n";
    if (!WinHttpSendRequest(request.get(), headers.c_str(), static_cast<DWORD>(headers.size()),
                            WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(request.get(), nullptr)) {
      saw_non_authentication_failure = true;
      continue;
    }
    DWORD status = 0;
    DWORD status_size = sizeof(status);
    if (!WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size, WINHTTP_NO_HEADER_INDEX) ||
        status == 0) {
      saw_non_authentication_failure = true;
      continue;
    }
    if (status == 401 || status == 403) {
      saw_authentication_rejection = true;
      continue;
    }
    if (status < 200 || status >= 300) {
      saw_non_authentication_failure = true;
      continue;
    }
    std::string body;
    bool body_complete = true;
    while (body.size() <= 2 * 1024 * 1024) {
      DWORD available = 0;
      if (!WinHttpQueryDataAvailable(request.get(), &available)) {
        body_complete = false;
        break;
      }
      if (available == 0) break;
      if (body.size() + available > 2 * 1024 * 1024) {
        body.clear();
        body_complete = false;
        break;
      }
      auto offset = body.size();
      body.resize(offset + available);
      DWORD read = 0;
      if (!WinHttpReadData(request.get(), body.data() + offset, available, &read)) {
        body.clear();
        body_complete = false;
        break;
      }
      body.resize(offset + read);
    }
    if (!body_complete || body.empty()) {
      saw_non_authentication_failure = true;
      continue;
    }
    try {
      auto root = json::JsonValue::Parse(Utf8ToWide(body));
      auto username = FirstJsonString(root, {
          {L"data", L"username"}, {L"data", L"email"},
          {L"data", L"user", L"username"}, {L"data", L"user", L"email"},
          {L"email"}, {L"username"}}).value_or(options.username.value_or(""));
      auto access = FirstJsonString(root, {{L"data", L"access_token"}, {L"access_token"}});
      auto refresh = FirstJsonString(root, {{L"data", L"refresh_token"}, {L"refresh_token"}});
      if (!access) access = captured_access;
      if (!refresh) refresh = captured_refresh;
      auto cookies = ParseCookieHeader(cookie_header);
      DWORD index = 0;
      while (true) {
        DWORD size = 0;
        WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_SET_COOKIE, WINHTTP_HEADER_NAME_BY_INDEX,
                            WINHTTP_NO_OUTPUT_BUFFER, &size, &index);
        if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0 || size > 65536) break;
        std::wstring set_cookie(size / sizeof(wchar_t), L'\0');
        if (!WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_SET_COOKIE, WINHTTP_HEADER_NAME_BY_INDEX,
                                 set_cookie.data(), &size, &index)) break;
        set_cookie.resize(wcslen(set_cookie.c_str()));
        auto pair_end = set_cookie.find(L';');
        auto pair = WideToUtf8(set_cookie.substr(0, pair_end));
        auto equals = pair.find('=');
        auto name = equals == std::string::npos ? std::string{} : Trim(pair.substr(0, equals));
        if (!name.empty() && name.find_first_of("\r\n;=") == std::string::npos) {
          auto value = pair.substr(equals + 1);
          if (value.find_first_of("\r\n;") == std::string::npos) cookies[name] = std::move(value);
        }
      }
      auto accepted_cookie = CookieHeader(cookies);
      if (username.empty() || username.size() > 320 || accepted_cookie.size() > 32768 ||
          (accepted_cookie.empty() && (!access || access->empty()))) {
        saw_non_authentication_failure = true;
        continue;
      }
      return EndpointProbeResult{std::move(username), std::move(accepted_cookie), std::move(access), std::move(refresh)};
    } catch (...) {
      saw_non_authentication_failure = true;
    }
  }
  if (confirmed_authentication_rejection &&
      saw_authentication_rejection && !saw_non_authentication_failure) {
    *confirmed_authentication_rejection = true;
  }
  return std::nullopt;
}

std::wstring WebViewDataFolder() {
  PWSTR folder = nullptr;
  if (FAILED(SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_CREATE, nullptr, &folder)) || folder == nullptr) return {};
  std::filesystem::path path(folder);
  CoTaskMemFree(folder);
  path /= L"LiteLLM Menu";
  path /= L"WebView2";
  std::error_code error;
  std::filesystem::create_directories(path, error);
  return error ? std::wstring{} : path.wstring();
}

std::wstring ProfileName(std::string const& account_id) {
  return L"relay_" + Hex(Fnv1a(account_id));
}

bool RunOwnedWindow(xaml::Window const& dialog, HWND owner, std::atomic_bool& finished) {
  HWND handle = nullptr;
  winrt::check_hresult(dialog.as<::IWindowNative>()->get_WindowHandle(&handle));
  DisableWindowTransitions(handle);
  bool disable_owner = owner != nullptr && IsWindow(owner);
  if (disable_owner) {
    SetWindowLongPtrW(handle, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(owner));
    EnableWindow(owner, FALSE);
  }
  auto window_id = winrt::Microsoft::UI::GetWindowIdFromWindow(handle);
  const auto frame = FrameTrackSizeForContentDips(handle, 980, 820);
  winrt::Microsoft::UI::Windowing::AppWindow::GetFromWindowId(window_id).Resize({frame.x, frame.y});
  dialog.Activate();
  MSG message{};
  BOOL result = TRUE;
  while (!finished.load() && (result = GetMessageW(&message, nullptr, 0, 0)) > 0) {
    TranslateMessage(&message);
    DispatchMessageW(&message);
  }
  if (!finished.exchange(true)) {
    try { dialog.Close(); } catch (...) {}
  }
  if (disable_owner && IsWindow(owner)) {
    EnableWindow(owner, TRUE);
    SetForegroundWindow(owner);
  }
  if (result == 0) PostQuitMessage(static_cast<int>(message.wParam));
  return result >= 0;
}

class RelayLoginAttempt {
 public:
  enum class CancellationOutcome {
    Cancelled,
    Committing,
    Finished,
  };

  CancellationOutcome RequestCancellation() {
    std::lock_guard<std::mutex> lock(mutex_);
    switch (state_) {
      case State::Pending:
        state_ = State::Finished;
        return CancellationOutcome::Cancelled;
      case State::Committing:
        return CancellationOutcome::Committing;
      case State::Finished:
        return CancellationOutcome::Finished;
    }
    return CancellationOutcome::Finished;
  }

  bool BeginCommit() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (state_ != State::Pending) return false;
    state_ = State::Committing;
    return true;
  }

  bool IsPending() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_ == State::Pending;
  }

  bool IsCommitting() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_ == State::Committing;
  }

  void Finish() {
    std::lock_guard<std::mutex> lock(mutex_);
    state_ = State::Finished;
  }

 private:
  enum class State {
    Pending,
    Committing,
    Finished,
  };

  mutable std::mutex mutex_;
  State state_ = State::Pending;
};

struct LoginState {
  WindowsRelayLoginOptions options;
  ParsedOrigin origin;
  xaml::Window dialog{nullptr};
  controls::WebView2 webview{nullptr};
  controls::TextBlock status{nullptr};
  controls::Button check{nullptr};
  controls::Button cancel{nullptr};
  std::optional<StoredSession> restored_session;
  std::optional<std::string> restored_password;
  std::optional<std::string> captured_password;
  std::optional<WindowsRelayLoginResult> result;
  std::atomic_bool finished{false};
  std::atomic_bool canceled{false};
  std::atomic_bool checking{false};
  bool auto_probe_pending = false;
  bool auto_submit_saved_password_pending = false;
  bool did_auto_submit_saved_password = false;
  bool auto_login_attempt = false;
  bool dialog_closed_during_commit = false;
  std::shared_ptr<RelayLoginAttempt> active_attempt;
};

std::optional<std::string> JsonString(json::JsonObject const& object, wchar_t const* name) {
  auto value = WideToUtf8(object.GetNamedString(name, L"").c_str());
  return value.empty() ? std::nullopt : std::optional<std::string>(std::move(value));
}

std::map<std::string, std::string> ParseCookieHeader(std::string const& header) {
  std::map<std::string, std::string> result;
  size_t start = 0;
  while (start < header.size()) {
    auto end = header.find(';', start);
    auto pair = Trim(header.substr(start, end == std::string::npos ? std::string::npos : end - start));
    auto equals = pair.find('=');
    auto name = equals == std::string::npos ? std::string{} : Trim(pair.substr(0, equals));
    if (!name.empty()) result[name] = pair.substr(equals + 1);
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return result;
}

winrt::fire_and_forget ProbeLogin(
    std::shared_ptr<LoginState> state,
    std::shared_ptr<RelayLoginAttempt> attempt) {
  if (state->canceled.load() || state->finished.load() ||
      state->active_attempt != attempt || !attempt->IsPending()) {
    if (state->active_attempt == attempt) {
      state->active_attempt.reset();
      state->checking = false;
    }
    co_return;
  }
  state->check.IsEnabled(false);
  state->status.Text(Text(state->options, L"Verifying sign-in...", L"正在验证登录..."));
  auto dispatcher = state->dialog.DispatcherQueue();
  try {
    std::wstring script = LR"JS((() => {
      let userToken = '';
      try { const user = JSON.parse(localStorage.getItem('user') || 'null'); userToken = typeof user?.token === 'string' ? user.token : ''; } catch {}
      const access = (localStorage.getItem('auth_token') || localStorage.getItem('access_token') || userToken).slice(0,32768);
      const refresh = (localStorage.getItem('refresh_token') || '').slice(0,32768);
      const password = )JS" + std::wstring(state->options.remember_password
          ? L"(document.querySelector('input[type=password],input[autocomplete=current-password]')?.value || '').slice(0,4096)"
          : L"''") + LR"JS(;
      return JSON.stringify({password,accessToken:access,refreshToken:refresh});
    })())JS";
    auto raw_result = co_await state->webview.ExecuteScriptAsync(script);
    if (raw_result.size() > 196 * 1024) throw winrt::hresult_error(E_INVALIDARG);
    auto outer = json::JsonValue::Parse(raw_result);
    if (outer.ValueType() != json::JsonValueType::String) throw winrt::hresult_error(E_INVALIDARG);
    auto probe = json::JsonObject::Parse(outer.GetString());
    auto access_token = JsonString(probe, L"accessToken");
    auto refresh_token = JsonString(probe, L"refreshToken");
    auto password = JsonString(probe, L"password");
    if (state->options.remember_password && !password) password = state->captured_password;

    auto cookies = co_await state->webview.CoreWebView2().CookieManager().GetCookiesAsync(Utf8ToWide(state->options.origin));
    if (state->canceled.load() || state->finished.load() || !attempt->IsPending()) co_return;
    std::map<std::string, std::string> cookie_values;
    auto origin_host = std::wstring(state->origin.uri.Host());
    std::transform(origin_host.begin(), origin_host.end(), origin_host.begin(), ::towlower);
    for (auto const& cookie : cookies) {
      auto domain = std::wstring(cookie.Domain());
      std::transform(domain.begin(), domain.end(), domain.begin(), ::towlower);
      while (!domain.empty() && domain.front() == L'.') domain.erase(domain.begin());
      if (origin_host == domain || (origin_host.size() > domain.size() &&
          origin_host.compare(origin_host.size() - domain.size(), domain.size(), domain) == 0 &&
          origin_host[origin_host.size() - domain.size() - 1] == L'.')) {
        cookie_values[WideToUtf8(cookie.Name().c_str())] = WideToUtf8(cookie.Value().c_str());
      }
    }
    std::string cookie_header;
    for (auto const& [name, value] : cookie_values) {
      if (!cookie_header.empty()) cookie_header += "; ";
      cookie_header += name + "=" + value;
    }
    co_await winrt::resume_background();
    if (state->canceled.load() || state->finished.load() || !attempt->IsPending()) co_return;
    auto verified = ProbeEndpoint(state->options, state->origin, cookie_header, access_token, refresh_token);
    if (state->canceled.load() || state->finished.load() || !attempt->IsPending()) co_return;
    if (!verified) throw winrt::hresult_error(E_ACCESSDENIED);
    StoredSession session{state->options.account_type, state->options.origin, verified->cookie,
                          verified->access_token.value_or(""), verified->refresh_token.value_or("")};
    auto session_text = EncodeSession(session);
    if (!attempt->BeginCommit()) co_return;
    dispatcher.TryEnqueue([state, attempt] {
      if (state->finished.load()) return;
      state->cancel.IsEnabled(false);
      state->status.Text(Text(state->options, L"Saving sign-in...", L"正在保存登录..."));
    });
    auto prior_session = ReadChunkedCredential(state->options.account_id, L"session");
    auto prior_password = state->options.remember_password
        ? ReadChunkedCredential(state->options.account_id, L"password")
        : std::nullopt;
    bool credentials_saved = WriteChunkedCredential(state->options.account_id, L"session", session_text);
    if (credentials_saved) {
      if (state->options.remember_password && password && !password->empty()) {
        credentials_saved = WriteChunkedCredential(state->options.account_id, L"password", EncodePassword(state->options, *password));
      } else {
        credentials_saved = ClearChunkedCredential(state->options.account_id, L"password");
      }
    }
    if (!credentials_saved) {
      WriteChunkedCredential(state->options.account_id, L"session", prior_session);
      if (state->options.remember_password) {
        WriteChunkedCredential(state->options.account_id, L"password", prior_password);
      }
    }
    // AcceptRelayLogin is transactional in Core: a missing response is a
    // definitive failed import, so restore the native side to the same prior
    // state. A visible window close cannot cancel this synchronous boundary.
    auto accepted = credentials_saved ? CoreIPCBridge::Shared().AcceptRelayLogin(
        state->options.account_id, state->options.account_type, state->options.label,
        state->options.origin, verified->username,
        verified->cookie.empty() ? std::nullopt : std::optional<std::string>(verified->cookie),
        verified->access_token, verified->refresh_token,
        state->options.remember_password ? password : std::nullopt) : std::nullopt;
    // Once Core accepted the login, preserve the matching native credentials
    // even if the user closed the dialog while that synchronous IPC call ran.
    if (credentials_saved && !accepted) {
      WriteChunkedCredential(state->options.account_id, L"session", prior_session);
      if (state->options.remember_password) {
        WriteChunkedCredential(state->options.account_id, L"password", prior_password);
      }
    }
    auto completed_result = accepted
        ? std::optional<WindowsRelayLoginResult>(WindowsRelayLoginResult{accepted->revision, accepted->username})
        : std::nullopt;
    if (!dispatcher.TryEnqueue([state, attempt, accepted = std::move(accepted)]() mutable {
      if (state->active_attempt != attempt) return;
      if (!accepted) {
        attempt->Finish();
        state->active_attempt.reset();
        state->checking = false;
        if (state->dialog_closed_during_commit) {
          state->finished.store(true);
          return;
        }
        if (state->auto_login_attempt) {
          state->auto_login_attempt = false;
          state->finished.store(true);
          try { state->dialog.Close(); } catch (...) {}
          return;
        }
        state->check.IsEnabled(true);
        state->cancel.IsEnabled(true);
        state->status.Text(Text(state->options,
          L"No valid sign-in was found. Complete sign-in in the page and try again.",
          L"未检测到有效登录状态。请完成登录后重试。"));
        return;
      }
      attempt->Finish();
      state->active_attempt.reset();
      state->result = WindowsRelayLoginResult{accepted->revision, accepted->username};
      state->checking = false;
      state->finished.store(true);
      if (!state->dialog_closed_during_commit) {
        try { state->dialog.Close(); } catch (...) {}
      }
    })) {
      // The window can disappear while Core is synchronously importing the
      // verified session. If its dispatcher has already stopped, complete the
      // native result directly instead of leaving the owned-window loop open.
      attempt->Finish();
      state->active_attempt.reset();
      state->checking = false;
      if (completed_result) state->result = std::move(*completed_result);
      state->finished.store(true);
    }
  } catch (...) {
    if (state->canceled.load() || state->finished.load()) {
      if (attempt->IsCommitting()) attempt->Finish();
      co_return;
    }
    if (!dispatcher.TryEnqueue([state, attempt] {
      if (state->finished.load() || state->active_attempt != attempt) return;
      attempt->Finish();
      state->active_attempt.reset();
      state->checking = false;
      if (state->dialog_closed_during_commit) {
        state->finished.store(true);
        return;
      }
      if (state->auto_login_attempt) {
        state->auto_login_attempt = false;
        state->finished.store(true);
        try { state->dialog.Close(); } catch (...) {}
        return;
      }
      state->check.IsEnabled(true);
      state->cancel.IsEnabled(true);
      state->status.Text(Text(state->options,
          L"No valid sign-in was found. Complete sign-in in the page and try again.",
          L"未检测到有效登录状态。请完成登录后重试。"));
    })) {
      attempt->Finish();
      state->active_attempt.reset();
      state->checking = false;
      state->finished.store(true);
    }
  }
}

void StartLoginCheck(std::shared_ptr<LoginState> const& state) {
  if (!state || state->canceled.load() || state->finished.load()) return;
  bool expected = false;
  if (!state->checking.compare_exchange_strong(expected, true)) return;
  auto attempt = std::make_shared<RelayLoginAttempt>();
  state->active_attempt = attempt;
  state->dialog_closed_during_commit = false;
  ProbeLogin(state, std::move(attempt));
}

winrt::fire_and_forget InitializeBrowser(std::shared_ptr<LoginState> state) {
  try {
    auto folder = WebViewDataFolder();
    if (folder.empty()) throw winrt::hresult_error(E_FAIL);
    web::CoreWebView2EnvironmentOptions environment_options;
    environment_options.AdditionalBrowserArguments(L"--disable-features=msEdgeAutofill,PasswordManagerOnboarding");
    auto environment = co_await web::CoreWebView2Environment::CreateWithOptionsAsync(
        winrt::hstring{}, winrt::hstring(folder), environment_options);
    auto controller_options = environment.CreateCoreWebView2ControllerOptions();
    controller_options.ProfileName(ProfileName(state->options.account_id));
    controller_options.IsInPrivateModeEnabled(true);
    co_await state->webview.EnsureCoreWebView2Async(environment, controller_options);
    if (state->canceled.load()) co_return;
    auto core = state->webview.CoreWebView2();
    core.Settings().AreDevToolsEnabled(false);
    core.Settings().IsPasswordAutosaveEnabled(false);
    core.NewWindowRequested([](auto const&, web::CoreWebView2NewWindowRequestedEventArgs const& args) { args.Handled(true); });
    co_await core.AddScriptToExecuteOnDocumentCreatedAsync(kImmediateWebPresentationScript);
    if (state->options.remember_password) {
      core.WebMessageReceived([weak = std::weak_ptr<LoginState>(state)](auto const&, web::CoreWebView2WebMessageReceivedEventArgs const& args) {
        auto current = weak.lock();
        if (!current || current->canceled.load()) return;
        try {
          winrt::Windows::Foundation::Uri source(args.Source());
          if (!SameOrigin(source, current->origin.uri)) return;
          auto message = WideToUtf8(args.TryGetWebMessageAsString().c_str());
          constexpr char prefix[] = "relay-password:";
          if (message.rfind(prefix, 0) == 0) {
            auto value = message.substr(sizeof(prefix) - 1);
            if (!value.empty() && value.size() <= kMaxPasswordBytes) current->captured_password = std::move(value);
          }
        } catch {}
      });
      co_await core.AddScriptToExecuteOnDocumentCreatedAsync(LR"JS((() => {
        const capture = (node) => { const value=node?.value; if (typeof value==='string' && value.length) chrome.webview.postMessage(`relay-password:${value.slice(0,4096)}`); };
        document.addEventListener('input', e => { if (e.target?.matches?.('input[type=password],input[autocomplete=current-password]')) capture(e.target); }, true);
        document.addEventListener('change', e => { if (e.target?.matches?.('input[type=password],input[autocomplete=current-password]')) capture(e.target); }, true);
        document.addEventListener('submit', e => capture(e.target?.querySelector?.('input[type=password],input[autocomplete=current-password]')), true);
      })())JS");
    }

    if (state->restored_session) {
      auto manager = core.CookieManager();
      for (auto const& [name, value] : ParseCookieHeader(state->restored_session->cookie)) {
        if (name.empty()) continue;
        auto cookie = manager.CreateCookie(Utf8ToWide(name), Utf8ToWide(value), state->origin.uri.Host(), L"/");
        cookie.IsSecure(_wcsicmp(state->origin.uri.SchemeName().c_str(), L"https") == 0);
        manager.AddOrUpdateCookie(cookie);
      }
      state->auto_probe_pending = true;
    }
    // Deployments do not share a stable login path. Start at the configured
    // origin and let the site's own navigation expose its sign-in control.
    state->webview.Source(winrt::Windows::Foundation::Uri(Utf8ToWide(state->options.origin)));
  } catch (...) {
    if (!state->canceled.load() && !state->finished.load()) {
      state->status.Text(Text(state->options, L"The embedded sign-in browser is unavailable.", L"内置登录浏览器不可用。"));
      state->check.IsEnabled(false);
    }
  }
}

}  // namespace

std::optional<WindowsRelayLoginResult> RunWindowsRelayLogin(
    HWND owner,
    WindowsRelayLoginOptions const& input_options) {
  ConfigureImmediateXamlPresentation();
  if (!ValidAccountID(input_options.account_id) ||
      (input_options.account_type != "newapi" && input_options.account_type != "sub2api") ||
      input_options.label.empty() || input_options.label.size() > 160 ||
      (input_options.language != "system" && input_options.language != "en" && input_options.language != "zh-Hans") ||
      (input_options.username && input_options.username->size() > 320)) return std::nullopt;
  auto origin = ParseOrigin(input_options.origin);
  if (!origin) return std::nullopt;
  auto state = std::make_shared<LoginState>();
  state->options = input_options;
  state->options.origin = origin->value;
  state->origin = *origin;
  state->restored_session = ReadSession(state->options);
  if (state->options.remember_password) {
    state->restored_password = ReadPassword(state->options);
  } else {
    ClearWindowsRelayPassword(state->options.account_id);
  }

  xaml::Window dialog;
  state->dialog = dialog;
  dialog.Title(Text(state->options, L"Relay Account Sign In", L"中转站账号登录"));
  controls::Grid root;
  controls::RowDefinition header_row;
  header_row.Height(xaml::GridLengthHelper::Auto());
  root.RowDefinitions().Append(header_row);
  root.RowDefinitions().Append(controls::RowDefinition());

  controls::Grid header;
  header.Margin(xaml::Thickness{18, 12, 18, 12});
  header.ColumnDefinitions().Append(controls::ColumnDefinition());
  controls::ColumnDefinition action_column;
  action_column.Width(xaml::GridLengthHelper::Auto());
  header.ColumnDefinitions().Append(action_column);
  controls::StackPanel labels;
  labels.Spacing(3);
  controls::TextBlock title;
  title.Text(Utf8ToWide(state->options.label));
  title.FontSize(kUIFontSize);
  title.FontWeight(winrt::Windows::UI::Text::FontWeights::SemiBold());
  labels.Children().Append(title);
  controls::TextBlock account;
  account.FontSize(kUIFontSize);
  account.Text(Utf8ToWide(state->options.origin));
  account.TextTrimming(xaml::TextTrimming::CharacterEllipsis);
  labels.Children().Append(account);
  controls::TextBlock status;
  status.FontSize(kUIFontSize);
  state->status = status;
  status.Text(Text(state->options, L"Complete sign-in in the page; then select Verify Sign-In.", L"请在页面中完成登录，然后选择“验证登录”。"));
  status.TextWrapping(xaml::TextWrapping::Wrap);
  labels.Children().Append(status);
  header.Children().Append(labels);

  controls::StackPanel actions;
  actions.Orientation(controls::Orientation::Horizontal);
  actions.Spacing(8);
  actions.SetValue(controls::Grid::ColumnProperty(), winrt::box_value(1));
  actions.VerticalAlignment(xaml::VerticalAlignment::Center);
  controls::Button check;
  check.FontSize(kUIFontSize);
  state->check = check;
  check.Content(winrt::box_value(Text(state->options, L"Verify Sign-In", L"验证登录")));
  check.IsEnabled(false);
  controls::Button cancel;
  cancel.FontSize(kUIFontSize);
  state->cancel = cancel;
  cancel.Content(winrt::box_value(Text(state->options, L"Cancel", L"取消")));
  actions.Children().Append(check);
  actions.Children().Append(cancel);
  header.Children().Append(actions);
  root.Children().Append(header);

  controls::WebView2 webview;
  state->webview = webview;
  webview.SetValue(controls::Grid::RowProperty(), winrt::box_value(1));
  root.Children().Append(webview);
  dialog.Content(root);

  webview.NavigationStarting([weak = std::weak_ptr<LoginState>(state)](auto const&, web::CoreWebView2NavigationStartingEventArgs const& args) {
    auto current = weak.lock();
    if (!current) return;
    try {
      winrt::Windows::Foundation::Uri destination(args.Uri());
      if (!SameOrigin(destination, current->origin.uri)) args.Cancel(true);
    } catch (...) { args.Cancel(true); }
  });
  webview.NavigationCompleted([weak = std::weak_ptr<LoginState>(state)](auto const&, web::CoreWebView2NavigationCompletedEventArgs const& args) {
    auto current = weak.lock();
    if (!current || !args.IsSuccess() || current->canceled.load() || current->finished.load()) return;
    auto username = current->options.username.value_or("");
    auto password = current->restored_password.value_or("");
    auto access = current->restored_session ? current->restored_session->access_token : "";
    auto refresh = current->restored_session ? current->restored_session->refresh_token : "";
    bool auto_submit_saved_password = current->options.remember_password && !password.empty() &&
        !current->auto_submit_saved_password_pending && !current->did_auto_submit_saved_password;
    if (auto_submit_saved_password) current->auto_submit_saved_password_pending = true;
    auto password_assignment = current->options.remember_password
        ? (L"set(document.querySelector('input[type=password],input[autocomplete=current-password]'), " + JsonLiteral(password) + L");")
        : std::wstring{};
    std::wstring script = LR"JS((() => {
      const set = (node,value) => { if (!node || !value || node.value) return; const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set; setter?.call(node,value); node.dispatchEvent(new Event('input',{bubbles:true})); node.dispatchEvent(new Event('change',{bubbles:true})); };
      const userInput = document.querySelector('input[type=email],input[type=text],input:not([type]),input[name=email],input[name=username],input[autocomplete=username],input[placeholder*="用户名"],input[placeholder*="email" i]');
      const passwordInput = document.querySelector('input[type=password],input[autocomplete=current-password]');
      if (!userInput) {
        const signIn = Array.from(document.querySelectorAll('a,button')).find(node => /sign in|log in|登录/i.test((node.textContent || node.getAttribute('aria-label') || '').trim()));
        if (signIn instanceof HTMLElement) signIn.click();
      }
      set(userInput, )JS" + JsonLiteral(username) + LR"JS();
      )JS" + password_assignment + LR"JS(
      if (userInput instanceof HTMLElement) {
        userInput.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
        const active = document.activeElement;
        if (!active || active === document.body || !(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement || active.isContentEditable)) {
          try { userInput.focus({ preventScroll: true }); } catch { userInput.focus(); }
        }
      }
      const access=)JS" + JsonLiteral(access) + LR"JS(; const refresh=)JS" + JsonLiteral(refresh) + LR"JS(;
      const username=)JS" + JsonLiteral(state->options.username.value_or("")) + LR"JS(;
      if (access) {
        localStorage.setItem('auth_token',access); localStorage.setItem('access_token',access);
        try {
          const current=JSON.parse(localStorage.getItem('user')||'null');
          const user=current&&typeof current==='object'&&!Array.isArray(current)?current:{};
          user.token=access; if (username && !user.username) user.username=username;
          localStorage.setItem('user',JSON.stringify(user));
        } catch {}
      }
      if (refresh) localStorage.setItem('refresh_token',refresh);
      const hasCredentials = Boolean(userInput?.value && passwordInput?.value);
      if (hasCredentials && )JS" + std::wstring(auto_submit_saved_password ? L"true" : L"false") + LR"JS() {
        const form = passwordInput?.form;
        const submit = form?.querySelector('button[type="submit"],input[type="submit"],button:not([type])')
          ?? Array.from(document.querySelectorAll('button')).find(node => /sign in|log in|登录/i.test((node.textContent || node.getAttribute('aria-label') || '').trim()));
        if (submit instanceof HTMLElement) submit.click();
        else if (form instanceof HTMLFormElement && typeof form.requestSubmit === 'function') form.requestSubmit();
      }
      const agreementPattern = /agree|terms|privacy|consent|协议|同意|隐私|用户协议/i;
      const agreement = Array.from(document.querySelectorAll('input[type="checkbox"],[role="checkbox"]')).find((node) => {
        const scope = node.closest('label,li,form,section,div') || node.parentElement || node;
        return agreementPattern.test((scope.innerText || scope.textContent || '').trim());
      });
      if (agreement) (agreement.closest('label,li,form,section') || agreement).scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
      return hasCredentials;
    })())JS";
    auto after = [current, script = std::move(script), auto_submit_saved_password]() -> winrt::fire_and_forget {
      bool submitted_saved_password = false;
      try {
        auto result = co_await current->webview.ExecuteScriptAsync(script);
        submitted_saved_password = auto_submit_saved_password && result == L"true";
      } catch (...) {}
      if (auto_submit_saved_password) {
        current->auto_submit_saved_password_pending = false;
        current->did_auto_submit_saved_password = submitted_saved_password;
      }
      if (current->canceled.load() || current->finished.load()) co_return;
      current->check.IsEnabled(true);
      if (submitted_saved_password) co_await winrt::resume_after(std::chrono::milliseconds(1200));
      if (current->canceled.load() || current->finished.load()) co_return;
      if (current->auto_probe_pending || submitted_saved_password) {
        current->auto_probe_pending = false;
        current->auto_login_attempt = submitted_saved_password;
        StartLoginCheck(current);
      }
    };
    after();
  });
  check.Click([weak = std::weak_ptr<LoginState>(state)](auto const&, auto const&) {
    if (auto current = weak.lock()) StartLoginCheck(current);
  });
  cancel.Click([weak = std::weak_ptr<LoginState>(state)](auto const&, auto const&) {
    if (auto current = weak.lock()) {
      if (current->active_attempt) {
        auto const outcome = current->active_attempt->RequestCancellation();
        if (outcome == RelayLoginAttempt::CancellationOutcome::Committing) {
          current->dialog_closed_during_commit = true;
          current->dialog.Close();
          return;
        }
        if (outcome == RelayLoginAttempt::CancellationOutcome::Finished) return;
      }
      current->active_attempt.reset();
      current->canceled.store(true);
      current->dialog.Close();
    }
  });
  dialog.Closed([weak = std::weak_ptr<LoginState>(state)](auto const&, auto const&) {
    if (auto current = weak.lock()) {
      if (!current->result) {
        if (current->active_attempt) {
          auto const outcome = current->active_attempt->RequestCancellation();
          if (outcome == RelayLoginAttempt::CancellationOutcome::Committing) {
            current->dialog_closed_during_commit = true;
            return;
          }
        }
        current->active_attempt.reset();
        current->canceled.store(true);
      }
      current->finished.store(true);
    }
  });
  HWND dialog_handle = nullptr;
  winrt::check_hresult(dialog.as<::IWindowNative>()->get_WindowHandle(&dialog_handle));
  auto app_window = winrt::Microsoft::UI::Windowing::AppWindow::GetFromWindowId(
      winrt::Microsoft::UI::GetWindowIdFromWindow(dialog_handle));
  app_window.Closing([weak = std::weak_ptr<LoginState>(state)](
      auto const&, winrt::Microsoft::UI::Windowing::AppWindowClosingEventArgs const&) {
    if (auto current = weak.lock(); current && !current->result) {
      if (current->active_attempt) {
        auto const outcome = current->active_attempt->RequestCancellation();
        if (outcome == RelayLoginAttempt::CancellationOutcome::Committing) {
          current->dialog_closed_during_commit = true;
          return;
        }
      }
      current->active_attempt.reset();
      current->canceled.store(true);
    }
  });
  InitializeBrowser(state);
  RunOwnedWindow(dialog, owner, state->finished);
  return state->result;
}

std::optional<WindowsRelaySessionRestoreResult> RestoreWindowsRelaySession(
    WindowsRelayLoginOptions const& input_options) {
  if (!ValidAccountID(input_options.account_id) ||
      (input_options.account_type != "newapi" && input_options.account_type != "sub2api") ||
      input_options.label.empty() || input_options.label.size() > 160 ||
      (input_options.username && input_options.username->size() > 320)) return std::nullopt;
  auto origin = ParseOrigin(input_options.origin);
  if (!origin) return std::nullopt;
  WindowsRelayLoginOptions options = input_options;
  options.origin = origin->value;
  auto stored = ReadSession(options);
  if (!stored) {
    auto accepted = CoreIPCBridge::Shared().RestoreRelaySession(
        options.account_id, options.account_type, options.label, options.origin,
        "signed_out", std::nullopt, std::nullopt, std::nullopt, std::nullopt);
    return accepted ? WindowsRelaySessionRestoreResult{
        accepted->revision, accepted->login_status, accepted->username} : std::nullopt;
  }
  bool confirmed_authentication_rejection = false;
  auto verified = ProbeEndpoint(
      options, *origin, stored->cookie,
      stored->access_token.empty() ? std::nullopt : std::optional<std::string>(stored->access_token),
      stored->refresh_token.empty() ? std::nullopt : std::optional<std::string>(stored->refresh_token),
      &confirmed_authentication_rejection);
  // An unreachable relay is not evidence that the account logged out. Keep
  // its Core status unknown until a later successful validation occurs.
  if (!verified) {
    if (!confirmed_authentication_rejection) return std::nullopt;
    auto accepted = CoreIPCBridge::Shared().RestoreRelaySession(
        options.account_id, options.account_type, options.label, options.origin,
        "expired", std::nullopt, std::nullopt, std::nullopt, std::nullopt);
    return accepted ? WindowsRelaySessionRestoreResult{
        accepted->revision, accepted->login_status, accepted->username} : std::nullopt;
  }
  auto accepted = CoreIPCBridge::Shared().RestoreRelaySession(
      options.account_id, options.account_type, options.label, options.origin,
      "signed_in", verified->username,
      verified->cookie.empty() ? std::nullopt : std::optional<std::string>(verified->cookie),
      verified->access_token, verified->refresh_token);
  return accepted ? WindowsRelaySessionRestoreResult{
      accepted->revision, accepted->login_status, accepted->username} : std::nullopt;
}

bool ClearWindowsRelayPassword(std::string const& account_id) {
  return ValidAccountID(account_id) && ClearChunkedCredential(account_id, L"password");
}

bool ClearWindowsRelayCredentials(std::string const& account_id) {
  if (!ValidAccountID(account_id)) return false;
  bool password = ClearChunkedCredential(account_id, L"password");
  bool session = ClearChunkedCredential(account_id, L"session");
  return password && session;
}

}  // namespace LiteLLMMenu
