#include "pch.h"
#include "WinUI3NativeLeafModule.h"
#include "CoreIPCBridge.h"
#include "WindowsRelayLogin.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <set>
#include <shobjidl_core.h>
#include <winrt/Windows.Globalization.h>
#include <winrt/Windows.ApplicationModel.DataTransfer.h>
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

std::vector<YoungRouter::NativeMenuAction> NativeActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) {
  std::vector<YoungRouter::NativeMenuAction> result;
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

std::optional<std::wstring> ShowPicker(HWND owner) {
  winrt::com_ptr<IFileDialog> dialog;
  HRESULT created = CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER,
      IID_PPV_ARGS(dialog.put()));
  if (FAILED(created)) return std::nullopt;
  DWORD options = 0;
  if (SUCCEEDED(dialog->GetOptions(&options))) dialog->SetOptions(options | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
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

std::optional<std::wstring> ShowSavePicker(
    HWND owner,
    std::wstring const& suggested_name,
    std::wstring const& json_filter,
    std::wstring const& all_filter) {
  if (suggested_name.empty() || suggested_name.size() > 255 ||
      suggested_name.find_first_of(L"\\/:*?\"<>|") != std::wstring::npos) return std::nullopt;
  winrt::com_ptr<IFileDialog> dialog;
  HRESULT created = CoCreateInstance(CLSID_FileSaveDialog, nullptr, CLSCTX_INPROC_SERVER,
      IID_PPV_ARGS(dialog.put()));
  if (FAILED(created)) return std::nullopt;
  DWORD options = 0;
  if (SUCCEEDED(dialog->GetOptions(&options))) dialog->SetOptions(options | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_OVERWRITEPROMPT);
  dialog->SetFileName(suggested_name.c_str());
  COMDLG_FILTERSPEC filters[] = {{json_filter.c_str(), L"*.json"}, {all_filter.c_str(), L"*.*"}};
  dialog->SetFileTypes(2, filters);
  dialog->SetDefaultExtension(L"json");
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

namespace YoungRouter {

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
    std::wstring const& route,
    double width,
    double height,
    winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept {
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, route, width, height, promise] {
      promise.Resolve(leaf->SetWindowContentSize(route, width, height));
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
        auto selected = ShowPicker(owner);
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
    std::wstring const& suggested_name,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::string>> const& promise) noexcept {
  try {
    auto owner = HostWindow(context_);
    auto js_dispatcher = context_.JSDispatcher();
    auto leaf = leaf_;
    context_.UIDispatcher().Post([owner, suggested_name, promise, js_dispatcher, leaf] {
      try {
        auto selected = ShowSavePicker(
            owner,
            suggested_name,
            leaf->Localized("fileFilterJson", L"JSON files"),
            leaf->Localized("fileFilterAll", L"All files"));
        if (!selected) {
          js_dispatcher.Post([promise] { promise.Resolve(std::nullopt); });
          return;
        }
        std::thread([selected = std::move(*selected), promise, js_dispatcher] {
          auto token = CoreIPCBridge::Shared().RegisterFileCapability(selected, "export");
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
    std::wstring const& cancel_label,
    bool destructive,
    winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept {
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([leaf, title, message, confirm_label, cancel_label, destructive, promise, js_dispatcher] {
      bool accepted = false;
      try {
        accepted = leaf->Confirm(title, message, confirm_label, cancel_label, destructive);
      } catch (...) {
      }
      js_dispatcher.Post([promise, accepted] { promise.Resolve(accepted); });
    });
  } catch (...) {
    promise.Resolve(false);
  }
}

void WinUI3NativeLeafModule::ShowReadOnlyText(
    std::wstring const& title,
    std::wstring const& text,
    std::wstring const& close_label,
    std::wstring const& language,
    std::wstring const& html,
    winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept {
  if (html.empty() || html.size() > 4 * 1024 * 1024 ||
      text.size() > 2 * 1024 * 1024 ||
      (language != L"json" && language != L"toml" && language != L"text")) {
    promise.Reject("The read-only text viewer input is invalid.");
    return;
  }
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    context_.UIDispatcher().Post([
        leaf,
        title,
        text,
        close_label,
        language,
        html,
        promise,
        js_dispatcher] {
      try {
        leaf->ShowReadOnlyText(title, text, close_label, language, html);
      } catch (...) {
        js_dispatcher.Post([promise] { promise.Reject("The read-only text viewer could not be opened."); });
        return;
      }
      js_dispatcher.Post([promise] { promise.Resolve(); });
    });
  } catch (...) {
    promise.Reject("The read-only text viewer could not be opened.");
  }
}

void WinUI3NativeLeafModule::ShowActionMenu(
    std::wstring const& title,
    std::vector<std::wstring> const& items,
    winrt::Microsoft::ReactNative::JSValueObject const& anchor,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<double>> const& promise) noexcept {
  auto number = [&anchor](char const* key) -> std::optional<double> {
    auto found = anchor.find(key);
    if (found == anchor.end()) return std::nullopt;
    if (auto value = found->second.TryGetDouble()) return *value;
    if (auto value = found->second.TryGetInt64()) return static_cast<double>(*value);
    return std::nullopt;
  };
  auto x = number("x");
  auto y = number("y");
  auto width = number("width");
  auto height = number("height");
  if (!x || !y || !width || !height || !std::isfinite(*x) || !std::isfinite(*y) ||
      !std::isfinite(*width) || !std::isfinite(*height) || *x < 0 || *y < 0 ||
      *width <= 0 || *height <= 0 || *width > 8192 || *height > 8192) {
    promise.Resolve(std::nullopt);
    return;
  }
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    YoungRouter::NativeMenuAnchor menu_anchor{*x, *y, *width, *height};
    context_.UIDispatcher().Post([leaf, title, items, menu_anchor, promise, js_dispatcher] {
      auto selected = leaf->ShowActionMenu(title, items, menu_anchor);
      js_dispatcher.Post([promise, selected] {
        promise.Resolve(selected ? std::optional<double>(static_cast<double>(*selected)) : std::nullopt);
      });
    });
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::ShowGroupedActionMenu(
    std::wstring const& title,
    winrt::Microsoft::ReactNative::JSValueArray const& groups,
    winrt::Microsoft::ReactNative::JSValueObject const& anchor,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  auto number = [&anchor](char const* key) -> std::optional<double> {
    auto found = anchor.find(key);
    if (found == anchor.end()) return std::nullopt;
    if (auto value = found->second.TryGetDouble()) return *value;
    if (auto value = found->second.TryGetInt64()) return static_cast<double>(*value);
    return std::nullopt;
  };
  auto x = number("x");
  auto y = number("y");
  auto width = number("width");
  auto height = number("height");
  if (!x || !y || !width || !height || !std::isfinite(*x) || !std::isfinite(*y) ||
      !std::isfinite(*width) || !std::isfinite(*height) || *x < 0 || *y < 0 ||
      *width <= 0 || *height <= 0 || *width > 8192 || *height > 8192 ||
      groups.empty() || groups.size() > 32) {
    promise.Resolve(std::nullopt);
    return;
  }
  std::vector<NativeMenuGroup> native_groups;
  native_groups.reserve(groups.size());
  for (auto const& entry : groups) {
    auto const* group = entry.TryGetObject();
    if (group == nullptr) {
      promise.Resolve(std::nullopt);
      return;
    }
    auto title_found = group->find("title");
    auto items_found = group->find("items");
    if (title_found == group->end() || items_found == group->end()) {
      promise.Resolve(std::nullopt);
      return;
    }
    auto group_title = title_found->second.TryGetString();
    if (!group_title || group_title->empty() || group_title->size() > 240) {
      promise.Resolve(std::nullopt);
      return;
    }
    auto items_array = items_found->second.TryGetArray();
    if (items_array == nullptr || items_array->empty() || items_array->size() > 64) {
      promise.Resolve(std::nullopt);
      return;
    }
    NativeMenuGroup native_group;
    native_group.title = winrt::hstring(*group_title).c_str();
    native_group.items.reserve(items_array->size());
    for (auto const& item : *items_array) {
      auto item_text = item.TryGetString();
      if (!item_text || item_text->empty() || item_text->size() > 240) {
        promise.Resolve(std::nullopt);
        return;
      }
      native_group.items.push_back(winrt::hstring(*item_text).c_str());
    }
    native_groups.push_back(std::move(native_group));
  }
  try {
    auto leaf = leaf_;
    auto js_dispatcher = context_.JSDispatcher();
    YoungRouter::NativeMenuAnchor menu_anchor{*x, *y, *width, *height};
    context_.UIDispatcher().Post([leaf, title, groups = std::move(native_groups), menu_anchor, promise, js_dispatcher]() mutable {
      std::optional<std::pair<size_t, size_t>> selected;
      try {
        selected = leaf->ShowGroupedActionMenu(title, groups, menu_anchor);
      } catch (...) {
      }
      js_dispatcher.Post([promise, selected] {
        if (!selected) {
          promise.Resolve(std::nullopt);
          return;
        }
        winrt::Microsoft::ReactNative::JSValueObject result;
        result["group"] = static_cast<double>(selected->first);
        result["item"] = static_cast<double>(selected->second);
        promise.Resolve(result);
      });
    });
  } catch (...) {
    promise.Resolve(std::nullopt);
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

void WinUI3NativeLeafModule::ShowGroupManager(
    winrt::Microsoft::ReactNative::JSValueObject const& options,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  auto string_field = [&options](char const* name) -> std::optional<std::string> {
    auto found = options.find(name);
    if (found == options.end()) return std::nullopt;
    auto value = found->second.TryGetString();
    return value ? std::optional<std::string>(*value) : std::nullopt;
  };
  auto title = string_field("title");
  auto account_label = string_field("accountLabel");
  auto account_id = string_field("accountId");
  auto grouping_entry = options.find("autoGrouping");
  std::optional<bool> auto_grouping;
  if (grouping_entry != options.end()) {
    if (auto const* checked = grouping_entry->second.TryGetBoolean()) auto_grouping = *checked;
  }
  bool valid = true;
  std::vector<YoungRouter::GroupManagerGroup> groups;
  auto groups_entry = options.find("groups");
  if (groups_entry == options.end()) {
    valid = false;
  } else {
    auto const* array = groups_entry->second.TryGetArray();
    if (array == nullptr || array->size() > 512) {
      valid = false;
    } else {
      for (auto const& item : *array) {
        auto const* object = item.TryGetObject();
        if (!object) {
          valid = false;
          break;
        }
        std::optional<std::string> id;
        std::optional<std::string> label;
        std::optional<std::string> name;
        std::optional<std::string> rate;
        for (auto const& [key, value] : *object) {
          if (key == "id") {
            if (auto const* text = value.TryGetString()) id = *text;
          } else if (key == "label") {
            if (auto const* text = value.TryGetString()) label = *text;
          } else if (key == "name") {
            if (auto const* text = value.TryGetString()) name = *text;
          } else if (key == "rate") {
            if (auto const* text = value.TryGetString()) rate = *text;
          }
        }
        if (!id || id->size() > 256 || !label || label->empty() || label->size() > 256
            || (name && name->size() > 256) || (rate && rate->size() > 64)) {
          valid = false;
          break;
        }
        std::string column_name = name && !name->empty() ? *name : *label;
        groups.push_back(YoungRouter::GroupManagerGroup{
            Utf8ToWide(*id), Utf8ToWide(*label), Utf8ToWide(column_name), Utf8ToWide(rate.value_or(""))});
      }
    }
  }
  std::vector<YoungRouter::GroupManagerKey> keys;
  auto keys_entry = options.find("keys");
  if (!valid || keys_entry == options.end()) {
    valid = false;
  } else {
    auto const* array = keys_entry->second.TryGetArray();
    if (array == nullptr || array->size() > 512) {
      valid = false;
    } else {
      for (auto const& item : *array) {
        auto const* object = item.TryGetObject();
        if (!object) {
          valid = false;
          break;
        }
        std::optional<std::string> id;
        std::optional<std::string> name;
        std::optional<std::string> group_id;
        std::optional<std::string> group_label;
        std::optional<std::string> multiplier;
        std::optional<std::string> hint;
        std::optional<std::string> models;
        for (auto const& [key, value] : *object) {
          // The key's models travel as a list so the sheet can lay them out in
          // aligned columns; they are joined into the newline-separated text
          // the sheet's own API carries.
          if (key == "models") {
            auto const* array = value.TryGetArray();
            if (array != nullptr && array->size() <= 512) {
              std::string joined;
              for (auto const& entry : *array) {
                auto const* model = entry.TryGetString();
                if (model == nullptr || model->empty() || model->size() > 256) {
                  joined.clear();
                  break;
                }
                if (!joined.empty()) joined.push_back('\n');
                joined.append(*model);
                if (joined.size() > 16384) {
                  joined.clear();
                  break;
                }
              }
              models = joined;
            }
            continue;
          }
          auto const* text = value.TryGetString();
          if (!text) continue;
          if (key == "id") id = *text;
          else if (key == "name") name = *text;
          else if (key == "groupID") group_id = *text;
          else if (key == "groupLabel") group_label = *text;
          else if (key == "multiplier") multiplier = *text;
          else if (key == "hint") hint = *text;
        }
        if (!id || id->size() > 256 || !name || name->empty() || name->size() > 256
            || (hint && hint->size() > 256) || (models && models->size() > 16384)) {
          valid = false;
          break;
        }
        keys.push_back(YoungRouter::GroupManagerKey{
            Utf8ToWide(*id),
            Utf8ToWide(*name),
            Utf8ToWide(group_id.value_or("")),
            Utf8ToWide(group_label.value_or("")),
            Utf8ToWide(multiplier.value_or("")),
            Utf8ToWide(hint.value_or("")),
            Utf8ToWide(models.value_or(""))});
      }
    }
  }
  YoungRouter::GroupManagerLabels labels;
  auto labels_entry = options.find("labels");
  if (!valid || labels_entry == options.end()) {
    valid = false;
  } else {
    auto const* object = labels_entry->second.TryGetObject();
    if (!object) {
      valid = false;
    } else {
      auto read = [object](char const* name) -> std::wstring {
        auto found = object->find(name);
        if (found == object->end()) return std::wstring{};
        auto const* text = found->second.TryGetString();
        return text ? Utf8ToWide(*text) : std::wstring{};
      };
      labels.list_label = read("listLabel");
      labels.add_label = read("addLabel");
      labels.remove_label = read("removeLabel");
      labels.name_label = read("nameLabel");
      labels.group_label = read("groupLabel");
      labels.multiplier_label = read("multiplierLabel");
      labels.value_label = read("valueLabel");
      labels.copy_label = read("copyLabel");
      labels.failed_label = read("failedLabel");
      labels.models_label = read("modelsLabel");
      labels.empty_label = read("emptyLabel");
      labels.enabled_label = read("enabledLabel");
      labels.new_key_name = read("newKeyName");
      labels.auto_grouping_label = read("autoGroupingLabel");
      labels.auto_grouping_help = read("autoGroupingHelp");
      labels.ungrouped_label = read("ungroupedLabel");
      labels.close_label = read("closeLabel");
      labels.apply_label = read("applyLabel");
      labels.discard_title = read("discardTitle");
      labels.discard_body = read("discardBody");
      labels.discard_confirm = read("discardConfirm");
      if (labels.add_label.empty() || labels.remove_label.empty() || labels.close_label.empty() || labels.apply_label.empty()) valid = false;
    }
  }
  if (!title || title->empty() || title->size() > 320 ||
      !account_label || account_label->size() > 320 ||
      !account_id || account_id->empty() || account_id->size() > 256 ||
      !auto_grouping || !valid) {
    promise.Reject("The group manager input is invalid.");
    return;
  }
  try {
    auto ui_dispatcher = context_.UIDispatcher();
    auto js_dispatcher = context_.JSDispatcher();
    auto leaf = leaf_;
    ui_dispatcher.Post([leaf,
                        title = Utf8ToWide(*title),
                        account_label = Utf8ToWide(*account_label),
                        account_id = Utf8ToWide(*account_id),
                        groups = std::move(groups),
                        keys = std::move(keys),
                        labels,
                        auto_grouping = *auto_grouping,
                        promise,
                        js_dispatcher]() mutable {
      std::optional<YoungRouter::GroupManagerResult> result;
      try {
        result = leaf->ShowGroupManager(std::move(title), std::move(account_label), std::move(account_id),
                                        std::move(groups), std::move(keys), labels, auto_grouping);
      } catch (...) {
      }
      js_dispatcher.Post([promise, result = std::move(result)]() mutable {
        if (!result) {
          promise.Resolve(std::nullopt);
          return;
        }
        winrt::Microsoft::ReactNative::JSValueObject value;
        value["autoGrouping"] = result->auto_grouping;
        winrt::Microsoft::ReactNative::JSValueArray creates;
        for (auto const& [name, group_id] : result->creates) {
          winrt::Microsoft::ReactNative::JSValueObject entry;
          entry["name"] = winrt::to_string(name);
          entry["groupID"] = winrt::to_string(group_id);
          creates.push_back(std::move(entry));
        }
        value["creates"] = std::move(creates);
        winrt::Microsoft::ReactNative::JSValueArray updates;
        for (auto const& update : result->updates) {
          winrt::Microsoft::ReactNative::JSValueObject entry;
          entry["keyID"] = winrt::to_string(update.key_id);
          entry["name"] = winrt::to_string(update.name);
          entry["groupID"] = winrt::to_string(update.group_id);
          entry["enabled"] = update.enabled;
          updates.push_back(std::move(entry));
        }
        value["updates"] = std::move(updates);
        winrt::Microsoft::ReactNative::JSValueArray deletes;
        for (auto const& key_id : result->deletes) deletes.push_back(winrt::to_string(key_id));
        value["deletes"] = std::move(deletes);
        promise.Resolve(std::optional<winrt::Microsoft::ReactNative::JSValueObject>(std::move(value)));
      });
    });
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

void WinUI3NativeLeafModule::CopySecret(
    std::string const& domain,
    std::string const& field,
    std::string const& target,
    winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept {
  if (domain.empty() || domain.size() > 64 || field.empty() || field.size() > 64 ||
      target.empty() || target.size() > 256) {
    promise.Resolve(false);
    return;
  }
  try {
    auto ui_dispatcher = context_.UIDispatcher();
    auto js_dispatcher = context_.JSDispatcher();
    std::thread([domain, field, target, promise, ui_dispatcher, js_dispatcher] {
      auto value = CoreIPCBridge::Shared().ReadPlainTextSecret(domain, field, target);
      if (!value) {
        js_dispatcher.Post([promise] { promise.Resolve(false); });
        return;
      }
      ui_dispatcher.Post([value = std::move(*value), promise, js_dispatcher]() mutable {
        bool copied = false;
        try {
          using namespace winrt::Windows::ApplicationModel::DataTransfer;
          DataPackage package;
          package.SetText(winrt::hstring{Utf8ToWide(value)});
          Clipboard::SetContent(package);
          Clipboard::Flush();
          copied = true;
        } catch (...) {
        }
        value.clear();
        js_dispatcher.Post([promise, copied] { promise.Resolve(copied); });
      });
    }).detach();
  } catch (...) {
    promise.Resolve(false);
  }
}

void WinUI3NativeLeafModule::RelayLogin(
    winrt::Microsoft::ReactNative::JSValueObject const& options,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  static std::atomic_bool login_active{false};
  auto field = [&options](char const* name) -> std::optional<std::string> {
    auto found = options.find(name);
    if (found == options.end()) return std::nullopt;
    auto value = found->second.TryGetString();
    return value ? std::optional<std::string>(*value) : std::nullopt;
  };
  auto account_id = field("accountId");
  auto account_type = field("type");
  auto label = field("label");
  auto origin = field("origin");
  auto language = field("language");
  auto username = field("username");
  auto pending_entry = options.find("pendingAccount");
  std::optional<bool> pending_account;
  if (pending_entry != options.end()) pending_account = pending_entry->second.TryGetBoolean();
  // "embedded" is a macOS sheet hint; Windows always opens its own login
  // window, so the flag is accepted and ignored here.
  static std::set<std::string> const allowed{"accountId", "type", "label", "origin", "language", "username", "embedded", "pendingAccount", "stationId", "stationName", "stationType", "stationOrigin"};
  auto station_id = field("stationId");
  auto station_name = field("stationName");
  auto station_type = field("stationType");
  auto station_origin = field("stationOrigin");
  auto const ui_language = language.value_or("system");
  if (options.size() > allowed.size() ||
      std::any_of(options.begin(), options.end(), [&allowed](auto const& entry) { return allowed.find(entry.first) == allowed.end(); }) ||
      !account_id || !account_type || !label || !origin ||
      (options.find("language") != options.end() && !language) ||
      (pending_account && !pending_account.value()) ||
      (station_type && station_type->empty()) ||
      (station_type && *station_type != "newapi" && *station_type != "sub2api") ||
      // "" (or "auto") lets the flow settle the family from the page's own
      // answer, so the sign-in window opens immediately instead of waiting on
      // two network probes first.
      (!IsRelayAccountType(*account_type)) ||
      account_id->empty() || account_id->size() > 96 || label->empty() || label->size() > 160 ||
      origin->empty() || origin->size() > 2048 ||
      (ui_language != "system" && ui_language != "en" && ui_language != "zh-Hans") ||
      (username && username->size() > 320)) {
    promise.Reject("The relay account is invalid.");
    return;
  }
  bool expected = false;
  if (!login_active.compare_exchange_strong(expected, true)) {
    promise.Reject("A relay account sign-in is already open.");
    return;
  }
  try {
  auto owner = HostWindow(context_);
  auto js_dispatcher = context_.JSDispatcher();
  WindowsRelayLoginOptions native_options{
        *account_id, *account_type, *label, *origin, username};
  native_options.language = ui_language;
  if (pending_account) native_options.pending_account = pending_account.value();
  if (station_id) native_options.station_id = station_id;
  if (station_name) native_options.station_name = station_name;
  if (station_type) native_options.station_type = station_type;
  if (station_origin) native_options.station_origin = station_origin;
  // One decision surface for the whole app: the sign-in browser asks its
  // post-login question through the host's own window instead of a dialog of
  // its own.
  native_options.decide = [leaf = leaf_](
                               std::wstring const& title,
                               std::wstring const& message,
                               std::wstring const& primary_label,
                               std::wstring const& secondary_label) {
    return leaf->DecideChoice(title, message, primary_label, secondary_label);
  };
    context_.UIDispatcher().Post([
        owner, native_options = std::move(native_options), promise, js_dispatcher]() mutable {
      std::optional<WindowsRelayLoginResult> result;
      try {
        result = RunWindowsRelayLogin(owner, native_options);
      } catch (...) {
      }
      login_active.store(false);
      js_dispatcher.Post([promise, result = std::move(result)]() mutable {
        if (!result) {
          promise.Resolve(std::nullopt);
          return;
        }
        winrt::Microsoft::ReactNative::JSValueObject value;
        value["revision"] = result->revision;
        value["loginStatus"] = "signed_in";
        value["username"] = std::move(result->username);
        promise.Resolve(std::optional<winrt::Microsoft::ReactNative::JSValueObject>(std::move(value)));
      });
    });
  } catch (...) {
    login_active.store(false);
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::RestoreRelaySession(
    winrt::Microsoft::ReactNative::JSValueObject const& options,
    winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept {
  auto field = [&options](char const* name) -> std::optional<std::string> {
    auto found = options.find(name);
    if (found == options.end()) return std::nullopt;
    auto value = found->second.TryGetString();
    return value ? std::optional<std::string>(*value) : std::nullopt;
  };
  auto account_id = field("accountId");
  auto account_type = field("type");
  auto label = field("label");
  auto origin = field("origin");
  auto username = field("username");
  static std::set<std::string> const allowed{"accountId", "type", "label", "origin", "username"};
  if (
      std::any_of(options.begin(), options.end(), [&allowed](auto const& entry) { return allowed.find(entry.first) == allowed.end(); }) ||
      !account_id || !account_type || !label || !origin ||
      account_id->empty() || account_id->size() > 96 || label->empty() || label->size() > 160 ||
      origin->empty() || origin->size() > 2048 || (username && username->size() > 320) ||
      (options.find("username") != options.end() && !username)) {
    promise.Reject("The relay account is invalid.");
    return;
  }
  try {
    auto js_dispatcher = context_.JSDispatcher();
    WindowsRelayLoginOptions native_options{
        *account_id, *account_type, *label, *origin, username, false};
    std::thread([native_options = std::move(native_options), promise, js_dispatcher] () mutable {
      std::optional<WindowsRelaySessionRestoreResult> result;
      try {
        result = RestoreWindowsRelaySession(native_options);
      } catch (...) {
      }
      js_dispatcher.Post([promise, result = std::move(result)]() mutable {
        if (!result) {
          promise.Resolve(std::nullopt);
          return;
        }
        winrt::Microsoft::ReactNative::JSValueObject value;
        value["revision"] = result->revision;
        value["loginStatus"] = std::move(result->login_status);
        value["username"] = std::move(result->username);
        promise.Resolve(std::optional<winrt::Microsoft::ReactNative::JSValueObject>(std::move(value)));
      });
    }).detach();
  } catch (...) {
    promise.Resolve(std::nullopt);
  }
}

void WinUI3NativeLeafModule::ClearRelayCredentials(
    std::string const& account_id,
    winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept {
  try {
    auto js_dispatcher = context_.JSDispatcher();
    std::thread([account_id, promise, js_dispatcher] {
      auto removed = ClearWindowsRelayCredentials(account_id);
      js_dispatcher.Post([promise, removed] {
        if (removed) promise.Resolve();
        else promise.Reject("The relay credentials could not be removed.");
      });
    }).detach();
  } catch (...) {
    promise.Reject("The relay credentials could not be removed.");
  }
}

void WinUI3NativeLeafModule::ClearRelayPassword(
    std::string const& account_id,
    winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept {
  try {
    auto js_dispatcher = context_.JSDispatcher();
    std::thread([account_id, promise, js_dispatcher] {
      auto removed = ClearWindowsRelayPassword(account_id);
      js_dispatcher.Post([promise, removed] {
        if (removed) promise.Resolve();
        else promise.Reject("The remembered relay password could not be removed.");
      });
    }).detach();
  } catch (...) {
    promise.Reject("The remembered relay password could not be removed.");
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

void WinUI3NativeLeafModule::VersionInfo(
    winrt::Microsoft::ReactNative::ReactPromise<winrt::Microsoft::ReactNative::JSValueObject> const& promise) noexcept {
  auto leaf = leaf_;
  try {
    context_.UIDispatcher().Post([leaf, promise] {
      auto info = leaf->VersionInfo();
      winrt::Microsoft::ReactNative::JSValueObject result;
      result["app"] = winrt::to_string(info.app);
      result["litellm"] = winrt::to_string(info.litellm);
      promise.Resolve(result);
    });
  } catch (...) {
    winrt::Microsoft::ReactNative::JSValueObject result;
    result["app"] = std::string{};
    result["litellm"] = std::string{};
    promise.Resolve(result);
  }
}

void WinUI3NativeLeafModule::OpenExternalURL(std::wstring const& url) noexcept {
  if (url.empty() || url.size() > 8192) return;
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, url] { leaf->OpenExternalURL(url); });
  } catch (...) {
  }
}

void WinUI3NativeLeafModule::RevealFile(std::wstring const& path) noexcept {
  if (path.empty() || path.size() > 4096) return;
  try {
    auto leaf = leaf_;
    context_.UIDispatcher().Post([leaf, path] { leaf->RevealFile(path); });
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
