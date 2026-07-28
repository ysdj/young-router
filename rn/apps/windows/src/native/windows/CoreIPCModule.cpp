#include "pch.h"
#include "CoreIPCModule.h"

namespace LiteLLMMenu {

void CoreIPCModule::Initialize(winrt::Microsoft::ReactNative::ReactContext const& context) noexcept {
  try {
    context_ = context;
    auto dispatcher = context_.JSDispatcher();
    auto emitter = CoreEvent;
    CoreIPCBridge::Shared().SetEventHandler([dispatcher, emitter](std::string const& event) {
      dispatcher.Post([emitter, event] { emitter(event); });
    });
  } catch (...) {
  }
}

void CoreIPCModule::Send(
    std::string request,
    winrt::Microsoft::ReactNative::ReactPromise<std::string> promise) noexcept {
  try {
    auto dispatcher = context_.JSDispatcher();
    std::thread([request = std::move(request), promise = std::move(promise), dispatcher]() mutable {
      try {
        std::string response = CoreIPCBridge::Shared().Send(request);
        dispatcher.Post([promise = std::move(promise), response = std::move(response)] { promise.Resolve(response); });
      } catch (...) {
        dispatcher.Post([promise = std::move(promise)] { promise.Reject("The local Core is unavailable."); });
      }
    }).detach();
  } catch (...) {
    promise.Reject("The local Core is unavailable.");
  }
}

void CoreIPCModule::Shutdown() noexcept {
  try {
    CoreIPCBridge::Shared().Stop();
  } catch (...) {
  }
}

void CoreIPCModule::AddListener(std::string const&) noexcept {}
void CoreIPCModule::RemoveListeners(double) noexcept {}

}  // namespace LiteLLMMenu
