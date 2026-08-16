#pragma once

#include <atomic>
#include <condition_variable>
#include <exception>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace LiteLLMMenu {

class CoreIPCBridge {
 public:
  struct SecretCapability {
    std::string token;
    bool present = false;
  };

  struct SecretStageResult {
    double revision = 0;
    bool present = false;
  };

  struct SecretReadCapability {
    std::string token;
  };

  struct RelayLoginResult {
    double revision = 0;
    std::string username;
  };

  struct RelaySessionRestoreResult {
    double revision = 0;
    std::string login_status;
    std::string username;
  };

  static CoreIPCBridge& Shared();

  std::string Send(std::string const& request_json);
  std::optional<std::string> RegisterFileCapability(std::wstring const& path, std::string const& purpose);
  std::optional<SecretCapability> CreateSecretCapability(
      std::string const& domain,
      std::string const& field,
      std::optional<std::string> const& target,
      std::string const& purpose);
  std::optional<SecretStageResult> StageSecret(
      std::string const& secret_token,
      std::optional<std::string> const& value,
      bool clear);
  std::optional<SecretReadCapability> CreateSecretReadCapability(
      std::string const& domain,
      std::string const& field,
      std::optional<std::string> const& target);
  std::optional<std::string> ReadSecret(std::string const& secret_read_token);
  std::optional<std::string> ReadPlainTextSecret(
      std::string const& domain,
      std::string const& field,
      std::optional<std::string> const& target);
  std::optional<RelayLoginResult> AcceptRelayLogin(
      std::string const& account_id,
      std::string const& account_type,
      std::string const& label,
      std::string const& origin,
      std::string const& username,
      std::optional<std::string> const& cookie,
      std::optional<std::string> const& access_token,
      std::optional<std::string> const& refresh_token,
      std::optional<std::string> const& password = std::nullopt);
  std::optional<RelaySessionRestoreResult> RestoreRelaySession(
      std::string const& account_id,
      std::string const& account_type,
      std::string const& label,
      std::string const& origin,
      std::string const& login_status,
      std::optional<std::string> const& username,
      std::optional<std::string> const& cookie,
      std::optional<std::string> const& access_token,
      std::optional<std::string> const& refresh_token);
  void SetEventHandler(std::function<void(std::string const&)> handler);
  void Stop();

 private:
  struct Endpoint {
    std::wstring address;
    unsigned short port = 0;
    std::wstring bootstrap_token;
  };

  struct HttpResult {
    unsigned long status = 0;
    std::string body;
    std::wstring session;
  };

  struct Session {
    Endpoint endpoint;
    std::wstring token;
    unsigned long generation = 0;
  };

  CoreIPCBridge() = default;
  ~CoreIPCBridge();
  CoreIPCBridge(CoreIPCBridge const&) = delete;
  CoreIPCBridge& operator=(CoreIPCBridge const&) = delete;

  Session EnsureSession();
  Endpoint StartCoreLocked();
  HttpResult Request(
      Endpoint const& endpoint,
      std::wstring const& route,
      std::wstring const& method,
      std::string const& body,
      std::wstring const& token,
      int receive_timeout_ms = 30000);
  std::optional<HttpResult> HostRequest(
      std::wstring const& route,
      std::string const& body,
      bool retry_session = true,
      int receive_timeout_ms = 30000);
  void StartPollingIfSubscription(std::string const& response_json, unsigned long generation);
  void PollEvents(std::string subscription_id);
  void RecoverSubscription();
  void InvalidateCore(bool preserve_subscription);
  void InvalidateCoreIfGeneration(unsigned long generation, bool preserve_subscription);
  void InvalidateCoreLocked(bool preserve_subscription);
  void TakeCoreLocked(std::vector<void*>& processes, std::vector<std::wstring>& directories, bool preserve_subscription);
  void JoinRetiredPollThread();
  static bool IsSessionFailure(unsigned long status) noexcept;

  std::mutex mutex_;
  std::condition_variable session_condition_;
  std::optional<Endpoint> endpoint_;
  std::wstring session_token_;
  void* process_handle_ = nullptr;
  std::wstring runtime_directory_;
  std::vector<void*> retired_process_handles_;
  std::vector<std::wstring> retired_runtime_directories_;
  unsigned long session_expires_at_tick_ = 0;
  std::string subscription_id_;
  std::string subscription_request_;
  bool establishing_session_ = false;
  std::exception_ptr session_error_;
  unsigned long core_generation_ = 0;
  std::function<void(std::string const&)> event_handler_;
  std::atomic<bool> stopping_{false};
  std::mutex poll_mutex_;
  std::thread poll_thread_;
};

}  // namespace LiteLLMMenu
