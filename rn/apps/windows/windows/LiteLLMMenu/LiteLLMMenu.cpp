// LiteLLMMenu.cpp : Defines the entry point for the application.
//

#include "pch.h"
#include "LiteLLMMenu.h"

#include <algorithm>
#include <iterator>
#include <shellapi.h>
#include <string_view>

#include "AutolinkedNativeModules.g.h"

#include "NativeModules.h"
#include "JSValueWriter.h"
#include "CoreIPCBridge.h"
#include "CoreIPCModule.h"
#include "WinUIControls.h"
#include "WinUI3NativeLeaf.h"
#include "WinUI3NativeLeafModule.h"

namespace {
std::wstring AllowedRoute(std::wstring route) {
  static constexpr std::wstring_view allowed[] = {
      L"home", L"providers-models", L"codex-settings", L"claude-settings",
      L"runtime-settings", L"relay-accounts", L"webdav-settings",
      L"logs"};
  return std::find(std::begin(allowed), std::end(allowed), route) == std::end(allowed)
      ? std::wstring{} : route;
}

std::wstring AllowedLogTab(std::wstring tab) {
  static constexpr std::wstring_view allowed[] = {
      L"requests", L"service", L"menu", L"route-trace", L"recovery", L"online-usage"};
  return std::find(std::begin(allowed), std::end(allowed), tab) == std::end(allowed)
      ? std::wstring{} : tab;
}

std::wstring RouteWithAllowedTab(std::wstring route, std::wstring_view query) {
  route = AllowedRoute(std::move(route));
  if (route != L"logs" || query.empty()) return route;
  if (query.front() == L'?') query.remove_prefix(1);
  constexpr std::wstring_view prefix = L"tab=";
  if (query.rfind(prefix, 0) != 0 || query.find(L'&') != std::wstring::npos) return route;
  auto tab = AllowedLogTab(std::wstring(query.substr(prefix.size())));
  return tab.empty() ? route : route + L"?tab=" + tab;
}

std::wstring RouteFromProtocolUri(winrt::Windows::Foundation::Uri const& uri) {
  if (!uri || uri.SchemeName() != L"litellm-menu" || uri.Host() != L"open") return {};
  std::wstring route(uri.Path());
  while (!route.empty() && route.front() == L'/') route.erase(route.begin());
  return RouteWithAllowedTab(std::move(route), uri.Query());
}

std::wstring RouteFromActivation(
    winrt::Microsoft::Windows::AppLifecycle::AppActivationArguments const& arguments) {
  using winrt::Microsoft::Windows::AppLifecycle::ExtendedActivationKind;
  if (arguments.Kind() == ExtendedActivationKind::Protocol) {
    try {
      auto protocol = arguments.Data().as<winrt::Windows::ApplicationModel::Activation::IProtocolActivatedEventArgs>();
      return RouteFromProtocolUri(protocol.Uri());
    } catch (...) {
    }
  }
  return {};
}

std::wstring RequestedRouteFromCommandLine() {
  int count = 0;
  auto arguments = CommandLineToArgvW(GetCommandLineW(), &count);
  if (arguments == nullptr) return {};
  std::wstring route;
  if (count > 1) {
    std::wstring value(arguments[1]);
    constexpr std::wstring_view prefix = L"litellm-menu://open/";
    if (value.rfind(prefix, 0) == 0) {
      auto query = value.find_first_of(L"?#", prefix.size());
      route = RouteWithAllowedTab(
          value.substr(prefix.size(), query == std::wstring::npos ? query : query - prefix.size()),
          query == std::wstring::npos || value[query] != L'?' ? std::wstring_view{} : std::wstring_view(value).substr(query + 1));
    }
  }
  LocalFree(arguments);
  return route;
}

std::wstring RequestedRoute(
    winrt::Microsoft::Windows::AppLifecycle::AppActivationArguments const& arguments) {
  auto route = RouteFromActivation(arguments);
  return route.empty() ? RequestedRouteFromCommandLine() : route;
}
}  // namespace

// A PackageProvider containing any turbo modules you define within this app project
struct CompReactPackageProvider
    : winrt::implements<CompReactPackageProvider, winrt::Microsoft::ReactNative::IReactPackageProvider> {
 public: // IReactPackageProvider
  void CreatePackage(winrt::Microsoft::ReactNative::IReactPackageBuilder const &packageBuilder) noexcept {
    packageBuilder.AddTurboModule(
        L"LiteLLMCore",
        winrt::Microsoft::ReactNative::MakeModuleProvider<LiteLLMMenu::CoreIPCModule>());
    packageBuilder.AddTurboModule(
        L"LiteLLMNativeLeaf",
        winrt::Microsoft::ReactNative::MakeModuleProvider<LiteLLMMenu::WinUI3NativeLeafModule>());
    LiteLLMMenu::RegisterWinUIControls(packageBuilder);
  }
};

// The entry point of the Win32 application
_Use_decl_annotations_ int CALLBACK WinMain(HINSTANCE instance, HINSTANCE, PSTR /* commandLine */, int showCmd) {
  // Initialize WinRT
  winrt::init_apartment(winrt::apartment_type::single_threaded);

  using winrt::Microsoft::Windows::AppLifecycle::AppInstance;
  auto activation = AppInstance::GetCurrent().GetActivatedEventArgs();
  auto mainInstance = AppInstance::FindOrRegisterForKey(L"LiteLLMMenu.Main");
  if (!mainInstance.IsCurrent()) {
    try {
      mainInstance.RedirectActivationToAsync(activation).get();
    } catch (...) {
    }
    return 0;
  }
  auto activationToken = mainInstance.Activated([](auto const&, auto const& args) {
    auto route = RouteFromActivation(args);
    if (route.empty()) return;
    LiteLLMMenu::WinUI3NativeLeaf::Shared()->DispatchAction("open-" + winrt::to_string(route));
  });

  // Enable per monitor DPI scaling
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

  // Find the path hosting the app exe file
  WCHAR appDirectory[MAX_PATH];
  GetModuleFileNameW(NULL, appDirectory, MAX_PATH);
  PathCchRemoveFileSpec(appDirectory, MAX_PATH);

  // Create a ReactNativeWin32App with the ReactNativeAppBuilder
  auto reactNativeWin32App{winrt::Microsoft::ReactNative::ReactNativeAppBuilder().Build()};

  // Configure the initial InstanceSettings for the app's ReactNativeHost
  auto settings{reactNativeWin32App.ReactNativeHost().InstanceSettings()};
  // Register any autolinked native modules
  RegisterAutolinkedNativeModulePackages(settings.PackageProviders());
  // Register any native modules defined within this app project
  settings.PackageProviders().Append(winrt::make<CompReactPackageProvider>());

#if BUNDLE
  // Load the JS bundle from a file (not Metro):
  // Set the path (on disk) where the .bundle file is located
  settings.BundleRootPath(std::wstring(L"file://").append(appDirectory).append(L"\\Bundle\\").c_str());
  // Set the name of the bundle file (without the .bundle extension)
  settings.JavaScriptBundleFile(L"index.windows");
  // Disable hot reload
  settings.UseFastRefresh(false);
#else
  // Load the JS bundle from Metro
  settings.JavaScriptBundleFile(L"index");
  // Enable hot reload
  settings.UseFastRefresh(true);
#endif
#if _DEBUG
  // For Debug builds
  // Enable Direct Debugging of JS
  settings.UseDirectDebugger(true);
  // Enable the Developer Menu
  settings.UseDeveloperSupport(true);
#else
  // For Release builds:
  // Disable Direct Debugging of JS
  settings.UseDirectDebugger(false);
  // Disable the Developer Menu
  settings.UseDeveloperSupport(false);
#endif

  // Get the AppWindow so we can configure its initial title and size
  auto appWindow{reactNativeWin32App.AppWindow()};
  appWindow.Title(L"LiteLLM Menu");
  appWindow.Resize({1120, 680});
  LiteLLMMenu::DisableWindowTransitions(winrt::Microsoft::UI::GetWindowFromWindowId(appWindow.Id()));
  LiteLLMMenu::ConfigureImmediateXamlPresentation();
  appWindow.Destroying([](auto const &, auto const &) {
    LiteLLMMenu::CoreIPCBridge::Shared().Stop();
  });

  // Get the ReactViewOptions so we can set the initial RN component to load
  auto viewOptions{reactNativeWin32App.ReactViewOptions()};
  viewOptions.ComponentName(L"LiteLLMMenu");
  auto requestedRoute = RequestedRoute(activation);
  if (!requestedRoute.empty()) {
    viewOptions.InitialProps(winrt::Microsoft::ReactNative::MakeJSValueWriter(
        [requestedRoute](winrt::Microsoft::ReactNative::IJSValueWriter const &writer) noexcept {
      writer.WriteObjectBegin();
      auto query = requestedRoute.find(L'?');
      auto route = requestedRoute.substr(0, query);
      writer.WritePropertyName(L"initialRoute");
      writer.WriteString(route);
      if (query != std::wstring::npos) {
        constexpr std::wstring_view prefix = L"?tab=";
        if (requestedRoute.substr(query).rfind(prefix, 0) == 0) {
          writer.WritePropertyName(L"initialLogTab");
          writer.WriteString(requestedRoute.substr(query + prefix.size()));
        }
      }
      writer.WriteObjectEnd();
    }));
  }

  // Start the app
  reactNativeWin32App.Start();
  mainInstance.Activated(activationToken);
}
