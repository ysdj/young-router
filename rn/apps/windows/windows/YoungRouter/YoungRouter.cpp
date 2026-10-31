// YoungRouter.cpp : Defines the entry point for the application.
//

#include "pch.h"
#include "YoungRouter.h"

#include <algorithm>
#include <appmodel.h>
#include <atomic>
#include <iterator>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <shellapi.h>
#include <string_view>
#include <thread>

#include "AutolinkedNativeModules.g.h"

#include "NativeModules.h"
#include <winrt/Microsoft.ReactNative.h>
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
      L"runtime-settings", L"data-management", L"provider-wizard",
      L"logs"};
  return std::find(std::begin(allowed), std::end(allowed), route) == std::end(allowed)
      ? std::wstring{} : route;
}

std::wstring AllowedLogTab(std::wstring tab) {
  static constexpr std::wstring_view allowed[] = {
      L"requests", L"service", L"actions", L"route-trace", L"recovery", L"online-usage"};
  return std::find(std::begin(allowed), std::end(allowed), tab) == std::end(allowed)
      ? std::wstring{} : tab;
}

std::wstring CanonicalRoute(std::wstring route) {
  // The former Service Provider Management surfaces are integrated into the
  // unified provider workspace; legacy routes land there.
  if (route == L"relay-accounts") return L"providers-models";
  if (route == L"relay-add") return L"provider-wizard";
  return route;
}

std::wstring RouteWithAllowedTab(std::wstring route, std::wstring_view query) {
  route = CanonicalRoute(AllowedRoute(std::move(route)));
  if (route != L"logs" || query.empty()) return route;
  if (query.front() == L'?') query.remove_prefix(1);
  constexpr std::wstring_view prefix = L"tab=";
  if (query.rfind(prefix, 0) != 0 || query.find(L'&') != std::wstring::npos) return route;
  auto tab = AllowedLogTab(std::wstring(query.substr(prefix.size())));
  return tab.empty() ? route : route + L"?tab=" + tab;
}

std::wstring RouteFromProtocolUri(winrt::Windows::Foundation::Uri const& uri) {
  if (!uri || uri.SchemeName() != L"young-router" || uri.Host() != L"open") return {};
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
    constexpr std::wstring_view prefix = L"young-router://open/";
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

std::wstring RequestedChildRouteFromCommandLine() {
  int count = 0;
  auto arguments = CommandLineToArgvW(GetCommandLineW(), &count);
  if (arguments == nullptr) return {};
  std::wstring route;
  constexpr std::wstring_view prefix = L"--young-router-child=";
  for (int index = 1; index < count; ++index) {
    std::wstring value(arguments[index]);
    if (value.rfind(prefix, 0) == 0) {
      route = RouteWithAllowedTab(value.substr(prefix.size()), {});
      break;
    }
  }
  LocalFree(arguments);
  return route == L"provider-wizard" || route == L"file-editor" ? route : std::wstring{};
}

std::wstring RequestedChildFileTargetFromCommandLine() {
  int count = 0;
  auto arguments = CommandLineToArgvW(GetCommandLineW(), &count);
  if (arguments == nullptr) return {};
  std::wstring target;
  constexpr std::wstring_view prefix = L"--young-router-file-target=";
  for (int index = 1; index < count; ++index) {
    std::wstring value(arguments[index]);
    if (value.rfind(prefix, 0) == 0) {
      target = value.substr(prefix.size());
      break;
    }
  }
  LocalFree(arguments);
  return target;
}

std::wstring QuoteCommandLineArgument(std::wstring_view value) {
  std::wstring result = L"\"";
  size_t slashes = 0;
  for (const auto character : value) {
    if (character == L'\\') {
      ++slashes;
    } else if (character == L'\"') {
      result.append(slashes * 2 + 1, L'\\');
      result.push_back(L'\"');
      slashes = 0;
    } else {
      result.append(slashes, L'\\');
      result.push_back(character);
      slashes = 0;
    }
  }
  result.append(slashes * 2, L'\\');
  result.push_back(L'\"');
  return result;
}

struct ReactChildHostState {
  std::wstring route;
  std::wstring initial_file_target;
  HWND parent = nullptr;
  std::atomic<HWND> window{nullptr};
};
std::mutex g_child_lock;
std::shared_ptr<ReactChildHostState> g_child_state;
std::map<HWND, std::wstring> g_child_routes;
std::atomic<bool> g_child_process_mode{false};
std::wstring g_child_process_route;
HANDLE g_child_process_handle = nullptr;
}  // namespace

// A PackageProvider containing any turbo modules you define within this app project
struct CompReactPackageProvider
    : winrt::implements<CompReactPackageProvider, winrt::Microsoft::ReactNative::IReactPackageProvider> {
 public: // IReactPackageProvider
  void CreatePackage(winrt::Microsoft::ReactNative::IReactPackageBuilder const &packageBuilder) noexcept {
    packageBuilder.AddTurboModule(
        L"LiteLLMCore",
        winrt::Microsoft::ReactNative::MakeModuleProvider<YoungRouter::CoreIPCModule>());
    packageBuilder.AddTurboModule(
        L"LiteLLMNativeLeaf",
        winrt::Microsoft::ReactNative::MakeModuleProvider<YoungRouter::WinUI3NativeLeafModule>());
    YoungRouter::RegisterWinUIControls(packageBuilder);
  }
};

namespace YoungRouter {

std::wstring InitialChildRoute(HWND window) {
  std::lock_guard<std::mutex> guard(g_child_lock);
  auto found = g_child_routes.find(window);
  return found == g_child_routes.end() ? std::wstring{} : found->second;
}

HWND CurrentReactChildHostWindow() noexcept {
  std::lock_guard<std::mutex> guard(g_child_lock);
  return g_child_state ? g_child_state->window.load() : nullptr;
}

void CloseReactChildWindow() {
  if (g_child_process_mode) {
    if (auto child = WinUI3NativeLeaf::Shared()->WindowHandle(); child != nullptr) {
      PostMessageW(child, WM_APP + 33, 0, 0);
    }
    return;
  }
  std::shared_ptr<ReactChildHostState> state;
  {
    std::lock_guard<std::mutex> guard(g_child_lock);
    state = g_child_state;
  }
  if (!state) return;
  const auto child_handle = state->window.load();
  if (child_handle != nullptr) ShowWindow(child_handle, SW_HIDE);
  const auto parent = state->parent;
  if (parent != nullptr && IsWindow(parent)) PostMessageW(parent, WM_APP + 34, 0, 0);
  {
    std::lock_guard<std::mutex> guard(g_child_lock);
    if (child_handle != nullptr) g_child_routes[child_handle] = state->route;
  }
}

void RunReactChildHost(std::shared_ptr<ReactChildHostState> state) {
  HWND child_handle = nullptr;
  try {
    winrt::init_apartment(winrt::apartment_type::single_threaded);
    auto child = winrt::Microsoft::ReactNative::ReactNativeAppBuilder().Build();
    auto settings = child.ReactNativeHost().InstanceSettings();
    RegisterAutolinkedNativeModulePackages(settings.PackageProviders());
    settings.PackageProviders().Append(winrt::make<CompReactPackageProvider>());

#if BUNDLE
    WCHAR app_directory[MAX_PATH];
    GetModuleFileNameW(nullptr, app_directory, MAX_PATH);
    PathCchRemoveFileSpec(app_directory, MAX_PATH);
    settings.BundleRootPath(std::wstring(L"file://").append(app_directory).append(L"\\Bundle\\").c_str());
    settings.JavaScriptBundleFile(L"index.windows");
    settings.UseFastRefresh(false);
#else
    settings.JavaScriptBundleFile(L"index");
    settings.UseFastRefresh(true);
#endif

    auto view_options = child.ReactViewOptions();
    view_options.ComponentName(L"YoungRouter");
    const auto requested_route = state->route;
    const auto requested_file = state->initial_file_target;
    view_options.InitialProps(winrt::Microsoft::ReactNative::MakeJSValueWriter(
        [requested_route, requested_file](winrt::Microsoft::ReactNative::IJSValueWriter const& writer) noexcept {
      writer.WriteObjectBegin();
      writer.WritePropertyName(L"initialRoute");
      writer.WriteString(requested_route);
      writer.WritePropertyName(L"isPrimaryHost");
      writer.WriteBoolean(false);
      if (!requested_file.empty()) {
        writer.WritePropertyName(L"initialFileTarget");
        writer.WriteString(requested_file);
      }
      writer.WriteObjectEnd();
    }));

    auto app_window = child.AppWindow();
    app_window.Title(state->route == L"provider-wizard" ? L"Add Provider" : L"Edit File");
    child_handle = winrt::Microsoft::UI::GetWindowFromWindowId(app_window.Id());
    const LONG content_width = state->route == L"provider-wizard" ? 620 : 900;
    const LONG content_height = state->route == L"provider-wizard" ? 460 : 560;
    const auto frame = FrameTrackSizeForContentDips(child_handle, content_width, content_height);
    app_window.Resize({frame.x, frame.y});
    {
      std::lock_guard<std::mutex> guard(g_child_lock);
      state->window.store(child_handle);
      g_child_routes[child_handle] = state->route;
    }

    // Start owns the child dispatcher, sets the top-level window id before
    // creating the JS instance, mounts the React view host, and runs the
    // message loop on this same thread.
    child.Start();
  } catch (winrt::hresult_error const& error) {
    OutputDebugStringW((L"Young Router child host failed: " + error.message() + L"\\n").c_str());
  } catch (...) {
    OutputDebugStringW(L"Young Router child host failed.\\n");
  }

  const auto parent = state->parent;
  {
    std::lock_guard<std::mutex> guard(g_child_lock);
    if (child_handle != nullptr) g_child_routes.erase(child_handle);
    state->window.store(nullptr);
    if (g_child_state == state) g_child_state.reset();
  }
  if (parent != nullptr && IsWindow(parent)) {
    EnableWindow(parent, TRUE);
    SetForegroundWindow(parent);
    SetFocus(parent);
  }
  winrt::uninit_apartment();
}

void OpenReactChildWindow(std::wstring_view route, std::wstring_view initial_file_target) {
  if (g_child_process_mode) return;
  if (route != L"provider-wizard" && route != L"file-editor") return;
  const auto parent = WinUI3NativeLeaf::Shared()->WindowHandle();
  if (parent == nullptr) return;
  {
    std::lock_guard<std::mutex> guard(g_child_lock);
    if (g_child_process_handle != nullptr) {
      EnableWindow(parent, FALSE);
      return;
    }
    std::wstring module(MAX_PATH, L'\0');
    DWORD length = GetModuleFileNameW(nullptr, module.data(), static_cast<DWORD>(module.size()));
    if (length == 0) return;
    module.resize(length);
    std::wstring command = QuoteCommandLineArgument(module);
    command.append(L" --young-router-child=");
    command.append(route);
    if (!initial_file_target.empty()) {
      command.append(L" --young-router-file-target=");
      command.append(QuoteCommandLineArgument(initial_file_target));
    }
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    auto mutable_command = std::vector<wchar_t>(command.begin(), command.end());
    mutable_command.push_back(L'\0');
    if (!CreateProcessW(module.c_str(), mutable_command.data(), nullptr, nullptr, FALSE,
                        CREATE_UNICODE_ENVIRONMENT, nullptr, nullptr, &startup, &process)) return;
    CloseHandle(process.hThread);
    g_child_process_handle = process.hProcess;
    EnableWindow(parent, FALSE);
    auto child_handle = process.hProcess;
    std::thread([parent, child_handle] {
      WaitForSingleObject(child_handle, INFINITE);
      CloseHandle(child_handle);
      PostMessageW(parent, WM_APP + 34, 0, 0);
      std::lock_guard<std::mutex> guard(g_child_lock);
      if (g_child_process_handle == child_handle) g_child_process_handle = nullptr;
    }).detach();
  }
}

}  // namespace YoungRouter

// The entry point of the Win32 application
_Use_decl_annotations_ int CALLBACK WinMain(HINSTANCE instance, HINSTANCE, PSTR /* commandLine */, int showCmd) {
  try {
  // A directly launched unpackaged EXE has no Package Identity.  AppInstance
  // calls fail-fast in that mode on ARM64, so only use the single-instance
  // activation API when Windows actually supplied package identity. Keep this
  // check before WinRT/Windows App SDK initialization; even constructing those
  // objects can fail-fast for an unpackaged ARM64 process.
  UINT32 packagePathLength = 0;
  const bool hasPackageIdentity =
      GetCurrentPackagePath(&packagePathLength, nullptr) == ERROR_INSUFFICIENT_BUFFER;
  if (!hasPackageIdentity) {
    // WinUI 3's packaged host requires Package Identity. A user may still
    // double-click the build-output EXE, so hand that launch to the installed
    // MSIX instead of letting the framework fail-fast during initialization.
    // AppsFolder uses the Package Family Name, which is stable across package
    // versions and does not include the version/architecture fields.
    constexpr wchar_t packaged_app[] =
        L"shell:AppsFolder\\YoungRouter_vephk5vwyzj3j!App";
    auto result = reinterpret_cast<intptr_t>(ShellExecuteW(
        nullptr, L"open", L"explorer.exe", packaged_app, nullptr, SW_SHOWNORMAL));
    if (result <= 32) {
      MessageBoxW(nullptr, L"Install the Young Router MSIX package before launching this file.",
          L"Young Router", MB_ICONERROR | MB_OK);
    }
    return 0;
  }

  // Initialize WinRT
  winrt::init_apartment(winrt::apartment_type::single_threaded);
  const auto childRoute = RequestedChildRouteFromCommandLine();
  const bool childMode = !childRoute.empty();
  if (childMode) {
    g_child_process_mode = true;
    g_child_process_route = childRoute;
  }

  using winrt::Microsoft::Windows::AppLifecycle::AppInstance;
  winrt::Microsoft::Windows::AppLifecycle::AppActivationArguments activation{nullptr};
  AppInstance mainInstance{nullptr};
  winrt::event_token activationToken{};
  activation = AppInstance::GetCurrent().GetActivatedEventArgs();
  if (!childMode) {
    mainInstance = AppInstance::FindOrRegisterForKey(L"YoungRouter.Main");
    if (!mainInstance.IsCurrent()) {
      try {
        mainInstance.RedirectActivationToAsync(activation).get();
      } catch (...) {
      }
      return 0;
    }
    activationToken = mainInstance.Activated([](auto const&, auto const& args) {
      auto route = RouteFromActivation(args);
      if (route.empty()) return;
      YoungRouter::WinUI3NativeLeaf::Shared()->DispatchAction("open-" + winrt::to_string(route));
    });
  }

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
  appWindow.Title(childMode && childRoute == L"provider-wizard" ? L"Add Provider"
      : childMode && childRoute == L"file-editor" ? L"Edit File" : L"Young Router");
  // The React root is created by this host, so the leaf's window handle is
  // known here: ``GetActiveWindow`` answers nothing during module
  // initialization, which left every OpenRoute call with a null HWND and the
  // window never shown, titled, or sized.
  auto windowHandle = winrt::Microsoft::UI::GetWindowFromWindowId(appWindow.Id());
  YoungRouter::WinUI3NativeLeaf::Shared()->SetChildHost(childMode);
  YoungRouter::WinUI3NativeLeaf::Shared()->Initialize(windowHandle);
  // Start at the settings shell's content size, in physical pixels for this
  // monitor's DPI.  The root is sized from the client rect when Start()
  // creates it; resizing the still-hidden window afterwards (the old
  // 1120x680 start, then OpenRoute's 960x660 DIP) left the composition root
  // at the smaller size and the window painted the rest as empty background.
  const auto initialContentWidth = childMode && childRoute == L"provider-wizard" ? 620 : childMode ? 900 : 960;
  const auto initialContentHeight = childMode && childRoute == L"provider-wizard" ? 460 : childMode ? 560 : 660;
  // Keep the parent settings-shell geometry explicit; child windows use their
  // own content dimensions below.
  const auto initialFrame = childMode
      ? YoungRouter::FrameTrackSizeForContentDips(windowHandle, initialContentWidth, initialContentHeight)
      : YoungRouter::FrameTrackSizeForContentDips(windowHandle, 960, 660);
  appWindow.Resize({initialFrame.x, initialFrame.y});
  YoungRouter::DisableWindowTransitions(windowHandle);
  YoungRouter::ConfigureImmediateXamlPresentation();
  appWindow.Destroying([childMode](auto const &, auto const &) {
    if (!childMode) YoungRouter::CoreIPCBridge::Shared().Stop();
  });

  // Get the ReactViewOptions so we can set the initial RN component to load
  auto viewOptions{reactNativeWin32App.ReactViewOptions()};
  viewOptions.ComponentName(L"YoungRouter");
  auto requestedRoute = childMode ? childRoute : RequestedRoute(activation);
  if (childMode || !requestedRoute.empty()) {
    viewOptions.InitialProps(winrt::Microsoft::ReactNative::MakeJSValueWriter(
        [requestedRoute, childMode](winrt::Microsoft::ReactNative::IJSValueWriter const& writer) noexcept {
      writer.WriteObjectBegin();
      auto query = requestedRoute.find(L'?');
      auto route = requestedRoute.substr(0, query);
      writer.WritePropertyName(L"initialRoute");
      writer.WriteString(route);
      writer.WritePropertyName(L"isPrimaryHost");
      writer.WriteBoolean(!childMode);
      if (childMode && route == L"file-editor") {
        auto target = RequestedChildFileTargetFromCommandLine();
        if (!target.empty()) {
          writer.WritePropertyName(L"initialFileTarget");
          writer.WriteString(target);
        }
      }
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
  if (!childMode) mainInstance.Activated(activationToken);
  } catch (winrt::hresult_error const& error) {
    MessageBoxW(nullptr, error.message().c_str(), L"Young Router startup failure", MB_ICONERROR | MB_OK);
    return static_cast<int>(error.code().value);
  } catch (std::exception const& error) {
    MessageBoxA(nullptr, error.what(), "Young Router startup failure", MB_ICONERROR | MB_OK);
    return -1;
  } catch (...) {
    MessageBoxW(nullptr, L"Unknown startup failure", L"Young Router startup failure", MB_ICONERROR | MB_OK);
    return -1;
  }
  return 0;
}
