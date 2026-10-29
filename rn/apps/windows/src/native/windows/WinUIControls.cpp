#include "pch.h"
#include "WinUIControls.h"
#include "CoreIPCBridge.h"

#if defined(RNW_NEW_ARCH)

#include "codegen/react/components/YoungRouter/LiteLLMWinUICodeWebView.g.h"
#include "codegen/react/components/YoungRouter/LiteLLMWinUISecureTextInput.g.h"

#include <shlobj.h>
#include <winrt/Microsoft.UI.Xaml.Controls.h>
#include <winrt/Microsoft.UI.Xaml.Controls.Primitives.h>
#include <winrt/Microsoft.UI.Xaml.h>
#include <winrt/Microsoft.UI.Xaml.Automation.h>
#include <winrt/Microsoft.UI.Xaml.Input.h>
#include <winrt/Microsoft.UI.Xaml.Media.h>
#include <winrt/Microsoft.UI.Xaml.Media.Imaging.h>
#include <winrt/Microsoft.Web.WebView2.Core.h>
#include <winrt/Windows.ApplicationModel.DataTransfer.h>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.System.h>
#include <winrt/Windows.UI.Text.h>
#include <winrt/Windows.UI.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <iterator>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {


using winrt::Microsoft::ReactNative::Composition::ContentIslandComponentView;
using winrt::Microsoft::UI::Xaml::Controls::Button;
using winrt::Microsoft::UI::Xaml::Controls::Border;
using winrt::Microsoft::UI::Xaml::Controls::CheckBox;
using winrt::Microsoft::UI::Xaml::Controls::ComboBox;
using winrt::Microsoft::UI::Xaml::Controls::FontIcon;
using winrt::Microsoft::UI::Xaml::Controls::Grid;
using winrt::Microsoft::UI::Xaml::Controls::HyperlinkButton;
using winrt::Microsoft::UI::Xaml::Controls::Image;
using winrt::Microsoft::UI::Xaml::Controls::ListView;
using winrt::Microsoft::UI::Xaml::Controls::ListViewSelectionMode;
using winrt::Microsoft::UI::Xaml::Controls::Orientation;
using winrt::Microsoft::UI::Xaml::Controls::PasswordBox;
using winrt::Microsoft::UI::Xaml::Controls::ProgressRing;
using winrt::Microsoft::UI::Xaml::Controls::ScrollViewer;
using winrt::Microsoft::UI::Xaml::Controls::StackPanel;
using winrt::Microsoft::UI::Xaml::Controls::TextBox;
using winrt::Microsoft::UI::Xaml::Controls::TextBlock;
using winrt::Microsoft::UI::Xaml::Controls::ToolTipService;
using winrt::Microsoft::UI::Xaml::Automation::AutomationProperties;
using winrt::Microsoft::UI::Xaml::Controls::WebView2;
using winrt::Microsoft::UI::Xaml::Controls::Primitives::Thumb;
using winrt::Microsoft::UI::Xaml::Controls::Primitives::ToggleButton;
using winrt::Microsoft::UI::Xaml::Media::SolidColorBrush;
using winrt::Microsoft::UI::Xaml::Media::FontFamily;
using winrt::Microsoft::UI::Xaml::Thickness;

namespace web = winrt::Microsoft::Web::WebView2::Core;

constexpr double kUIFontSize = 13.0;
// Mirrors the Core's own raw-editor budget (``MAX_EDITOR_DOCUMENT_BYTES`` and
// ``MAX_MESSAGE_BYTES`` in ``src/young_router/core/protocol.py``).
constexpr size_t kMaxEditorDocumentBytes = 6 * 1024 * 1024;
constexpr size_t kMaxEditorMessageBytes = 16 * 1024 * 1024;

winrt::hstring ToHString(std::string const& value) {
  return winrt::to_hstring(value);
}

std::wstring CodeEditorWebViewDataFolder() {
  PWSTR folder = nullptr;
  if (FAILED(SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_CREATE, nullptr, &folder)) || folder == nullptr) {
    return {};
  }
  std::filesystem::path path(folder);
  CoTaskMemFree(folder);
  path /= L"Young Router";
  path /= L"CodeEditorWebView2";
  std::error_code error;
  std::filesystem::create_directories(path, error);
  return error ? std::wstring{} : path.wstring();
}

bool Enabled(std::optional<bool> const& disabled) {
  return !disabled.value_or(false);
}

struct SecureInputLifecycle final {
  std::atomic<bool> alive{true};
  std::atomic<uint64_t> generation{0};
  std::atomic<bool> staging{false};
};

struct CodeWebViewComponentView
    : winrt::implements<CodeWebViewComponentView, winrt::IInspectable>,
      winrt::YoungRouter::Codegen::BaseLiteLLMWinUICodeWebView<CodeWebViewComponentView> {
  ~CodeWebViewComponentView() {
    ReleaseWebView();
  }

  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    try {
      island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
      webview_ = WebView2{};
      auto weak = get_weak();
      navigation_completed_token_ = webview_.NavigationCompleted(
          [weak](auto const&, auto const& args) {
            if (!args.IsSuccess()) {
              if (auto current = weak.get(); current && !current->disposed_) {
                current->RecoverEditorPage("page_load_failed");
              }
            }
          });
      island_.Content(webview_);
      island_view.Connect(island_.ContentIsland());
      InitializeBrowser();
    } catch (...) {
      EmitEditorError("initialize_failed");
    }
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewProps> const& props,
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewProps> const& old_props) noexcept override {
    winrt::YoungRouter::Codegen::BaseLiteLLMWinUICodeWebView<CodeWebViewComponentView>::UpdateProps(
        view, props, old_props);
    ApplyProps(old_props);
  }

  void PrepareForRecycle(
      winrt::Microsoft::ReactNative::ComponentView const& view) noexcept {
    if (disposed_) return;
    disposed_ = true;
    ReleaseWebView();
    std::deque<std::string>{}.swap(emitted_editor_texts_);
    auto old_props = Props();
    winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewProps> empty_props;
    winrt::YoungRouter::Codegen::BaseLiteLLMWinUICodeWebView<CodeWebViewComponentView>::UpdateProps(
        view, empty_props, old_props);
    std::shared_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewEventEmitter> empty_emitter;
    winrt::YoungRouter::Codegen::BaseLiteLLMWinUICodeWebView<CodeWebViewComponentView>::UpdateEventEmitter(
        empty_emitter);
  }

 private:
  static int32_t BoundedCount(
      winrt::Windows::Data::Json::JsonObject const& values,
      wchar_t const* name) noexcept {
    try {
      const auto value = values.GetNamedNumber(name, 0.0);
      if (!std::isfinite(value)) return 0;
      return static_cast<int32_t>(std::clamp(
          value,
          0.0,
          static_cast<double>(std::numeric_limits<int32_t>::max())));
    } catch (...) {
      return 0;
    }
  }

  void InitializeBrowser() noexcept {
    auto weak = get_weak();
    [weak]() -> winrt::fire_and_forget {
      auto self = weak.get();
      if (!self || self->disposed_) co_return;
      try {
        const auto data_folder = CodeEditorWebViewDataFolder();
        if (data_folder.empty()) throw winrt::hresult_error(E_FAIL);
        web::CoreWebView2EnvironmentOptions environment_options;
        auto environment = co_await web::CoreWebView2Environment::CreateWithOptionsAsync(
            winrt::hstring{}, winrt::hstring(data_folder), environment_options);
        self = weak.get();
        if (!self || self->disposed_ || !self->webview_) co_return;
        auto controller_options = environment.CreateCoreWebView2ControllerOptions();
        co_await self->webview_.EnsureCoreWebView2Async(environment, controller_options);
        self = weak.get();
        if (!self || self->disposed_ || !self->webview_) co_return;
        self->core_ = self->webview_.CoreWebView2();
        self->web_message_token_ = self->core_.WebMessageReceived(
            [weak](auto const&, auto const& args) {
              if (auto current = weak.get()) current->HandleWebMessage(args);
            });
        self->browser_ready_ = true;
        self->NavigateToCurrentHtml();
      } catch (...) {
        if (auto current = weak.get(); current && !current->disposed_) {
          current->EmitEditorError("webview_initialization_failed");
        }
      }
    }();
  }

  void ApplyProps(
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewProps> const& old_props) noexcept {
    if (disposed_ || !Props()) return;
    auto const& props = *Props();
    if (props.html.empty() || props.documentKey.empty() ||
        (props.language != "json" && props.language != "toml" && props.language != "yaml" && props.language != "text") ||
        props.html.size() > kMaxEditorMessageBytes ||
        props.value.size() > kMaxEditorDocumentBytes ||
        props.baseline.size() > kMaxEditorDocumentBytes) {
      EmitEditorError("editor_document_too_large");
      return;
    }
    try {
      winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(
          webview_, ToHString(props.language.empty() ? "Source editor" : props.language + " source editor"));
    } catch (...) {
    }

    const bool html_changed = !old_props || old_props->html != props.html;
    const bool document_changed = !old_props || old_props->documentKey != props.documentKey;
    const bool value_changed = !old_props || old_props->value != props.value;
    auto echoed_editor_text = false;
    if (value_changed && !document_changed) {
      auto match = std::find(emitted_editor_texts_.begin(), emitted_editor_texts_.end(), props.value);
      if (match != emitted_editor_texts_.end()) {
        echoed_editor_text = true;
        emitted_editor_texts_.erase(match);
      }
    }
    if (document_changed) {
      emitted_editor_texts_.clear();
    }
    const bool editor_state_changed = document_changed ||
        (value_changed && !echoed_editor_text) ||
        !old_props ||
        old_props->baseline != props.baseline ||
        old_props->language != props.language ||
        old_props->readOnly != props.readOnly ||
        old_props->showDiff != props.showDiff;
    if (editor_state_changed) {
      ++editor_state_generation_;
      pending_sync_ = true;
    }
    if (html_changed) {
      html_state_generation_ = editor_state_generation_;
      page_recovery_attempts_ = 0;
      editor_ready_ = false;
      pending_sync_ = true;
      NavigateToCurrentHtml();
      return;
    }
    SynchronizeEditorIfReady();
  }

  void NavigateToCurrentHtml() noexcept {
    if (disposed_ || !browser_ready_ || !webview_ || !Props()) return;
    auto const& props = *Props();
    if (props.html.empty()) return;
    try {
      webview_.NavigateToString(ToHString(props.html));
    } catch (...) {
      EmitEditorError("page_load_failed");
    }
  }

  void RecoverEditorPage(std::string const& error) noexcept {
    if (page_recovery_attempts_ >= 1) {
      EmitEditorError(error);
      return;
    }
    ++page_recovery_attempts_;
    editor_ready_ = false;
    pending_sync_ = true;
    NavigateToCurrentHtml();
  }

  void SynchronizeEditorIfReady() noexcept {
    if (disposed_ || !browser_ready_ || !editor_ready_ || !pending_sync_ || !Props()) return;
    auto const& props = *Props();
    try {
      auto payload = winrt::Windows::Data::Json::JsonObject{};
      payload.Insert(L"type", winrt::Windows::Data::Json::JsonValue::CreateStringValue(L"replace"));
      payload.Insert(L"documentKey", winrt::Windows::Data::Json::JsonValue::CreateStringValue(ToHString(props.documentKey)));
      payload.Insert(L"value", winrt::Windows::Data::Json::JsonValue::CreateStringValue(ToHString(props.value)));
      payload.Insert(L"baseline", winrt::Windows::Data::Json::JsonValue::CreateStringValue(ToHString(props.baseline)));
      payload.Insert(L"language", winrt::Windows::Data::Json::JsonValue::CreateStringValue(ToHString(props.language)));
      payload.Insert(L"readOnly", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(props.readOnly.value_or(false)));
      payload.Insert(L"showDiff", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(props.showDiff.value_or(false)));
      auto script = winrt::hstring(L"window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(") +
          payload.Stringify() + L");";
      pending_sync_ = false;
      ExecuteEditorScript(std::move(script));
    } catch (...) {
      pending_sync_ = false;
      EmitEditorError("editor_payload_serialization_failed");
    }
  }

  void InsertClipboardText() noexcept {
    auto weak = get_weak();
    [weak]() -> winrt::fire_and_forget {
      auto self = weak.get();
      if (!self || self->disposed_) co_return;
      try {
        auto content = winrt::Windows::ApplicationModel::DataTransfer::Clipboard::GetContent();
        if (!content.Contains(winrt::Windows::ApplicationModel::DataTransfer::StandardDataFormats::Text())) co_return;
        auto clipboard_text = co_await content.GetTextAsync();
        auto current = weak.get();
        if (!current || current->disposed_) co_return;
        std::wstring value = clipboard_text.c_str();
        // Keep the injected payload bounded; the editor document itself is
        // capped at 2 MiB in the shared UI.
        constexpr size_t maximum_length = 262144;
        if (value.size() > maximum_length) value.resize(maximum_length);
        auto payload = winrt::Windows::Data::Json::JsonObject{};
        payload.Insert(L"type", winrt::Windows::Data::Json::JsonValue::CreateStringValue(L"insertText"));
        payload.Insert(L"text", winrt::Windows::Data::Json::JsonValue::CreateStringValue(winrt::hstring{value}));
        auto script = winrt::hstring(L"window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(") +
            payload.Stringify() + L");";
        current->ExecuteEditorScript(std::move(script));
      } catch (...) {
      }
    }();
  }

  void ExecuteEditorScript(winrt::hstring script) noexcept {
    auto weak = get_weak();
    [weak, script = std::move(script)]() -> winrt::fire_and_forget {
      auto self = weak.get();
      if (!self || self->disposed_ || !self->webview_) co_return;
      try {
        co_await self->webview_.ExecuteScriptAsync(script);
      } catch (...) {
        if (auto current = weak.get(); current && !current->disposed_) {
          current->EmitEditorError("javascript_evaluation_failed");
        }
      }
    }();
  }

  void HandleWebMessage(
      winrt::Microsoft::Web::WebView2::Core::CoreWebView2WebMessageReceivedEventArgs const& args) noexcept {
    if (disposed_) return;
    try {
      auto payload = winrt::Windows::Data::Json::JsonObject::Parse(args.TryGetWebMessageAsString());
      auto type = winrt::to_string(payload.GetNamedString(L"type", L""));
      if (type == "ready") {
        editor_ready_ = true;
        page_recovery_attempts_ = 0;
        const auto document_key = winrt::to_string(payload.GetNamedString(L"documentKey", L""));
        const auto props = Props();
        const bool initial_document_is_current = props &&
            document_key == props->documentKey &&
            html_state_generation_ == editor_state_generation_;
        pending_sync_ = !initial_document_is_current;
        SynchronizeEditorIfReady();
        return;
      }
      if (type == "error") {
        EmitEditorError(winrt::to_string(payload.GetNamedString(L"message", L"editor_error")));
        return;
      }
      if (type == "paste") {
        InsertClipboardText();
        return;
      }
      if (type != "change") {
        EmitEditorError("unknown_editor_message");
        return;
      }

      const auto text = winrt::to_string(payload.GetNamedString(L"text", L""));
      if (!payload.HasKey(L"text")) {
        EmitEditorError("invalid_editor_change");
        return;
      }
      emitted_editor_texts_.push_back(text);
      if (emitted_editor_texts_.size() > 8) emitted_editor_texts_.pop_front();
      if (!EventEmitter()) return;
      winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewEventEmitter::OnEditorChange event;
      event.text = text;
      event.added = BoundedCount(payload, L"added");
      event.changed = BoundedCount(payload, L"changed");
      event.deleted = BoundedCount(payload, L"deleted");
      EventEmitter()->onEditorChange(std::move(event));
    } catch (...) {
      EmitEditorError("invalid_editor_message");
    }
  }

  void EmitEditorError(std::string message) noexcept {
    if (disposed_) return;
    if (auto emitter = EventEmitter()) {
      winrt::YoungRouter::Codegen::LiteLLMWinUICodeWebViewEventEmitter::OnEditorError event;
      event.message = std::move(message);
      emitter->onEditorError(std::move(event));
    }
  }

  void ReleaseWebView() noexcept {
    browser_ready_ = false;
    editor_ready_ = false;
    pending_sync_ = false;
    editor_state_generation_ = 0;
    html_state_generation_ = 0;
    page_recovery_attempts_ = 0;
    try {
      if (core_ && web_message_token_.value != 0) {
        core_.WebMessageReceived(web_message_token_);
      }
    } catch (...) {
    }
    web_message_token_ = {};
    try {
      if (webview_ && navigation_completed_token_.value != 0) {
        webview_.NavigationCompleted(navigation_completed_token_);
      }
    } catch (...) {
    }
    navigation_completed_token_ = {};
    try {
      if (core_) core_.Stop();
    } catch (...) {
    }
    try {
      if (webview_) webview_.NavigateToString(L"");
    } catch (...) {
    }
    try {
      if (island_) {
        island_.Content(winrt::Microsoft::UI::Xaml::UIElement{nullptr});
      }
    } catch (...) {
    }
    core_ = nullptr;
    webview_ = nullptr;
    island_ = nullptr;
  }

  bool disposed_ = false;
  bool browser_ready_ = false;
  bool editor_ready_ = false;
  bool pending_sync_ = false;
  uint64_t editor_state_generation_ = 0;
  uint64_t html_state_generation_ = 0;
  uint32_t page_recovery_attempts_ = 0;
  std::deque<std::string> emitted_editor_texts_;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  WebView2 webview_{nullptr};
  winrt::Microsoft::Web::WebView2::Core::CoreWebView2 core_{nullptr};
  winrt::event_token web_message_token_{};
  winrt::event_token navigation_completed_token_{};
};

void RegisterCodeWebViewRecycleHandler(
    ContentIslandComponentView const& island_view,
    winrt::com_ptr<CodeWebViewComponentView> const& user_data) noexcept {
  auto weak = user_data->get_weak();
  // RNW invokes ContentIslandComponentView::prepareForRecycle immediately before
  // this Destroying event. Codegen user data has no recycle callback of its own.
  island_view.Destroying(
      [weak](auto const&, winrt::Microsoft::ReactNative::ComponentView const& view) noexcept {
        if (auto current = weak.get()) current->PrepareForRecycle(view);
      });
}

struct SecureTextInputComponentView
    : winrt::implements<SecureTextInputComponentView, winrt::IInspectable>,
      winrt::YoungRouter::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView> {
  SecureTextInputComponentView() = default;

  ~SecureTextInputComponentView() {
    if (lifecycle_) {
      lifecycle_->alive.store(false, std::memory_order_release);
      lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel);
    }
    try {
      if (password_box_ && password_changed_token_.value != 0) {
        password_box_.PasswordChanged(password_changed_token_);
      }
      if (password_box_ && lost_focus_token_.value != 0) {
        password_box_.LostFocus(lost_focus_token_);
      }
      if (password_box_ && key_down_token_.value != 0) {
        password_box_.KeyDown(key_down_token_);
      }
      if (multiline_box_ && text_changed_token_.value != 0) {
        multiline_box_.TextChanged(text_changed_token_);
      }
      if (multiline_box_ && multiline_lost_focus_token_.value != 0) {
        multiline_box_.LostFocus(multiline_lost_focus_token_);
      }
    } catch (...) {
    }
  }

  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    try {
      island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
      password_box_ = PasswordBox{};
      password_box_.FontSize(kUIFontSize);
      // Keep secret fields visually and typographically identical to regular
      // inputs. PasswordBox otherwise inherits template padding that differs
      // from TextBox and can make a 13pt value look vertically clipped.
      password_box_.MinHeight(30.0);
      password_box_.Padding(winrt::Microsoft::UI::Xaml::Thickness{8, 0, 8, 0});
      password_box_.VerticalContentAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Center);
      password_box_.PasswordRevealMode(winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Hidden);
      password_box_.MaxLength(16 * 1024);
      dispatcher_ = winrt::Microsoft::UI::Dispatching::DispatcherQueue::GetForCurrentThread();
      auto weak_self = get_weak();
      password_changed_token_ = password_box_.PasswordChanged([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->MarkDirty();
      });
      lost_focus_token_ = password_box_.LostFocus([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->StageOnBlur();
      });
      key_down_token_ = password_box_.KeyDown(
          [weak_self](auto const&, winrt::Microsoft::UI::Xaml::Input::KeyRoutedEventArgs const& args) {
            if (args.Key() != winrt::Windows::System::VirtualKey::Enter) return;
            if (auto self = weak_self.get()) self->StageOnSubmit();
          });
      island_.Content(password_box_);
      island_view.Connect(island_.ContentIsland());
      ApplyProps(nullptr);
    } catch (...) {
      EmitState(0, false, "error", "initialize_failed", last_commit_request_);
    }
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputProps> const& props,
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputProps> const& old_props) noexcept override {
    winrt::YoungRouter::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView>::UpdateProps(
        view, props, old_props);
    ApplyProps(old_props);
  }

  void UpdateEventEmitter(
      std::shared_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputEventEmitter> const& emitter) noexcept override {
    winrt::YoungRouter::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView>::UpdateEventEmitter(emitter);
    if (emitter && !last_status_.empty()) {
      EmitState(last_revision_, last_present_, last_status_, last_error_, last_commit_request_);
    }
  }

 private:
  bool Current(uint64_t generation) const noexcept {
    return lifecycle_ && lifecycle_->alive.load(std::memory_order_acquire) &&
        lifecycle_->generation.load(std::memory_order_acquire) == generation;
  }

  bool IsPlainTextAutoCommitField(
      winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputProps const& props) const noexcept {
    if (!props.plainText.value_or(false) || !props.autoCommit.value_or(false)) return false;
    if (props.domain == "providers_models") {
      return props.field == "api_key" && !props.target.empty();
    }
    if (props.domain == "relay_accounts") {
      return props.field == "api_key" && !props.target.empty();
    }
    if (props.domain == "codex") {
      return props.field == "api_key" && props.target.empty();
    }
    if (props.domain == "claude") {
      return props.target.empty() &&
          (props.field == "deployment_token" || props.field == "desktop_gateway_api_key");
    }
    if (props.domain != "runtime" || props.field != "setting") return false;
    // The General pane's proxy key, the one credential a window draws in the
    // clear so it can be copied into a client.
    return props.target == "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON" ||
        props.target == "YOUNG_ROUTER_MASTER_KEY";
  }

  bool IsPlainTextAutoCommitField() const noexcept {
    return Props() && IsPlainTextAutoCommitField(*Props());
  }

  bool IsMultiline() const noexcept {
    return Props() && Props()->multiline.value_or(false);
  }

  // A TextBox may fail to construct in this island on some Windows builds; a
  // single-line PasswordBox must still work, so its creation is lazy and its
  // failure is reported only for a field that actually asks for multiline.
  bool EnsureMultilineBox() noexcept {
    if (multiline_box_) return true;
    try {
      multiline_box_ = TextBox{};
      multiline_box_.FontSize(kUIFontSize);
      multiline_box_.MinHeight(72.0);
      multiline_box_.Padding(winrt::Microsoft::UI::Xaml::Thickness{8, 4, 8, 4});
      multiline_box_.VerticalContentAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Top);
      multiline_box_.AcceptsReturn(true);
      multiline_box_.TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::Wrap);
      multiline_box_.MaxLength(16 * 1024);
      auto weak_self = get_weak();
      text_changed_token_ = multiline_box_.TextChanged([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->MarkDirty();
      });
      multiline_lost_focus_token_ = multiline_box_.LostFocus([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->StageOnBlur();
      });
      return true;
    } catch (...) {
      multiline_box_ = nullptr;
      return false;
    }
  }

  void SetInput(winrt::hstring const& value) noexcept {
    syncing_ = true;
    if (IsMultiline()) {
      multiline_box_.Text(value);
    } else {
      password_box_.Password(value);
    }
    syncing_ = false;
  }

  void ClearInputs() noexcept {
    syncing_ = true;
    if (password_box_) password_box_.Password(winrt::hstring{});
    if (multiline_box_) multiline_box_.Text(winrt::hstring{});
    syncing_ = false;
  }

  std::wstring InputValue() const {
    return IsMultiline() && multiline_box_
        ? std::wstring{multiline_box_.Text().c_str()}
        : std::wstring{password_box_.Password().c_str()};
  }

  void SetInputEnabled(bool enabled) noexcept {
    if (IsMultiline() && multiline_box_) {
      multiline_box_.IsEnabled(enabled);
    } else {
      password_box_.IsEnabled(enabled);
    }
  }

  void ApplyProps(
      winrt::com_ptr<winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputProps> const& old_props) noexcept {
    if (!password_box_ || !Props()) return;
    auto const& props = *Props();
    if (IsMultiline() && !EnsureMultilineBox()) {
      EmitState(last_revision_, last_present_, "error", "initialize_failed", last_commit_request_);
      return;
    }
    const bool identity_changed = !old_props || old_props->domain != props.domain ||
        old_props->field != props.field || old_props->target != props.target;
    const bool plain_key_mode_changed = !old_props ||
        old_props->plainText != props.plainText || old_props->autoCommit != props.autoCommit;
    const bool multiline_changed = !old_props || old_props->multiline != props.multiline;
    const bool reset_identity = identity_changed || plain_key_mode_changed || multiline_changed;
    const bool should_load_plaintext = reset_identity && IsPlainTextAutoCommitField(props);
    const bool placeholder_changed = !old_props || old_props->placeholder != props.placeholder;
    const bool plain_text_changed = !old_props || old_props->plainText != props.plainText;
    const bool label_changed = !old_props || old_props->label != props.label;
    const bool disabled_changed = !old_props || old_props->disabled != props.disabled;
    if (reset_identity) {
      lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel);
      lifecycle_->staging.store(false, std::memory_order_release);
      loading_ = false;
      active_domain_ = props.domain;
      active_field_ = props.field;
      active_target_ = props.target;
      ClearInputs();
      dirty_ = false;
      last_revision_ = 0;
      last_present_ = false;
      last_status_ = "ready";
      last_error_.clear();
    }
    if (placeholder_changed) {
      password_box_.PlaceholderText(ToHString(props.placeholder.value_or("")));
      if (multiline_box_) multiline_box_.PlaceholderText(ToHString(props.placeholder.value_or("")));
    }
    if (plain_text_changed) {
      password_box_.PasswordRevealMode(props.plainText.value_or(false)
          ? winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Visible
          : winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Hidden);
    }
    if (label_changed) {
      winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(password_box_, ToHString(props.label));
      if (multiline_box_) {
        winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(multiline_box_, ToHString(props.label));
      }
    }
    if (multiline_changed) {
      if (IsMultiline() && multiline_box_) {
        island_.Content(multiline_box_);
      } else {
        island_.Content(password_box_);
      }
    }
    if (disabled_changed || reset_identity) {
      SetInputEnabled(Enabled(props.disabled) && !lifecycle_->staging.load(std::memory_order_acquire) && !loading_);
    }
    const int32_t reset_request = props.resetRequest.value_or(0);
    if ((!old_props || old_props->resetRequest != props.resetRequest) && reset_request != last_reset_request_) {
      last_reset_request_ = reset_request;
      ClearInputs();
      dirty_ = false;
      if (!lifecycle_->staging.load(std::memory_order_acquire)) {
        EmitState(last_revision_, last_present_, "ready", "", last_commit_request_);
      }
    }
    const int32_t commit_request = props.commitRequest.value_or(0);
    if ((!old_props || old_props->commitRequest != props.commitRequest) && commit_request != last_commit_request_) {
      StageForRequest(commit_request, false);
    }
    if (should_load_plaintext) {
      LoadPlainTextSecret();
    }
  }

  void MarkDirty() noexcept {
    if (syncing_ || !lifecycle_ || lifecycle_->staging.load(std::memory_order_acquire)) return;
    try {
      if (InputValue().empty() && !IsPlainTextAutoCommitField()) return;
      dirty_ = true;
      EmitState(last_revision_, last_present_, "dirty", "", last_commit_request_);
    } catch (...) {
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
    }
  }

  void StageOnBlur() noexcept {
    if (!IsPlainTextAutoCommitField() || !dirty_) return;
    StageForRequest(NextAutoCommitRequest(), true);
  }

  void StageOnSubmit() noexcept {
    if (!IsPlainTextAutoCommitField() || !dirty_) return;
    StageForRequest(NextAutoCommitRequest(), true);
  }

  int32_t NextAutoCommitRequest() noexcept {
    next_auto_commit_request_ = std::max(next_auto_commit_request_, last_commit_request_);
    if (next_auto_commit_request_ < std::numeric_limits<int32_t>::max()) {
      ++next_auto_commit_request_;
    }
    return next_auto_commit_request_;
  }

  void StageForRequest(int32_t requested_commit, bool allow_empty) noexcept {
    if (!Props() || !password_box_ || (IsMultiline() && !multiline_box_) || lifecycle_->staging.load(std::memory_order_acquire)) return;
    std::wstring password;
    try {
      password = InputValue();
    } catch (...) {
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    if (password.empty() && !allow_empty) {
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      EmitState(last_revision_, last_present_, "ready", "", last_commit_request_);
      return;
    }
    if (active_domain_.empty() || active_field_.empty()) {
      ClearInputs();
      if (IsPlainTextAutoCommitField()) dirty_ = true;
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    std::optional<std::string> secret;
    try {
      secret = winrt::to_string(winrt::hstring{password});
    } catch (...) {
      secret.reset();
    }
    const bool preserve_input = IsPlainTextAutoCommitField();
    if (!preserve_input) {
      ClearInputs();
    }
    if (!secret || secret->size() > 16 * 1024) {
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      if (IsPlainTextAutoCommitField()) dirty_ = true;
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    bool expected = false;
    if (!lifecycle_->staging.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) return;
    SetInputEnabled(false);
    dirty_ = false;
    last_commit_request_ = std::max(last_commit_request_, requested_commit);
    const auto generation = lifecycle_->generation.load(std::memory_order_acquire);
    const bool disabled = Props()->disabled.value_or(false);
    EmitState(last_revision_, last_present_, "saving", "", last_commit_request_);
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    auto domain = active_domain_;
    auto field = active_field_;
    auto target = active_target_;
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, disabled, domain = std::move(domain), field = std::move(field), target = std::move(target), secret = std::move(*secret)]() mutable {
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation) return;
        auto capability = YoungRouter::CoreIPCBridge::Shared().CreateSecretCapability(
            domain, field, target.empty() ? std::nullopt : std::optional<std::string>{target}, "settings");
        std::optional<YoungRouter::CoreIPCBridge::SecretStageResult> result;
        if (capability) result = YoungRouter::CoreIPCBridge::Shared().StageSecret(capability->token, secret, false);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) return;
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, disabled, result = std::move(result)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) return;
          if (auto self = weak_self.get()) self->FinishStage(generation, disabled, std::move(result));
        });
      }).detach();
    } catch (...) {
      lifecycle_->staging.store(false, std::memory_order_release);
      SetInputEnabled(!disabled);
      if (IsPlainTextAutoCommitField()) dirty_ = true;
      EmitState(last_revision_, last_present_, "error", "stage_failed", last_commit_request_);
    }
  }

  void FinishStage(
      uint64_t generation,
      bool disabled,
      std::optional<YoungRouter::CoreIPCBridge::SecretStageResult> result) noexcept {
    if (!Current(generation)) return;
    lifecycle_->staging.store(false, std::memory_order_release);
    SetInputEnabled(!disabled);
    if (!result || result->revision < 0 || result->revision > std::numeric_limits<int32_t>::max() ||
        std::floor(result->revision) != result->revision) {
      EmitState(last_revision_, last_present_, "error", "stage_failed", last_commit_request_);
      if (IsPlainTextAutoCommitField()) dirty_ = true;
      return;
    }
    EmitState(static_cast<int32_t>(result->revision), result->present, "saved", "", last_commit_request_);
  }

  void LoadPlainTextSecret() noexcept {
    if (!IsPlainTextAutoCommitField() || !lifecycle_ || !dispatcher_ || !Props()) return;
    const auto generation = lifecycle_->generation.load(std::memory_order_acquire);
    const bool disabled = Props()->disabled.value_or(false);
    const auto domain = active_domain_;
    const auto field = active_field_;
    const auto target = active_target_.empty() ? std::nullopt : std::optional<std::string>{active_target_};
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    loading_ = true;
    SetInputEnabled(false);
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, disabled, domain, field, target] {
        auto value = YoungRouter::CoreIPCBridge::Shared().ReadPlainTextSecret(domain, field, target);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, disabled, value = std::move(value)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) self->FinishPlainTextSecretLoad(generation, disabled, std::move(value));
        });
      }).detach();
    } catch (...) {
      loading_ = false;
      SetInputEnabled(!disabled);
      EmitState(last_revision_, last_present_, "error", "read_failed", last_commit_request_);
    }
  }

  void FinishPlainTextSecretLoad(
      uint64_t generation,
      bool disabled,
      std::optional<std::string> value) noexcept {
    if (!Current(generation) || !IsPlainTextAutoCommitField()) return;
    loading_ = false;
    auto display_value = value.value_or("");
    SetInput(ToHString(display_value));
    dirty_ = false;
    SetInputEnabled(!disabled);
    if (!value) {
      EmitState(last_revision_, false, "error", "read_failed", last_commit_request_);
      return;
    }
    EmitState(last_revision_, !value->empty(), "ready", "", last_commit_request_);
  }

  void EmitState(int32_t revision, bool present, std::string status, std::string error, int32_t commit_request) noexcept {
    last_revision_ = std::max(0, revision);
    last_present_ = present;
    last_status_ = status;
    last_error_ = error;
    last_commit_request_ = std::max(0, commit_request);
    try {
      if (auto emitter = EventEmitter()) {
        winrt::YoungRouter::Codegen::LiteLLMWinUISecureTextInputEventEmitter::OnSecretState args;
        args.revision = last_revision_;
        args.present = last_present_;
        args.status = std::move(status);
        args.error = std::move(error);
        args.commitRequest = last_commit_request_;
        emitter->onSecretState(std::move(args));
      }
    } catch (...) {
    }
  }

  bool syncing_ = false;
  bool dirty_ = false;
  bool loading_ = false;
  std::shared_ptr<SecureInputLifecycle> lifecycle_{std::make_shared<SecureInputLifecycle>()};
  winrt::Microsoft::UI::Dispatching::DispatcherQueue dispatcher_{nullptr};
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  PasswordBox password_box_{nullptr};
  TextBox multiline_box_{nullptr};
  std::string active_domain_;
  std::string active_field_;
  std::string active_target_;
  int32_t last_revision_ = 0;
  bool last_present_ = false;
  std::string last_status_;
  std::string last_error_;
  int32_t last_commit_request_ = 0;
  int32_t next_auto_commit_request_ = 0;
  int32_t last_reset_request_ = 0;
  winrt::event_token password_changed_token_{};
  winrt::event_token lost_focus_token_{};
  winrt::event_token key_down_token_{};
  winrt::event_token text_changed_token_{};
  winrt::event_token multiline_lost_focus_token_{};
};

template <typename TComponent>
void RegisterComponent(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder,
    void (*register_component)(
        winrt::Microsoft::ReactNative::IReactPackageBuilder const&,
        std::function<void(winrt::Microsoft::ReactNative::Composition::IReactCompositionViewComponentBuilder const&)>)) noexcept {
  register_component(package_builder, [](winrt::Microsoft::ReactNative::Composition::IReactCompositionViewComponentBuilder const& builder) {
    builder.SetContentIslandComponentViewInitializer([](ContentIslandComponentView const& island_view) noexcept {
      YoungRouter::ConfigureImmediateXamlPresentation();
      auto user_data = winrt::make_self<TComponent>();
      user_data->InitializeContentIsland(island_view);
      island_view.UserData(*user_data);
    });
  });
}

void RegisterCodeWebView(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder) noexcept {
  winrt::YoungRouter::Codegen::RegisterLiteLLMWinUICodeWebViewNativeComponent<CodeWebViewComponentView>(
      package_builder,
      [](winrt::Microsoft::ReactNative::Composition::IReactCompositionViewComponentBuilder const& builder) {
        builder.SetContentIslandComponentViewInitializer(
            [](ContentIslandComponentView const& island_view) noexcept {
              YoungRouter::ConfigureImmediateXamlPresentation();
              auto user_data = winrt::make_self<CodeWebViewComponentView>();
              user_data->InitializeContentIsland(island_view);
              RegisterCodeWebViewRecycleHandler(island_view, user_data);
              island_view.UserData(*user_data);
            });
      });
}

}  // namespace

namespace YoungRouter {

void ConfigureImmediateXamlPresentation() noexcept {
  try {
    auto application = winrt::Microsoft::UI::Xaml::Application::Current();
    if (!application) return;
    auto resources = application.Resources();
    const auto zero = winrt::box_value(
        winrt::Microsoft::UI::Xaml::DurationHelper::FromTimeSpan(
            winrt::Windows::Foundation::TimeSpan{0}));
    resources.Insert(winrt::box_value(L"ControlNormalAnimationDuration"), zero);
    resources.Insert(winrt::box_value(L"ControlFastAnimationDuration"), zero);
    resources.Insert(winrt::box_value(L"ControlFastAnimationAfterDuration"), zero);
    resources.Insert(winrt::box_value(L"ControlFasterAnimationDuration"), zero);
  } catch (...) {
  }
}

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder) noexcept {
  RegisterCodeWebView(package_builder);
  RegisterComponent<SecureTextInputComponentView>(
      package_builder,
      winrt::YoungRouter::Codegen::RegisterLiteLLMWinUISecureTextInputNativeComponent<SecureTextInputComponentView>);
}

}  // namespace YoungRouter

#else

namespace YoungRouter {

void ConfigureImmediateXamlPresentation() noexcept {}

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const&) noexcept {}

}  // namespace YoungRouter

#endif  // defined(RNW_NEW_ARCH)
