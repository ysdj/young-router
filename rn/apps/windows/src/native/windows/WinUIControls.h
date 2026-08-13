#pragma once

#include <winrt/Microsoft.ReactNative.h>

namespace LiteLLMMenu {

// Applies the process-wide XAML resource policy used by every native control:
// UI state changes must be presented in the same frame, without theme motion.
void ConfigureImmediateXamlPresentation() noexcept;

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder) noexcept;

}  // namespace LiteLLMMenu
