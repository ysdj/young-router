#include "pch.h"
#include "CoreIPCModule.h"

namespace YoungRouter {

void CoreIPCModule::Initialize(winrt::Microsoft::ReactNative::ReactContext const& context) noexcept {
  try {
    context_ = context;
  } catch (...) {
  }
}

void CoreIPCModule::RegisterEventHandler() noexcept {
  try {
    if (event_handler_token_ != 0) return;
    auto call_invoker = context_.CallInvoker();
    auto emitter = CoreEvent;
    // One observer per React root: the menu host and every route window each
    // hold their own lifecycle, so the bridge fans Core events out instead of
    // delivering them to whichever root registered last.
    event_handler_token_ = CoreIPCBridge::Shared().AddEventHandler(
        [call_invoker, emitter](std::string const& event) {
          call_invoker->invokeAsync([emitter, event](facebook::jsi::Runtime&) { emitter(event); });
        });
  } catch (...) {
  }
}

void CoreIPCModule::UnregisterEventHandler() noexcept {
  try {
    if (event_handler_token_ == 0) return;
    CoreIPCBridge::Shared().RemoveEventHandler(event_handler_token_);
    event_handler_token_ = 0;
  } catch (...) {
  }
}

void CoreIPCModule::Send(
    std::string request,
    winrt::Microsoft::ReactNative::ReactPromise<std::string> promise) noexcept {
  try {
    auto call_invoker = context_.CallInvoker();
    std::thread([request = std::move(request), promise = std::move(promise), call_invoker]() mutable {
      try {
        std::string response = CoreIPCBridge::Shared().Send(request);
        call_invoker->invokeAsync([promise = std::move(promise), response = std::move(response)](facebook::jsi::Runtime&) {
          promise.Resolve(response);
        });
      } catch (...) {
        call_invoker->invokeAsync([promise = std::move(promise)](facebook::jsi::Runtime&) { promise.Reject("The local Core is unavailable."); });
      }
    }).detach();
  } catch (...) {
    promise.Reject("The local Core is unavailable.");
  }
}

void CoreIPCModule::Shutdown() noexcept {
  try {
    UnregisterEventHandler();
    CoreIPCBridge::Shared().Stop();
  } catch (...) {
  }
}

void CoreIPCModule::AddListener(std::string const&) noexcept {
  RegisterEventHandler();
}

void CoreIPCModule::RemoveListeners(double count) noexcept {
  if (count > 0) return;
  UnregisterEventHandler();
}

}  // namespace YoungRouter
