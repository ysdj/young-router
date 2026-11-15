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
    // One observer per React root: the menu host and every route window each
    // hold their own lifecycle, so the bridge fans Core events out instead of
    // delivering them to whichever root registered last.  Read the event field
    // when the event arrives: it is filled when JS subscribes, after this
    // registration, and a copy taken here stays empty.
    event_handler_token_ = CoreIPCBridge::Shared().AddEventHandler(
        [this, call_invoker](std::string const& event) {
          call_invoker->invokeAsync([this, event](facebook::jsi::Runtime&) {
            if (CoreEvent) CoreEvent(event);
          });
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
    // ReactPromise is a copyable handle, so the lambdas take copies: the
    // failure paths below need a live promise, and a moved-from handle cannot
    // settle the JS one.
    std::thread([request = std::move(request), promise, call_invoker]() mutable {
      try {
        std::string response = CoreIPCBridge::Shared().Send(request);
        call_invoker->invokeAsync([promise, response = std::move(response)](facebook::jsi::Runtime&) {
          promise.Resolve(response);
        });
      } catch (...) {
        call_invoker->invokeAsync([promise](facebook::jsi::Runtime&) { promise.Reject("The local Core is unavailable."); });
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
  // The shared bridge holds this module together with its raw this pointer, so
  // a root that drops its subscription has to detach here; the next Core event
  // would otherwise be delivered into the torn-down module.
  if (count <= 0) return;
  UnregisterEventHandler();
}

}  // namespace YoungRouter
