#pragma once

#include "CoreIPCBridge.h"
#include <NativeModules.h>

namespace LiteLLMMenu {

struct CoreIPCModule {
  REACT_MODULE(CoreIPCModule, L"LiteLLMCore");
  REACT_INIT(Initialize);
  REACT_METHOD(Send, L"send");
  REACT_METHOD(Shutdown, L"shutdown");
  REACT_METHOD(AddListener, L"addListener");
  REACT_METHOD(RemoveListeners, L"removeListeners");
  REACT_EVENT(CoreEvent, L"coreEvent");

  void Initialize(winrt::Microsoft::ReactNative::ReactContext const& context) noexcept;
  void Send(std::string request, winrt::Microsoft::ReactNative::ReactPromise<std::string> promise) noexcept;
  void Shutdown() noexcept;
  void AddListener(std::string const&) noexcept;
  void RemoveListeners(double) noexcept;
  std::function<void(std::string const&)> CoreEvent;

 private:
  winrt::Microsoft::ReactNative::ReactContext context_{nullptr};
};

}  // namespace LiteLLMMenu
