#include <windows.h>
#include <shellapi.h>

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int show) {
  constexpr wchar_t app[] = L"shell:AppsFolder\\YoungRouter_vephk5vwyzj3j!App";
  auto result = reinterpret_cast<INT_PTR>(
      ShellExecuteW(nullptr, L"open", L"explorer.exe", app, nullptr, show));
  if (result <= 32) {
    MessageBoxW(nullptr,
        L"Install the Young Router MSIX package before launching this file.",
        L"Young Router", MB_ICONERROR | MB_OK);
    return 1;
  }
  return 0;
}
