#include "pch.h"
#include "WinUI3NativeLeafModule.h"
#include "CoreIPCBridge.h"

#include <algorithm>
#include <shobjidl_core.h>
#include <winrt/Windows.Globalization.h>
#include <ReactCoreInjection.h>

namespace {
std::wstring Utf8ToWide(std::string const& value) {
  if (value.empty()) return {};
  int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
  if (count <= 0) return {};
  std::wstring result(static_cast<size_t>(count), L'\0');
  MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), count);
  return result;
}

std::optional<std::string> WideToUtf8(std::wstring const& value) {
  if (value.empty()) return std::string{};
  int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  if (count <= 0) return std::nullopt;
  std::string result(static_cast<size_t>(count), '\0');
  WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), count, nullptr, nullptr);
  return result;
}

HWND HostWindow(winrt::Microsoft::ReactNative::ReactContext const& context) {
  auto value = winrt::Microsoft::ReactNative::implementation::ReactCoreInjection::GetTopLevelWindowId(context.Properties().Handle());
  return reinterpret_cast<HWND>(value);
}

std::vector<LiteLLMMenu::NativeMenuAction> NativeActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) {
  std::vector<LiteLLMMenu::NativeMenuAction> result;
  for (auto const& action : actions) {
    auto object = action.TryGetObject();
    if (!object) continue;
    auto found = object->find("id");
    if (found == object->end() || !found->second.TryGetString()) continue;
    auto id = Utf8ToWide(*found->second.TryGetString());
    auto title_entry = object->find("title");
    auto enabled_entry = object->find("enabled");
    auto checked_entry = object->find("checked");
    auto title = title_entry != object->end() && title_entry->second.TryGetString()
        ? Utf8ToWide(*title_entry->second.TryGetString()) : id;
    bool enabled = enabled_entry == object->end() || !enabled_entry->second.TryGetBoolean() || *enabled_entry->second.TryGetBoolean();
    bool checked = checked_entry != object->end() && checked_entry->second.TryGetBoolean() && *checked_entry->second.TryGetBoolean();
    if (!id.empty()) result.push_back({std::move(id), std::move(title), enabled, checked});
  }
  return result;
}

std::map<std::string, std::wstring> NativeStrings(
    winrt::Microsoft::ReactNative::JSValueObject const& strings) {
  std::map<std::string, std::wstring> result;
  for (auto const& [key, value] : strings) {
    if (auto text = value.TryGetString(); text && !text->empty()) {
      result.emplace(key, Utf8ToWide(*text));
    }
  }
  return result;
}

std::optional<std::wstring> ShowPicker(HWND owner, bool save) {
  winrt::com_ptr<IFileDialog> dialog;
  HRESULT created = CoCreateInstance(save ? CLSID_FileSaveDialog : CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER,
      IID_PPV_ARGS(dialog.put()));
  if (FAILED(created)) return std::nullopt;
  DWORD options = 0;
  if (SUCCEEDED(dialog->GetOptions(&options))) dialog->SetOptions(options | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
  if (save) dialog->SetFileName(L"litellm-menu-config.json");
  HRESULT shown = dialog->Show(owner);
  if (shown == HRESULT_FROM_WIN32(ERROR_CANCELLED) || FAILED(shown)) return std::nullopt;
  IShellItem* item = nullptr;
  if (FAILED(dialog->GetResult(&item)) || item == nullptr) return std::nullopt;
  PWSTR path = nullptr;
  HRESULT name_result = item->GetDisplayName(SIGDN_FILESYSPATH, &path);
  item->Release();
  if (FAILED(name_result) || path == nullptr) return std::nullopt;
  std::wstring result(path);
  CoTaskMemFree(path);
  return result.empty() ? std::nullopt : std::optional<std::wstring>(std::move(result));
}
}  // namespace

namespace LiteLLMMenu {

void WinUI3NativeLeafModule::Initialize(winrt::Microsoft::ReactNative::ReactContext const& context) noexcept {
  try {
    context_ = context;
    auto leaf = leaf_;
    auto emitter = MenuAction;
    auto js_dispatcher = context_.JSDispatcher();
    leaf->SetActionHandler([emitter, js_dispatcher](std::string const& action) {
      js_dispatcher.Post([emitter, action] { emitter(action); });
    });
    auto owner = HostWindow(context_);
    context_.UIDispatcher().Post([leaf, owner] { leaf->Initialize(owner); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::OpenWindow(std::wstring const& route) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, route] { leaf->OpenRoute(route); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::CloseWindow(std::optional<std::wstring> const& route) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, route] {
      if (route && !route->empty()) leaf->CloseRoute(*route);
    });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::FocusWindow(std::wstring const& route) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, route] { leaf->OpenRoute(route); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::SetWindowContentSize(
    double width,
    double height,
    winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, width, height, promise] {
      promise.Resolve(leaf->SetWindowContentSize(width, height));
    });
  } catch (...) {
    promise.Resolve(false);
  }
}

void WinUI3NativeLeafModule::SetMenuBarStatus(std::wstring const& title, bool running) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, title, running] { leaf->SetStatus(title, running); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::SetMenuBarActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) noexcept {
  try {
    auto leaf = leaf_;
    auto native_actions = NativeActions(actions);
    context_.UIDispatcher().Post([leaf, native_actions = std::move(native_actions)] {
      leaf->SetActions(native_actions);
    });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::SetTrayStatus(std::wstring const& title, bool running) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, title, running] { leaf->SetStatus(title, running); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::SetTrayActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) noexcept {
  try {
    auto leaf = leaf_;
    auto native_actions = NativeActions(actions);
    context_.UIDispatcher().Post([leaf, native_actions = std::move(native_actions)] {
      leaf->SetActions(native_actions);
    });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::OpenFilePicker(
    std::string const& purpose,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::string>> const& promise) noexcept {
  try {
    auto owner = HostWindow(context_);
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([owner, purpose, promise, js_dispatcher] {
      try {
        auto selected = ShowPicker(owner, false);
        if (!selected) {
          js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
          return;
        }
        std::thread([selected = std::move(*selected), purpose, promise, js_dispatcher] {
          auto token = CoreIPCBridge::Shared().RegisterFileCapability(selected, purpose);
          js_dispatcher.Post([promise, token] { promise.Resolve(token); });
        }).detach();
      } catch (...) {
        js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
      }
    });
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::SaveFilePicker(
    std::string const& purpose,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::string>> const& promise) noexcept {
  try {
    auto owner = HostWindow(context_);
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([owner, purpose, promise, js_dispatcher] {
      try {
        auto selected = ShowPicker(owner, true);
        if (!selected) {
          js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
          return;
        }
        std::thread([selected = std::move(*selected), purpose, promise, js_dispatcher] {
          auto token = CoreIPCBridge::Shared().RegisterFileCapability(selected, purpose);
          js_dispatcher.Post([promise, token] { promise.Resolve(token); });
        }).detach();
      } catch (...) {
        js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
      }
    });
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::ShowConfirmation(
    std::wstring const& title,
    std::wstring const& message,
    std::wstring const& confirm_label,
    winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept {
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([leaf, title, message, confirm_label, promise, js_dispatcher] {
      bool accepted = false;
      try {
        accepted = leaf->Confirm(title, message, confirm_label);
      } catch (...) {
      }
      js_dispatcher.Post([promise, accepted] { promise.Resolve(accepted); });
    });
  } catch (...) {
    promise.Resolve(false);
  }
}

void WinUI3NativeLeafModule::ChooseModelsToAdd(
    std::vector<std::string> const& models,
    std::wstring const& provider_name,
    std::wstring const& key_name,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::vector<std::string>>> const& promise) noexcept {
  if (models.size() > 10000 || provider_name.size() > 512 || key_name.size() > 512) {
    promise.Reject("The native model chooser input is invalid.");
    return;
  }
  std::vector<std::wstring> native_models;
  native_models.reserve(models.size());
  for (auto const& model : models) {
    if (model.empty() || model.size() > 256 ||
        std::any_of(model.begin(), model.end(), [](unsigned char character) { return character < 32; })) {
      promise.Reject("The native model chooser input is invalid.");
      return;
    }
    auto converted = Utf8ToWide(model);
    if (converted.empty()) {
      promise.Reject("The native model chooser input is invalid.");
      return;
    }
    native_models.push_back(std::move(converted));
  }
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([leaf, models = std::move(native_models), provider_name, key_name, promise, js_dispatcher]() mutable {
      std::optional<std::vector<std::wstring>> selected;
      try {
        selected = leaf->ChooseModelsToAdd(std::move(models), provider_name, key_name);
      } catch (...) {
      }
      js_dispatcher.Post([promise, selected = std::move(selected)]() mutable {
        if (!selected) {
          promise.Resolve(std::nullopt);
          return;
        }
        std::vector<std::string> result;
        result.reserve(selected->size());
        for (auto const& model : *selected) {
          auto converted = WideToUtf8(model);
          if (!converted) {
            promise.Reject("The native model chooser returned invalid text.");
            return;
          }
          result.push_back(std::move(*converted));
        }
        promise.Resolve(std::optional<std::vector<std::string>>(std::move(result)));
      });
    });
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::EditSecureDocument(
    std::string const& editor_token,
    std::string const& language,
    std::wstring const& title,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<double>> const& promise) noexcept {
  // RN supplies only an opaque Core-issued editor token. Raw settings text is
  // fetched, edited, and staged exclusively in this native/Core path.
  if (editor_token.empty() || editor_token.size() > 256) {
    promise.Reject("The native editor token is invalid.");
    return;
  }
  try {
    auto ui_dispatcher = context_.UIDispatcher();
    auto js_dispatcher = context_.JSDispatcher();
    auto leaf = leaf_;
    std::thread([leaf, editor_token, language, title, promise, ui_dispatcher, js_dispatcher] {
      auto text = CoreIPCBridge::Shared().ReadEditorDocument(editor_token);
      if (!text) {
        js_dispatcher.Post([promise] { promise.Reject("The local Core could not read the document."); });
        return;
      }
      ui_dispatcher.Post([
          leaf,
          editor_token,
          language,
          title,
          text = std::move(*text),
          promise,
          js_dispatcher]() mutable {
        std::optional<std::string> edited;
        try {
          edited = leaf->EditNativeText(text, language, title);
        } catch (...) {
        }
        if (!edited) {
          js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
          return;
        }
        try {
          std::thread([
              editor_token,
              edited = std::move(*edited),
              promise,
              js_dispatcher]() mutable {
            // Never place raw editor content in an event, promise, or diagnostic.
            auto revision = CoreIPCBridge::Shared().StageEditorDocument(editor_token, edited);
            js_dispatcher.Post([promise, revision] {
              if (revision) promise.Resolve(revision);
              else promise.Reject("The local Core could not stage the document.");
            });
          }).detach();
        } catch (...) {
          js_dispatcher.Post([promise] { promise.Reject("The local Core could not stage the document."); });
        }
      });
    }).detach();
  } catch (...) {
    promise.Reject("The local Core could not open the native editor.");
  }
}

void WinUI3NativeLeafModule::SetLocalization(
    winrt::Microsoft::ReactNative::JSValueObject const& strings) noexcept {
  try {
    auto leaf = leaf_;
    auto native_strings = NativeStrings(strings);
    context_.UIDispatcher().Post([leaf, native_strings = std::move(native_strings)]() mutable {
      leaf->SetLocalization(std::move(native_strings));
    });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::EditSecret(
    std::string const& domain,
    std::string const& field,
    std::optional<std::string> const& target,
    std::wstring const& title,
    bool allow_clear,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  if (domain.empty() || domain.size() > 64 || field.empty() || field.size() > 64 ||
      (target && target->size() > 256)) {
    promise.Resolve(std::nullopt);
    return;
  }
  try {
    auto ui_dispatcher = context_.UIDispatcher();
    auto js_dispatcher = context_.JSDispatcher();
    std::thread([domain, field, target, title, allow_clear, promise, ui_dispatcher, js_dispatcher, leaf = leaf_] {
      auto capability = CoreIPCBridge::Shared().CreateSecretCapability(domain, field, target, "settings");
      if (!capability) {
        js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
        return;
      }
      ui_dispatcher.Post([capability = std::move(*capability), title, allow_clear, promise, js_dispatcher, leaf] {
        try {
          auto edited = leaf->EditSecret(title, allow_clear, capability.present);
          if (!edited) {
            js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
            return;
          }
          std::thread([
              capability,
              value = std::move(edited->value),
              clear = edited->clear,
              promise,
              js_dispatcher]() mutable {
            std::optional<std::string> utf8_value;
            if (!clear) {
              utf8_value = WideToUtf8(value);
              if (!utf8_value) {
                js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
                return;
              }
            }
            auto staged = CoreIPCBridge::Shared().StageSecret(capability.token, utf8_value, clear);
            js_dispatcher.Post([promise, staged] {
              if (!staged) {
                promise.Resolve(std::nullopt);
                return;
              }
              winrt::Microsoft::ReactNative::JSValueObject result;
              result["revision"] = staged->revision;
              result["present"] = staged->present;
              promise.Resolve(std::move(result));
            });
          }).detach();
        } catch (...) {
          js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
        }
      });
    }).detach();
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::ClearSecret(
    std::string const& domain,
    std::string const& field,
    std::optional<std::string> const& target,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  if (domain.empty() || domain.size() > 64 || field.empty() || field.size() > 64 ||
      (target && target->size() > 256)) {
    promise.Resolve(std::nullopt);
    return;
  }
  try {
    auto js_dispatcher = context_.JSDispatcher();
    std::thread([domain, field, target, promise, js_dispatcher] {
      // The capability token and clear flag remain native-only.  React never
      // sends an empty string or receives secret material for this operation.
      auto capability = CoreIPCBridge::Shared().CreateSecretCapability(domain, field, target, "settings");
      if (!capability || !capability->present) {
        js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
        return;
      }
      auto staged = CoreIPCBridge::Shared().StageSecret(capability->token, std::nullopt, true);
      js_dispatcher.Post([promise, staged] {
        if (!staged) {
          promise.Resolve(std::nullopt);
          return;
        }
        winrt::Microsoft::ReactNative::JSValueObject result;
        result["revision"] = staged->revision;
        result["present"] = staged->present;
        promise.Resolve(std::move(result));
      });
    }).detach();
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

std::string WinUI3NativeLeafModule::SystemLocale() noexcept {
  try {
    auto languages = winrt::Windows::Globalization::ApplicationLanguages::Languages();
    return languages.Size() > 0 ? winrt::to_string(languages.GetAt(0)) : "en";
  } catch (...) {
    return "en";
  }
}

void WinUI3NativeLeafModule::SetLaunchAtLogin(
    bool enabled,
    winrt::Microsoft::ReactNative::ReactPromise<bool> promise) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, enabled, promise = std::move(promise)]() mutable {
      promise.Resolve(leaf->SetLaunchAtLogin(enabled));
    });
  } catch (...) {
    promise.Resolve(false);
  }
}

void WinUI3NativeLeafModule::ShowVersion() noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf] { leaf->ShowVersion(); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::Quit() noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf] { leaf->Quit(); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::SetShortcuts(winrt::Microsoft::ReactNative::JSValueObject const&) noexcept {
  // Standard Ctrl+Z/Ctrl+F are provided by the native edit controls. Window
  // route and reload shortcuts remain delivered by React Native's host menu.
}

}
