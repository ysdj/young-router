# WinUI 3 leaf

`WinUI3NativeLeaf` owns the Windows App SDK window, Win32 taskbar notification
icon, native split view, editor, selector, and confirmation boundary. The
RNW package provider registers `WinUI3NativeLeafModule` as
`LiteLLMNativeLeaf`; the module forwards route and status actions without
storing domain state.

These C++/WinRT sources are compiled by the checked-in Composition Win32 project,
which links the required Windows libraries and stages a relocatable Python Core
runtime beside the executable. The host uses an authenticated random loopback
endpoint; no fixed port, endpoint credential, raw configuration path, or editor
text enters React state.
