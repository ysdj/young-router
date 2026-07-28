#pragma once

#include <winrt/Microsoft.ReactNative.h>

namespace LiteLLMMenu {

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder) noexcept;

}  // namespace LiteLLMMenu
