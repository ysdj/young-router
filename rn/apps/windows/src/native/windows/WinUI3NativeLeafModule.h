#pragma once

#include "WinUI3NativeLeaf.h"
#include <memory>
#include <NativeModules.h>

namespace LiteLLMMenu {

struct WinUI3NativeLeafModule {
  REACT_MODULE(WinUI3NativeLeafModule, L"LiteLLMNativeLeaf");
  REACT_INIT(Initialize);
  REACT_METHOD(OpenWindow, L"openWindow");
  REACT_METHOD(CloseWindow, L"closeWindow");
  REACT_METHOD(FocusWindow, L"focusWindow");
  REACT_METHOD(SetWindowContentSize, L"setWindowContentSize");
  REACT_METHOD(SetMenuBarStatus, L"setMenuBarStatus");
  REACT_METHOD(SetMenuBarActions, L"setMenuBarActions");
  REACT_METHOD(SetTrayStatus, L"setTrayStatus");
  REACT_METHOD(SetTrayActions, L"setTrayActions");
  REACT_METHOD(OpenFilePicker, L"openFilePicker");
  REACT_METHOD(SaveFilePicker, L"saveFilePicker");
  REACT_METHOD(ShowConfirmation, L"showConfirmation");
  REACT_METHOD(ShowReadOnlyText, L"showReadOnlyText");
  REACT_METHOD(ShowActionMenu, L"showActionMenu");
  REACT_METHOD(ChooseModelsToAdd, L"chooseModelsToAdd");
  REACT_METHOD(EditSecureDocument, L"editSecureDocument");
  REACT_METHOD(SetLocalization, L"setLocalization");
  REACT_METHOD(EditSecret, L"editSecret");
  REACT_METHOD(ClearSecret, L"clearSecret");
  REACT_METHOD(CopySecret, L"copySecret");
  REACT_METHOD(RelayLogin, L"relayLogin");
  REACT_METHOD(RestoreRelaySession, L"restoreRelaySession");
  REACT_METHOD(ClearRelayCredentials, L"clearRelayCredentials");
  REACT_METHOD(ClearRelayPassword, L"clearRelayPassword");
  REACT_SYNC_METHOD(SystemLocale, L"systemLocale");
  REACT_METHOD(SetLaunchAtLogin, L"setLaunchAtLogin");
  REACT_METHOD(ShowVersion, L"showVersion");
  REACT_METHOD(Quit, L"quit");
  REACT_METHOD(SetShortcuts, L"setShortcuts");
  REACT_EVENT(MenuAction, L"menuAction");

  void Initialize(winrt::Microsoft::ReactNative::ReactContext const& context) noexcept;
  void OpenWindow(std::wstring const& route) noexcept;
  void CloseWindow(std::optional<std::wstring> const& route) noexcept;
  void FocusWindow(std::wstring const& route) noexcept;
  void SetWindowContentSize(
      double width,
      double height,
      winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept;
  void SetMenuBarStatus(std::wstring const& title, bool running) noexcept;
  void SetMenuBarActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) noexcept;
  void SetTrayStatus(std::wstring const& title, bool running) noexcept;
  void SetTrayActions(winrt::Microsoft::ReactNative::JSValueArray const& actions) noexcept;
  void OpenFilePicker(std::string const& purpose, winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::string>> const& promise) noexcept;
  void SaveFilePicker(std::wstring const& suggested_name, winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::string>> const& promise) noexcept;
  void ShowConfirmation(
      std::wstring const& title,
      std::wstring const& message,
      std::wstring const& confirm_label,
      winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept;
  void ShowReadOnlyText(
      std::wstring const& title,
      std::wstring const& text,
      std::wstring const& close_label,
      winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept;
  void ShowActionMenu(
      std::wstring const& title,
      std::vector<std::wstring> const& items,
      winrt::Microsoft::ReactNative::JSValueObject const& anchor,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<double>> const& promise) noexcept;
  void ChooseModelsToAdd(
      std::vector<std::string> const& models,
      std::wstring const& provider_name,
      std::wstring const& key_name,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<std::vector<std::string>>> const& promise) noexcept;
  void EditSecureDocument(
      std::string const& editor_token,
      std::string const& language,
      std::wstring const& title,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<double>> const& promise) noexcept;
  void SetLocalization(winrt::Microsoft::ReactNative::JSValueObject const& strings) noexcept;
  void EditSecret(
      std::string const& domain,
      std::string const& field,
      std::optional<std::string> const& target,
      std::wstring const& title,
      bool allow_clear,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept;
  void ClearSecret(
      std::string const& domain,
      std::string const& field,
      std::optional<std::string> const& target,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept;
  void CopySecret(
      std::string const& domain,
      std::string const& field,
      std::string const& target,
      winrt::Microsoft::ReactNative::ReactPromise<bool> const& promise) noexcept;
  void RelayLogin(
      winrt::Microsoft::ReactNative::JSValueObject const& options,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept;
  void RestoreRelaySession(
      winrt::Microsoft::ReactNative::JSValueObject const& options,
      winrt::Microsoft::ReactNative::ReactPromise<std::optional<winrt::Microsoft::ReactNative::JSValueObject>> const& promise) noexcept;
  void ClearRelayCredentials(
      std::string const& account_id,
      winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept;
  void ClearRelayPassword(
      std::string const& account_id,
      winrt::Microsoft::ReactNative::ReactPromise<void> const& promise) noexcept;
  std::string SystemLocale() noexcept;
  void SetLaunchAtLogin(bool enabled, winrt::Microsoft::ReactNative::ReactPromise<bool> promise) noexcept;
  void ShowVersion() noexcept;
  void Quit() noexcept;
  void SetShortcuts(winrt::Microsoft::ReactNative::JSValueObject const& shortcuts) noexcept;

 private:
  winrt::Microsoft::ReactNative::ReactContext context_{nullptr};
  std::shared_ptr<WinUI3NativeLeaf> leaf_{WinUI3NativeLeaf::Shared()};
  std::function<void(std::string const&)> MenuAction;
};

}
