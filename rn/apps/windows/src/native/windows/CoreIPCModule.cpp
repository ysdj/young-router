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
    auto dispatcher = context_.JSDispatcher();
    auto emitter = CoreEvent;
    // One observer per React root: the menu host and every route window each
    // hold their own lifecycle, so the bridge fans Core events out instead of
    // delivering them to whichever root registered last.
    event_handler_token_ = CoreIPCBridge::Shared().AddEventHandler(
        [dispatcher, emitter](std::string const& event) {
          dispatcher.Post([emitter, event] { emitter(event); });
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
