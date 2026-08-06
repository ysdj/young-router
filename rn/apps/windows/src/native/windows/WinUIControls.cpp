#include "pch.h"
#include "WinUIControls.h"
#include "CoreIPCBridge.h"

#if defined(RNW_NEW_ARCH)

#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUIButton.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUICheckbox.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUIPicker.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISecureTextEditor.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISecureTextInput.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISegmentedControl.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISelectableRow.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISplitView.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUISwitch.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUITable.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUITextEditor.g.h"
#include "codegen/react/components/LiteLLMMenu/LiteLLMWinUITextInput.g.h"

#include <winrt/Microsoft.UI.Xaml.Controls.h>
#include <winrt/Microsoft.UI.Xaml.Controls.Primitives.h>
#include <winrt/Microsoft.UI.Xaml.Automation.h>
#include <winrt/Microsoft.UI.Xaml.Input.h>
#include <winrt/Microsoft.UI.Xaml.Media.h>
#include <winrt/Windows.System.h>
#include <winrt/Windows.UI.Text.h>
#include <winrt/Windows.UI.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iterator>
#include <limits>
#include <memory>
#include <optional>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

using winrt::Microsoft::ReactNative::Composition::ContentIslandComponentView;
using winrt::Microsoft::UI::Xaml::Controls::Button;
using winrt::Microsoft::UI::Xaml::Controls::Border;
using winrt::Microsoft::UI::Xaml::Controls::CheckBox;
using winrt::Microsoft::UI::Xaml::Controls::ComboBox;
using winrt::Microsoft::UI::Xaml::Controls::FontIcon;
using winrt::Microsoft::UI::Xaml::Controls::Grid;
using winrt::Microsoft::UI::Xaml::Controls::HyperlinkButton;
using winrt::Microsoft::UI::Xaml::Controls::ListView;
using winrt::Microsoft::UI::Xaml::Controls::ListViewSelectionMode;
using winrt::Microsoft::UI::Xaml::Controls::Orientation;
using winrt::Microsoft::UI::Xaml::Controls::PasswordBox;
using winrt::Microsoft::UI::Xaml::Controls::ScrollViewer;
using winrt::Microsoft::UI::Xaml::Controls::StackPanel;
using winrt::Microsoft::UI::Xaml::Controls::TextBox;
using winrt::Microsoft::UI::Xaml::Controls::TextBlock;
using winrt::Microsoft::UI::Xaml::Controls::ToolTipService;
using winrt::Microsoft::UI::Xaml::Controls::ToggleSwitch;
using winrt::Microsoft::UI::Xaml::Controls::Primitives::Thumb;
using winrt::Microsoft::UI::Xaml::Controls::Primitives::ToggleButton;
using winrt::Microsoft::UI::Xaml::Media::SolidColorBrush;
using winrt::Microsoft::UI::Xaml::Media::FontFamily;
using winrt::Microsoft::UI::Xaml::Thickness;

constexpr size_t kSecureEditorMaximumBytes = 2 * 1024 * 1024;
constexpr auto kSecureEditorDebounce = std::chrono::milliseconds{450};
constexpr int32_t kSecureEditorInitialRevision = 0;
constexpr double kUIFontSize = 13.0;

winrt::hstring ToHString(std::string const& value) {
  return winrt::to_hstring(value);
}

bool Enabled(std::optional<bool> const& disabled) {
  return !disabled.value_or(false);
}

SolidColorBrush ThemeBrush(wchar_t const* resource, winrt::Windows::UI::Color fallback) {
  try {
    auto resources = winrt::Microsoft::UI::Xaml::Application::Current().Resources();
    auto value = resources.Lookup(winrt::box_value(winrt::hstring(resource)));
    if (auto brush = value.try_as<SolidColorBrush>()) return brush;
  } catch (...) {
  }
  return SolidColorBrush(fallback);
}

SolidColorBrush AccentBrush() {
  return ThemeBrush(L"AccentFillColorDefaultBrush", winrt::Windows::UI::Color{255, 0, 95, 184});
}

SolidColorBrush DestructiveBrush() {
  return ThemeBrush(L"SystemFillColorCriticalBrush", winrt::Windows::UI::Color{255, 196, 43, 28});
}

SolidColorBrush SelectionBrush() {
  return ThemeBrush(L"SubtleFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 220, 235, 252});
}

SolidColorBrush AlternatingRowBrush() {
  return ThemeBrush(L"SubtleFillColorTransparentBrush", winrt::Windows::UI::Color{20, 128, 128, 128});
}

SolidColorBrush SecondaryTextBrush() {
  return ThemeBrush(L"TextFillColorSecondaryBrush", winrt::Windows::UI::Color{255, 110, 110, 115});
}

ScrollViewer FindTextEditorScrollViewer(
    winrt::Microsoft::UI::Xaml::DependencyObject const& root) {
  if (!root) return nullptr;
  auto const child_count = winrt::Microsoft::UI::Xaml::Media::VisualTreeHelper::GetChildrenCount(root);
  for (int32_t index = 0; index < child_count; ++index) {
    auto const child = winrt::Microsoft::UI::Xaml::Media::VisualTreeHelper::GetChild(root, index);
    if (auto const viewer = child.try_as<ScrollViewer>()) return viewer;
    if (auto const viewer = FindTextEditorScrollViewer(child)) return viewer;
  }
  return nullptr;
}

ScrollViewer FindListScrollViewer(
    winrt::Microsoft::UI::Xaml::DependencyObject const& root) {
  if (!root) return nullptr;
  auto const child_count = winrt::Microsoft::UI::Xaml::Media::VisualTreeHelper::GetChildrenCount(root);
  for (int32_t index = 0; index < child_count; ++index) {
    auto const child = winrt::Microsoft::UI::Xaml::Media::VisualTreeHelper::GetChild(root, index);
    if (auto const viewer = child.try_as<ScrollViewer>()) return viewer;
    if (auto const viewer = FindListScrollViewer(child)) return viewer;
  }
  return nullptr;
}

bool ListIsFollowingBottom(ListView const& list) {
  auto const viewer = FindListScrollViewer(list);
  return !viewer || viewer.ScrollableHeight() <= 4.0 ||
      viewer.ScrollableHeight() - viewer.VerticalOffset() <= 4.0;
}

double ColumnWidth(std::vector<float> const& widths, size_t index) {
  if (index >= widths.size() || widths[index] <= 0) return 140.0;
  return std::max(88.0, static_cast<double>(widths[index]));
}

void ApplyKeyboardType(TextBox const& text_box, std::optional<std::string> const& keyboard_type) {
  auto scope = winrt::Microsoft::UI::Xaml::Input::InputScope{};
  auto name = winrt::Microsoft::UI::Xaml::Input::InputScopeName{};
  name.NameValue(
      keyboard_type && *keyboard_type == "numeric"
          ? winrt::Microsoft::UI::Xaml::Input::InputScopeNameValue::Number
          : winrt::Microsoft::UI::Xaml::Input::InputScopeNameValue::Default);
  scope.Names().Append(name);
  text_box.InputScope(scope);
}

struct SecureEditorLifecycle final {
  std::atomic<bool> alive{true};
  std::atomic<bool> attached{true};
  std::atomic<uint64_t> generation{0};
  std::atomic<uint64_t> edit_serial{0};
  std::atomic<uint64_t> debounce_serial{0};
  std::atomic<bool> staging{false};
  std::atomic<bool> terminal{false};
};

struct SecureInputLifecycle final {
  std::atomic<bool> alive{true};
  std::atomic<uint64_t> generation{0};
  std::atomic<bool> staging{false};
};

struct ButtonComponentView final
    : winrt::implements<ButtonComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUIButton<ButtonComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    container_ = Grid{};
    button_ = Button{};
    hyperlink_ = HyperlinkButton{};
    button_.FontSize(kUIFontSize);
    hyperlink_.FontSize(kUIFontSize);
    button_.HorizontalAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    button_.VerticalAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Stretch);
    hyperlink_.HorizontalAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    hyperlink_.VerticalAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Stretch);
    hyperlink_.HorizontalContentAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Left);
    hyperlink_.Padding(winrt::Microsoft::UI::Xaml::Thickness{0, 0, 0, 0});
    hyperlink_.MinWidth(0.0);
    hyperlink_.MinHeight(22.0);
    button_.Click([this](auto const&, auto const&) {
      EmitPress();
    });
    hyperlink_.Click([this](auto const&, auto const&) {
      EmitPress();
    });
    container_.Children().Append(button_);
    container_.Children().Append(hyperlink_);
    island_.Content(container_);
    island_view.Connect(island_.ContentIsland());
    default_padding_ = button_.Padding();
    default_min_width_ = button_.MinWidth();
    default_min_height_ = button_.MinHeight();
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUIButtonProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUIButtonProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUIButton<ButtonComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  void EmitPress() noexcept {
    if (auto emitter = EventEmitter()) {
      winrt::LiteLLMMenu::Codegen::LiteLLMWinUIButtonEventEmitter::OnPress args;
      emitter->onPress(std::move(args));
    }
  }

  void ApplyProps() noexcept {
    if (!button_ || !Props()) return;
    auto const& props = *Props();
    bool const link = props.link.value_or(false);
    const auto symbol = props.symbol.value_or("");
    if (symbol.empty()) {
      button_.Content(winrt::box_value(ToHString(props.title)));
    } else {
      auto icon = FontIcon{};
      icon.FontFamily(FontFamily(L"Segoe MDL2 Assets"));
      icon.FontSize(kUIFontSize);
      icon.Glyph(symbol == "pause" ? L"\xE769" : symbol == "play" ? L"\xE768" : L"\xE74D");
      button_.Content(icon);
    }
    hyperlink_.Content(winrt::box_value(ToHString(props.title)));
    button_.IsEnabled(Enabled(props.disabled));
    hyperlink_.IsEnabled(Enabled(props.disabled));
    button_.Visibility(link ? winrt::Microsoft::UI::Xaml::Visibility::Collapsed
                            : winrt::Microsoft::UI::Xaml::Visibility::Visible);
    hyperlink_.Visibility(link ? winrt::Microsoft::UI::Xaml::Visibility::Visible
                               : winrt::Microsoft::UI::Xaml::Visibility::Collapsed);
    if (link) {
      hyperlink_.Background(nullptr);
      hyperlink_.Foreground(nullptr);
      return;
    }
    if (props.compact.value_or(false)) {
      button_.Padding(winrt::Microsoft::UI::Xaml::Thickness{6, 2, 6, 2});
      button_.MinWidth(28.0);
      button_.MinHeight(28.0);
      compact_applied_ = true;
    } else if (compact_applied_) {
      // This branch only resets a previously compact button. A regular button
      // starts with untouched WinUI theme defaults.
      button_.Padding(default_padding_);
      button_.MinWidth(default_min_width_);
      button_.MinHeight(default_min_height_);
      compact_applied_ = false;
    }
    if (props.destructive.value_or(false)) {
      button_.Background(DestructiveBrush());
      button_.Foreground(SolidColorBrush(winrt::Windows::UI::Colors::White()));
    } else if (props.primary.value_or(false)) {
      button_.Background(AccentBrush());
      button_.Foreground(SolidColorBrush(winrt::Windows::UI::Colors::White()));
    } else {
      button_.Background(nullptr);
      button_.Foreground(nullptr);
    }
  }

  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  Grid container_{nullptr};
  Button button_{nullptr};
  HyperlinkButton hyperlink_{nullptr};
  winrt::Microsoft::UI::Xaml::Thickness default_padding_{};
  double default_min_width_ = 0;
  double default_min_height_ = 0;
  bool compact_applied_ = false;
};

struct SegmentedComponentView final
    : winrt::implements<SegmentedComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISegmentedControl<SegmentedComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    panel_ = StackPanel{};
    panel_.Orientation(Orientation::Horizontal);
    panel_.Spacing(4);
    island_.Content(panel_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISegmentedControlProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISegmentedControlProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISegmentedControl<SegmentedComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  struct ItemDefaults {
    ToggleButton button{nullptr};
    winrt::Microsoft::UI::Xaml::Thickness padding{};
    double min_height = 0;
  };

  void RestoreCompactItems() noexcept {
    for (auto const& item : compact_items_) {
      try {
        item.button.Padding(item.padding);
        item.button.MinHeight(item.min_height);
      } catch (...) {
      }
    }
    compact_items_.clear();
  }

  void ApplyProps() noexcept {
    if (!panel_ || !Props()) return;
    auto const& props = *Props();
    RestoreCompactItems();
    panel_.Children().Clear();
    auto selected_index = 0;
    if (props.selectedValue) {
      auto selected = std::find(props.labels.begin(), props.labels.end(), *props.selectedValue);
      if (selected != props.labels.end()) {
        selected_index = static_cast<int32_t>(std::distance(props.labels.begin(), selected));
      }
    }
    for (int32_t index = 0; index < static_cast<int32_t>(props.labels.size()); ++index) {
      auto item = ToggleButton{};
      item.FontSize(kUIFontSize);
      item.Content(winrt::box_value(ToHString(props.labels[static_cast<size_t>(index)])));
      item.IsChecked(index == selected_index);
      item.IsEnabled(Enabled(props.disabled));
      item.Click([this, index](auto const&, auto const&) {
        if (!Props() || index >= static_cast<int32_t>(Props()->labels.size())) return;
        if (auto emitter = EventEmitter()) {
          winrt::LiteLLMMenu::Codegen::LiteLLMWinUISegmentedControlEventEmitter::OnChange args;
          args.index = index;
          args.value = Props()->labels[static_cast<size_t>(index)];
          emitter->onChange(std::move(args));
        }
      });
      panel_.Children().Append(item);
      if (props.compact.value_or(false)) {
        ItemDefaults defaults{item, item.Padding(), item.MinHeight()};
        item.Padding(winrt::Microsoft::UI::Xaml::Thickness{7, 2, 7, 2});
        item.MinHeight(28.0);
        compact_items_.push_back(std::move(defaults));
      }
    }
  }

  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  StackPanel panel_{nullptr};
  std::vector<ItemDefaults> compact_items_;
};

struct PickerComponentView final
    : winrt::implements<PickerComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUIPicker<PickerComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    picker_ = ComboBox{};
    picker_.FontSize(kUIFontSize);
    picker_.SelectionChanged([this](auto const&, auto const&) {
      if (syncing_ || !Props()) return;
      const auto index = picker_.SelectedIndex();
      if (index < 0 || index >= static_cast<int32_t>(Props()->labels.size())) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUIPickerEventEmitter::OnChange args;
        args.index = index;
        args.value = Props()->labels[static_cast<size_t>(index)];
        emitter->onChange(std::move(args));
      }
    });
    island_.Content(picker_);
    island_view.Connect(island_.ContentIsland());
    default_padding_ = picker_.Padding();
    default_min_height_ = picker_.MinHeight();
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUIPickerProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUIPickerProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUIPicker<PickerComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  void ApplyProps() noexcept {
    if (!picker_ || !Props()) return;
    auto const& props = *Props();
    syncing_ = true;
    picker_.Items().Clear();
    int32_t selected_index = -1;
    for (int32_t index = 0; index < static_cast<int32_t>(props.labels.size()); ++index) {
      picker_.Items().Append(winrt::box_value(ToHString(props.labels[static_cast<size_t>(index)])));
      if (props.labels[static_cast<size_t>(index)] == props.selectedValue) selected_index = index;
    }
    picker_.SelectedIndex(selected_index);
    syncing_ = false;
    picker_.IsEnabled(Enabled(props.disabled));
    if (props.compact.value_or(false)) {
      picker_.Padding(winrt::Microsoft::UI::Xaml::Thickness{6, 1, 6, 1});
      picker_.MinHeight(28.0);
      compact_applied_ = true;
    } else if (compact_applied_) {
      picker_.Padding(default_padding_);
      picker_.MinHeight(default_min_height_);
      compact_applied_ = false;
    }
  }

  bool syncing_ = false;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  ComboBox picker_{nullptr};
  winrt::Microsoft::UI::Xaml::Thickness default_padding_{};
  double default_min_height_ = 0;
  bool compact_applied_ = false;
};

struct CheckboxComponentView final
    : winrt::implements<CheckboxComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUICheckbox<CheckboxComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    checkbox_ = CheckBox{};
    checkbox_.FontSize(kUIFontSize);
    checkbox_.Click([this](auto const&, auto const&) {
      if (syncing_) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUICheckboxEventEmitter::OnValueChange args;
        auto checked = checkbox_.IsChecked();
        args.value = checked && checked.Value();
        emitter->onValueChange(std::move(args));
      }
    });
    island_.Content(checkbox_);
    island_view.Connect(island_.ContentIsland());
    default_padding_ = checkbox_.Padding();
    default_min_height_ = checkbox_.MinHeight();
    ApplyProps(nullptr);
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUICheckboxProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUICheckboxProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUICheckbox<CheckboxComponentView>::UpdateProps(view, props, old_props);
    ApplyProps(old_props);
  }

 private:
  void ApplyProps(
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUICheckboxProps> const& old_props) noexcept {
    if (!checkbox_ || !Props()) return;
    auto const& props = *Props();
    const bool value_changed = !old_props || old_props->value != props.value;
    const bool disabled_changed = !old_props || old_props->disabled != props.disabled;
    const bool label_changed = !old_props || old_props->label != props.label;
    const bool label_visibility_changed = !old_props || old_props->labelVisible != props.labelVisible;
    const bool compact_changed = !old_props || old_props->compact != props.compact;
    syncing_ = true;
    if (label_changed || label_visibility_changed) {
      checkbox_.Content(props.labelVisible.value_or(true) ? winrt::box_value(ToHString(props.label)) : nullptr);
      winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(checkbox_, ToHString(props.label));
    }
    if (value_changed) checkbox_.IsChecked(props.value.value_or(false));
    syncing_ = false;
    if (disabled_changed) checkbox_.IsEnabled(Enabled(props.disabled));
    if (compact_changed && props.compact.value_or(false)) {
      checkbox_.Padding(winrt::Microsoft::UI::Xaml::Thickness{4, 1, 4, 1});
      checkbox_.MinHeight(24.0);
      compact_applied_ = true;
    } else if (compact_changed && compact_applied_) {
      checkbox_.Padding(default_padding_);
      checkbox_.MinHeight(default_min_height_);
      compact_applied_ = false;
    }
  }

  bool syncing_ = false;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  CheckBox checkbox_{nullptr};
  winrt::Microsoft::UI::Xaml::Thickness default_padding_{};
  double default_min_height_ = 0;
  bool compact_applied_ = false;
};

struct TableComponentView final
    : winrt::implements<TableComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITable<TableComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    root_ = Grid{};
    table_frame_ = Border{};
    table_ = Grid{};
    horizontal_scroller_ = ScrollViewer{};
    horizontal_scroller_.HorizontalAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    horizontal_scroller_.VerticalAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Stretch);
    horizontal_scroller_.HorizontalScrollBarVisibility(
        winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
    horizontal_scroller_.VerticalScrollBarVisibility(
        winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Disabled);
    auto header_row = winrt::Microsoft::UI::Xaml::Controls::RowDefinition{};
    auto body_row = winrt::Microsoft::UI::Xaml::Controls::RowDefinition{};
    header_row.Height(winrt::Microsoft::UI::Xaml::GridLengthHelper::Auto());
    table_.RowDefinitions().Append(header_row);
    table_.RowDefinitions().Append(body_row);

    header_ = Grid{};
    header_frame_ = Border{};
    header_frame_.Child(header_);
    header_frame_.Background(ThemeBrush(
        L"LayerFillColorDefaultBrush",
        winrt::Windows::UI::Color{255, 249, 249, 249}));
    header_frame_.BorderThickness(Thickness{0, 0, 0, 0});
    table_frame_.BorderBrush(ThemeBrush(
        L"ControlStrokeColorDefaultBrush",
        winrt::Windows::UI::Color{255, 140, 140, 140}));
    table_frame_.BorderThickness(Thickness{1, 1, 1, 1});
    table_frame_.Background(ThemeBrush(
        L"ControlFillColorDefaultBrush",
        winrt::Windows::UI::Color{255, 255, 255, 255}));
    list_ = ListView{};
    list_.SelectionMode(ListViewSelectionMode::Single);
    list_.IsItemClickEnabled(true);
    list_.HorizontalContentAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    list_.Padding(Thickness{0, 0, 0, 0});
    list_.Background(ThemeBrush(
        L"ControlFillColorDefaultBrush",
        winrt::Windows::UI::Color{255, 255, 255, 255}));
    list_.BorderThickness(Thickness{0, 0, 0, 0});
    ScrollViewer::SetHorizontalScrollBarVisibility(
        list_, winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Disabled);
    ScrollViewer::SetVerticalScrollBarVisibility(
        list_, winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
    list_.SelectionChanged([this](auto const&, auto const&) {
      if (syncing_ || !Props()) return;
      const auto index = list_.SelectedIndex();
      if (index < 0 || index >= static_cast<int32_t>(Props()->rowKeys.size())) return;
      if (IsSpanningKey(Props()->rowKeys[static_cast<size_t>(index)])) {
        RestoreControlledSelection();
        return;
      }
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITableEventEmitter::OnSelectionChange args;
        args.index = index;
        args.key = Props()->rowKeys[static_cast<size_t>(index)];
        emitter->onSelectionChange(std::move(args));
      }
    });
    list_.ItemClick([this](auto const&, auto const& args) {
      if (!Props()) return;
      uint32_t index = 0;
      if (!list_.Items().IndexOf(args.ClickedItem(), index) || index >= Props()->rowKeys.size()) return;
      if (IsSpanningKey(Props()->rowKeys[index])) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITableEventEmitter::OnSelectionChange event;
        event.index = static_cast<int32_t>(index);
        event.key = Props()->rowKeys[index];
        emitter->onSelectionChange(std::move(event));
      }
    });
    list_.DoubleTapped([this](auto const&, auto const&) {
      if (!Props()) return;
      const auto index = list_.SelectedIndex();
      if (index < 0 || index >= static_cast<int32_t>(Props()->rowKeys.size())) return;
      if (IsSpanningKey(Props()->rowKeys[static_cast<size_t>(index)])) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITableEventEmitter::OnRowDoublePress args;
        args.index = index;
        args.key = Props()->rowKeys[static_cast<size_t>(index)];
        emitter->onRowDoublePress(std::move(args));
      }
    });

    Grid::SetRow(header_frame_, 0);
    Grid::SetRow(list_, 1);
    table_.Children().Append(header_frame_);
    table_.Children().Append(list_);
    horizontal_scroller_.Content(table_);
    table_frame_.Child(horizontal_scroller_);
    root_.Children().Append(table_frame_);
    island_.Content(root_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITableProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITableProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITable<TableComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  void AddColumns(Grid const& grid, std::vector<float> const& widths, size_t count) noexcept {
    grid.ColumnDefinitions().Clear();
    for (size_t index = 0; index < count; ++index) {
      auto column = winrt::Microsoft::UI::Xaml::Controls::ColumnDefinition{};
      column.Width(winrt::Microsoft::UI::Xaml::GridLengthHelper::FromPixels(ColumnWidth(widths, index)));
      grid.ColumnDefinitions().Append(column);
    }
  }

  double TableWidth(std::vector<float> const& widths, size_t count) const noexcept {
    double width = 0;
    for (size_t index = 0; index < count; ++index) {
      width += ColumnWidth(widths, index);
    }
    return width;
  }

  bool IsSpanningKey(std::string const& key) const noexcept {
    if (!Props() || !Props()->spanningRowKeys) return false;
    auto const& spanning_row_keys = *Props()->spanningRowKeys;
    return std::find(spanning_row_keys.begin(), spanning_row_keys.end(), key) != spanning_row_keys.end();
  }

  void RestoreControlledSelection() noexcept {
    if (!Props()) return;
    auto const& props = *Props();
    auto selected = std::find(props.rowKeys.begin(), props.rowKeys.end(), props.selectedKey);
    const auto selected_index = selected == props.rowKeys.end()
        ? -1
        : static_cast<int32_t>(std::distance(props.rowKeys.begin(), selected));
    syncing_ = true;
    list_.SelectedIndex(selected_index);
    syncing_ = false;
  }

  void ApplyProps() noexcept {
    if (!root_ || !Props()) return;
    auto const& props = *Props();
    const auto disabled_row_keys = props.disabledRowKeys.value_or(std::vector<std::string>{});
    const auto secondary_cell_keys = props.secondaryCellKeys.value_or(std::vector<std::string>{});
    const auto spanning_row_keys = props.spanningRowKeys.value_or(std::vector<std::string>{});
    const auto column_count = props.columnLabels.size();
    const bool columns_changed = !has_applied_ || column_labels_ != props.columnLabels || column_widths_ != props.columnWidths || compact_ != props.compact;
    const bool rows_changed = !has_applied_ || row_keys_ != props.rowKeys || cells_ != props.cells || alternating_rows_ != props.alternatingRows || disabled_row_keys_ != disabled_row_keys || secondary_cell_keys_ != secondary_cell_keys || spanning_row_keys_ != spanning_row_keys || compact_ != props.compact;
    const bool selection_changed = !has_applied_ || selected_key_ != props.selectedKey;
    const bool was_following_bottom = props.followBottom.value_or(false) && rows_changed
        ? (!has_applied_ || ListIsFollowingBottom(list_))
        : false;
    syncing_ = true;

    if (columns_changed) {
      table_.MinWidth(TableWidth(props.columnWidths, column_count));
      header_.Children().Clear();
      AddColumns(header_, props.columnWidths, column_count);
      for (size_t column_index = 0; column_index < column_count; ++column_index) {
        auto label = TextBlock{};
        label.Text(ToHString(props.columnLabels[column_index]));
        const double vertical_margin = props.compact.value_or(false) ? 2.0 : 5.0;
        label.Margin({8, vertical_margin, 8, vertical_margin});
        label.FontSize(kUIFontSize);
        label.FontWeight(winrt::Windows::UI::Text::FontWeights::SemiBold());
        label.Foreground(SecondaryTextBrush());
        label.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
        Grid::SetColumn(label, static_cast<int32_t>(column_index));
        header_.Children().Append(label);
      }
    }

    if (rows_changed) {
      list_.Items().Clear();
      for (size_t row_index = 0; row_index < props.rowKeys.size(); ++row_index) {
        auto row = Grid{};
        row.MinHeight(props.compact.value_or(false) ? 22.0 : 28.0);
        const bool disabled = std::find(disabled_row_keys.begin(), disabled_row_keys.end(), props.rowKeys[row_index]) != disabled_row_keys.end();
        if (props.alternatingRows.value_or(false) && row_index % 2 == 1) {
          row.Background(AlternatingRowBrush());
        } else {
          row.Background(ThemeBrush(
              L"ControlFillColorDefaultBrush",
              winrt::Windows::UI::Color{255, 255, 255, 255}));
        }
        AddColumns(row, props.columnWidths, column_count);
        const bool spanning = std::find(spanning_row_keys.begin(), spanning_row_keys.end(), props.rowKeys[row_index]) != spanning_row_keys.end();
        if (spanning) {
          const auto cell_index = row_index * column_count;
          auto label = TextBlock{};
          label.FontSize(kUIFontSize);
          label.FontWeight(winrt::Windows::UI::Text::FontWeights::Normal());
          label.Foreground(SecondaryTextBrush());
          label.Text(ToHString(cell_index < props.cells.size() ? props.cells[cell_index] : ""));
          if (!label.Text().empty()) ToolTipService::SetToolTip(label, winrt::box_value(label.Text()));
          const double vertical_margin = props.compact.value_or(false) ? 4.0 : 7.0;
          label.Margin({8, vertical_margin, 8, vertical_margin});
          label.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
          Grid::SetColumnSpan(label, static_cast<int32_t>(std::max<size_t>(1, column_count)));
          row.Children().Append(label);
        } else {
          for (size_t column_index = 0; column_index < column_count; ++column_index) {
            const auto cell_index = row_index * column_count + column_index;
            auto cell = TextBlock{};
            cell.FontSize(kUIFontSize);
            cell.Text(ToHString(cell_index < props.cells.size() ? props.cells[cell_index] : ""));
            if (!cell.Text().empty()) ToolTipService::SetToolTip(cell, winrt::box_value(cell.Text()));
            const double vertical_margin = props.compact.value_or(false) ? 2.0 : 5.0;
            cell.Margin({8, vertical_margin, 8, vertical_margin});
            cell.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
            const auto cell_key = props.rowKeys[row_index] + "\x1f" + std::to_string(column_index);
            const bool secondary = std::find(secondary_cell_keys.begin(), secondary_cell_keys.end(), cell_key) != secondary_cell_keys.end();
            if (disabled || secondary) cell.Foreground(SecondaryTextBrush());
            Grid::SetColumn(cell, static_cast<int32_t>(column_index));
            row.Children().Append(cell);
          }
        }
        list_.Items().Append(row);
      }
    }

    int32_t selected_index = -1;
    auto selected = std::find(props.rowKeys.begin(), props.rowKeys.end(), props.selectedKey);
    if (selected != props.rowKeys.end()) selected_index = static_cast<int32_t>(std::distance(props.rowKeys.begin(), selected));
    if (selection_changed || rows_changed || list_.SelectedIndex() != selected_index) list_.SelectedIndex(selected_index);
    column_labels_ = props.columnLabels;
    column_widths_ = props.columnWidths;
    row_keys_ = props.rowKeys;
    cells_ = props.cells;
    selected_key_ = props.selectedKey;
    alternating_rows_ = props.alternatingRows;
    compact_ = props.compact;
    disabled_row_keys_ = disabled_row_keys;
    secondary_cell_keys_ = secondary_cell_keys;
    spanning_row_keys_ = spanning_row_keys;
    if (props.followBottom.value_or(false) && rows_changed && was_following_bottom && !props.rowKeys.empty()) {
      list_.ScrollIntoView(list_.Items().GetAt(static_cast<uint32_t>(props.rowKeys.size() - 1)));
    }
    has_applied_ = true;
    syncing_ = false;
  }

  bool syncing_ = false;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  Grid root_{nullptr};
  Border table_frame_{nullptr};
  Grid table_{nullptr};
  Grid header_{nullptr};
  Border header_frame_{nullptr};
  ListView list_{nullptr};
  ScrollViewer horizontal_scroller_{nullptr};
  bool has_applied_ = false;
  std::vector<std::string> column_labels_;
  std::vector<float> column_widths_;
  std::vector<std::string> row_keys_;
  std::vector<std::string> cells_;
  std::string selected_key_;
  std::optional<bool> alternating_rows_;
  std::optional<bool> compact_;
  std::vector<std::string> disabled_row_keys_;
  std::vector<std::string> secondary_cell_keys_;
  std::vector<std::string> spanning_row_keys_;
};

struct TextEditorComponentView final
    : winrt::implements<TextEditorComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITextEditor<TextEditorComponentView> {
  struct ViewportState final {
    double horizontal_offset = 0.0;
    double vertical_offset = 0.0;
    int32_t selection_start = 0;
    int32_t selection_length = 0;
    bool follows_bottom = true;
  };

  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    editor_ = TextBox{};
    editor_.FontSize(kUIFontSize);
    editor_.AcceptsReturn(true);
    editor_.TextChanged([this](auto const&, auto const&) {
      if (syncing_) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextEditorEventEmitter::OnChangeText args;
        args.text = winrt::to_string(editor_.Text());
        emitter->onChangeText(std::move(args));
      }
    });
    island_.Content(editor_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextEditorProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextEditorProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITextEditor<TextEditorComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  void ApplyProps() noexcept {
    if (!editor_ || !Props()) return;
    auto const& props = *Props();
    auto const next_document_key = props.documentKey.value_or("");
    auto const document_changed = active_document_key_ != next_document_key;
    syncing_ = true;
    auto const value = ToHString(props.value);
    if (document_changed && !active_document_key_.empty()) {
      if (auto const viewer = FindTextEditorScrollViewer(editor_)) {
        viewport_states_[active_document_key_] = ViewportState{
            viewer.HorizontalOffset(),
            viewer.VerticalOffset(),
            editor_.SelectionStart(),
            editor_.SelectionLength(),
            viewer.ScrollableHeight() - viewer.VerticalOffset() <= 4.0,
        };
      }
    }
    if (editor_.Text() != value) {
      editor_.ApplyTemplate();
      editor_.UpdateLayout();
      auto const viewer = FindTextEditorScrollViewer(editor_);
      auto const existing_state = document_changed
          ? viewport_states_.find(next_document_key)
          : viewport_states_.end();
      auto const previous_horizontal_offset = existing_state != viewport_states_.end()
          ? existing_state->second.horizontal_offset
          : document_changed ? 0.0 : viewer ? viewer.HorizontalOffset() : 0.0;
      auto const previous_vertical_offset = existing_state != viewport_states_.end()
          ? existing_state->second.vertical_offset
          : document_changed ? 0.0 : viewer ? viewer.VerticalOffset() : 0.0;
      auto const follow_bottom = existing_state != viewport_states_.end()
          ? existing_state->second.follows_bottom
          : document_changed ||
                (viewer && viewer.ScrollableHeight() - previous_vertical_offset <= 4.0);
      auto const previous_selection_start = existing_state != viewport_states_.end()
          ? existing_state->second.selection_start
          : document_changed ? 0 : editor_.SelectionStart();
      auto const previous_selection_length = existing_state != viewport_states_.end()
          ? existing_state->second.selection_length
          : document_changed ? 0 : editor_.SelectionLength();

      editor_.Text(value);
      // TextBox selection offsets are UTF-16 code units, not UTF-8 bytes.
      auto const text_length = static_cast<int32_t>(editor_.Text().size());
      auto const selection_start = std::clamp(previous_selection_start, 0, text_length);
      auto const selection_length = std::clamp(
          previous_selection_length, 0, text_length - selection_start);
      editor_.SelectionStart(selection_start);
      editor_.SelectionLength(selection_length);
      editor_.UpdateLayout();

      if (viewer) {
        viewer.UpdateLayout();
        auto const horizontal_offset = std::clamp(
            previous_horizontal_offset, 0.0, viewer.ScrollableWidth());
        auto const vertical_offset = follow_bottom
            ? viewer.ScrollableHeight()
            : std::clamp(previous_vertical_offset, 0.0, viewer.ScrollableHeight());
        auto const horizontal_reference = winrt::box_value(horizontal_offset)
            .as<winrt::Windows::Foundation::IReference<double>>();
        auto const vertical_reference = winrt::box_value(vertical_offset)
            .as<winrt::Windows::Foundation::IReference<double>>();
        viewer.ChangeView(
            horizontal_reference,
            vertical_reference,
            nullptr,
            true);
      }
    } else if (document_changed) {
      auto const existing_state = viewport_states_.find(next_document_key);
      auto const target_state = existing_state != viewport_states_.end()
          ? existing_state->second
          : ViewportState{};
      editor_.ApplyTemplate();
      editor_.UpdateLayout();
      auto const text_length = static_cast<int32_t>(editor_.Text().size());
      auto const selection_start = std::clamp(target_state.selection_start, 0, text_length);
      auto const selection_length = std::clamp(
          target_state.selection_length, 0, text_length - selection_start);
      editor_.SelectionStart(selection_start);
      editor_.SelectionLength(selection_length);
      if (auto const viewer = FindTextEditorScrollViewer(editor_)) {
        viewer.UpdateLayout();
        auto const horizontal_offset = std::clamp(
            target_state.horizontal_offset, 0.0, viewer.ScrollableWidth());
        auto const vertical_offset = target_state.follows_bottom
            ? viewer.ScrollableHeight()
            : std::clamp(target_state.vertical_offset, 0.0, viewer.ScrollableHeight());
        auto const horizontal_reference = winrt::box_value(horizontal_offset)
            .as<winrt::Windows::Foundation::IReference<double>>();
        auto const vertical_reference = winrt::box_value(vertical_offset)
            .as<winrt::Windows::Foundation::IReference<double>>();
        viewer.ChangeView(horizontal_reference, vertical_reference, nullptr, true);
      }
    }
    syncing_ = false;
    active_document_key_ = next_document_key;
    editor_.IsReadOnly(props.readOnly.value_or(false));
    editor_.TextWrapping(props.wrap
        ? winrt::Microsoft::UI::Xaml::TextWrapping::Wrap
        : winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
    editor_.HorizontalScrollBarVisibility(
        props.wrap
            ? winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Disabled
            : winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
    editor_.VerticalScrollBarVisibility(
        winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
  }

  bool syncing_ = false;
  std::string active_document_key_;
  std::unordered_map<std::string, ViewportState> viewport_states_;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  TextBox editor_{nullptr};
};

struct SecureTextEditorComponentView final
    : winrt::implements<SecureTextEditorComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextEditor<SecureTextEditorComponentView> {
  SecureTextEditorComponentView() = default;

  ~SecureTextEditorComponentView() {
    InvalidateLifecycle();
    try {
      if (editor_) {
        if (text_changed_token_.value != 0) editor_.TextChanged(text_changed_token_);
        if (loaded_token_.value != 0) editor_.Loaded(loaded_token_);
        if (unloaded_token_.value != 0) editor_.Unloaded(unloaded_token_);
      }
    } catch (...) {
    }
  }

  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    try {
      island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
      editor_ = TextBox{};
      editor_.FontSize(kUIFontSize);
      editor_.AcceptsReturn(true);
      editor_.FontFamily(winrt::Microsoft::UI::Xaml::Media::FontFamily(L"Consolas"));
      editor_.TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
      editor_.HorizontalScrollBarVisibility(
          winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
      editor_.VerticalScrollBarVisibility(
          winrt::Microsoft::UI::Xaml::Controls::ScrollBarVisibility::Auto);
      editor_.MaxLength(static_cast<int32_t>(kSecureEditorMaximumBytes));
      editor_.IsReadOnly(true);
      dispatcher_ = winrt::Microsoft::UI::Dispatching::DispatcherQueue::GetForCurrentThread();
      auto weak_self = get_weak();
      text_changed_token_ = editor_.TextChanged([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get(); self && !self->syncing_) self->ScheduleStage();
      });
      loaded_token_ = editor_.Loaded([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->ResumeAfterLoad();
      });
      unloaded_token_ = editor_.Unloaded([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->SuspendForUnload();
      });
      island_.Content(editor_);
      island_view.Connect(island_.ContentIsland());
    } catch (...) {
      EmitState(kSecureEditorInitialRevision, "error", "initialize_failed");
    }
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextEditorProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextEditorProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextEditor<SecureTextEditorComponentView>::UpdateProps(
        view, props, old_props);
    ApplyProps(old_props);
  }

  void UpdateEventEmitter(
      std::shared_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextEditorEventEmitter> const& emitter) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextEditor<SecureTextEditorComponentView>::UpdateEventEmitter(
        emitter);
    if (emitter && !last_status_.empty()) EmitState(last_revision_, last_status_, last_error_);
  }

  void SuspendForUnload() noexcept {
    if (!lifecycle_) return;
    if (!active_editor_token_.empty() && !lifecycle_->terminal.load(std::memory_order_acquire) &&
        last_status_ != "loading") {
      try {
        loaded_document_ = winrt::to_string(editor_.Text());
      } catch (...) {
        lifecycle_->terminal.store(true, std::memory_order_release);
        EmitState(last_revision_, "error", "invalid_text");
      }
    }
    lifecycle_->attached.store(false, std::memory_order_release);
    lifecycle_->debounce_serial.fetch_add(1, std::memory_order_acq_rel);
    try {
      if (editor_) editor_.IsReadOnly(true);
    } catch (...) {
    }
  }

 private:
  void ResumeAfterLoad() noexcept {
    if (!lifecycle_ || !lifecycle_->alive.load(std::memory_order_acquire)) return;
    lifecycle_->attached.store(true, std::memory_order_release);
    try {
      if (!editor_) return;
      if (loaded_document_ && !PresentLoadedDocument()) return;
      const bool can_edit = loaded_document_.has_value() && !active_editor_token_.empty() &&
          !lifecycle_->terminal.load(std::memory_order_acquire) && last_status_ != "loading";
      editor_.IsReadOnly(!can_edit);
      if (can_edit && last_status_ == "dirty" &&
          !lifecycle_->staging.load(std::memory_order_acquire)) {
        BeginStage(lifecycle_->generation.load(std::memory_order_acquire));
      } else if (!last_status_.empty()) {
        EmitState(last_revision_, last_status_, last_error_);
      }
    } catch (...) {
    }
  }

  void InvalidateLifecycle() noexcept {
    if (!lifecycle_) return;
    lifecycle_->attached.store(false, std::memory_order_release);
    lifecycle_->alive.store(false, std::memory_order_release);
    lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel);
    lifecycle_->edit_serial.fetch_add(1, std::memory_order_acq_rel);
    lifecycle_->debounce_serial.fetch_add(1, std::memory_order_acq_rel);
  }

  bool IsCurrent(
      std::shared_ptr<SecureEditorLifecycle> const& lifecycle,
      uint64_t generation) const noexcept {
    return lifecycle &&
        lifecycle->alive.load(std::memory_order_acquire) &&
        lifecycle->generation.load(std::memory_order_acquire) == generation;
  }

  bool PresentLoadedDocument() noexcept {
    if (!lifecycle_->attached.load(std::memory_order_acquire) || !loaded_document_) return true;
    try {
      auto wide_text = ToHString(*loaded_document_);
      syncing_ = true;
      if (editor_.Text() != wide_text) editor_.Text(wide_text);
      syncing_ = false;
      editor_.IsReadOnly(false);
      return true;
    } catch (...) {
      syncing_ = false;
      lifecycle_->terminal.store(true, std::memory_order_release);
      try {
        editor_.IsReadOnly(true);
      } catch (...) {
      }
      EmitState(kSecureEditorInitialRevision, "error", "read_failed");
      return false;
    }
  }

  void ApplyProps(
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextEditorProps> const& old_props) noexcept {
    if (!editor_ || !Props()) return;
    auto const& props = *Props();
    winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(
        editor_, ToHString(props.language.empty() ? "Source editor" : props.language + " source editor"));
    const bool token_changed = !old_props || old_props->editorToken != props.editorToken;
    if (!token_changed) return;

    lifecycle_->debounce_serial.fetch_add(1, std::memory_order_acq_rel);
    const auto generation = lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel) + 1;
    lifecycle_->edit_serial.store(0, std::memory_order_release);
    lifecycle_->staging.store(false, std::memory_order_release);
    lifecycle_->terminal.store(false, std::memory_order_release);
    load_recovery_attempted_ = false;
    stage_recovery_attempted_ = false;
    active_editor_token_ = props.editorToken;
    loaded_document_.reset();
    last_revision_ = 0;

    if (lifecycle_->attached.load(std::memory_order_acquire)) {
      syncing_ = true;
      editor_.Text(L"");
      syncing_ = false;
      editor_.IsReadOnly(true);
    }

    if (props.editorToken.empty() || props.editorToken.size() > 256) {
      lifecycle_->terminal.store(true, std::memory_order_release);
      EmitState(kSecureEditorInitialRevision, "error", "invalid_token");
      return;
    }

    EmitState(kSecureEditorInitialRevision, "loading", "");
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    auto token = active_editor_token_;
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, token = std::move(token)]() mutable {
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation) {
          return;
        }
        auto text = LiteLLMMenu::CoreIPCBridge::Shared().ReadEditorDocument(token);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, text = std::move(text)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) self->FinishRead(generation, std::move(text));
        });
      }).detach();
    } catch (...) {
      lifecycle_->terminal.store(true, std::memory_order_release);
      EmitState(kSecureEditorInitialRevision, "error", "read_failed");
    }
  }

  void FinishRead(uint64_t generation, std::optional<std::string> text) noexcept {
    if (!IsCurrent(lifecycle_, generation)) return;
    if (!text) {
      RecoverInitialRead(generation, active_editor_token_);
      return;
    }
    loaded_document_ = std::move(text);
    last_revision_ = 0;
    if (!PresentLoadedDocument()) return;
    EmitState(kSecureEditorInitialRevision, "ready", "");
  }

  void RecoverInitialRead(uint64_t generation, std::string const& failed_token) noexcept {
    if (!IsCurrent(lifecycle_, generation) || load_recovery_attempted_ ||
        failed_token != active_editor_token_) {
      lifecycle_->terminal.store(true, std::memory_order_release);
      if (lifecycle_->attached.load(std::memory_order_acquire)) editor_.IsReadOnly(true);
      EmitState(kSecureEditorInitialRevision, "error", "read_failed");
      return;
    }
    load_recovery_attempted_ = true;
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, failed_token] {
        auto refreshed = LiteLLMMenu::CoreIPCBridge::Shared().RefreshEditorDocument(failed_token);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, failed_token, refreshed = std::move(refreshed)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) {
            if (!refreshed || failed_token != self->active_editor_token_) {
              self->lifecycle_->terminal.store(true, std::memory_order_release);
              if (self->lifecycle_->attached.load(std::memory_order_acquire)) self->editor_.IsReadOnly(true);
              self->EmitState(kSecureEditorInitialRevision, "error", "read_failed");
              return;
            }
            self->active_editor_token_ = std::move(refreshed->editor_token);
            self->loaded_document_ = std::move(refreshed->text);
            self->last_revision_ = 0;
            if (!self->PresentLoadedDocument()) return;
            self->EmitState(kSecureEditorInitialRevision, "ready", "");
          }
        });
      }).detach();
    } catch (...) {
      lifecycle_->terminal.store(true, std::memory_order_release);
      if (lifecycle_->attached.load(std::memory_order_acquire)) editor_.IsReadOnly(true);
      EmitState(kSecureEditorInitialRevision, "error", "read_failed");
    }
  }

  void ScheduleStage() noexcept {
    if (!Props() || !lifecycle_->alive.load(std::memory_order_acquire) ||
        !lifecycle_->attached.load(std::memory_order_acquire) ||
        lifecycle_->terminal.load(std::memory_order_acquire)) {
      return;
    }
    const auto generation = lifecycle_->generation.load(std::memory_order_acquire);
    // A successful recovery belongs to the previous edit burst. Allow one
    // bounded capability refresh for each new user edit burst, while still
    // preventing an endlessly failing stage from spinning.
    stage_recovery_attempted_ = false;
    lifecycle_->edit_serial.fetch_add(1, std::memory_order_acq_rel);
    const auto serial = lifecycle_->debounce_serial.fetch_add(1, std::memory_order_acq_rel) + 1;
    EmitState(last_revision_, "dirty", "");
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, serial] {
        std::this_thread::sleep_for(kSecureEditorDebounce);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            !lifecycle->attached.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation ||
            lifecycle->debounce_serial.load(std::memory_order_acquire) != serial || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, serial] {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              !lifecycle->attached.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation ||
              lifecycle->debounce_serial.load(std::memory_order_acquire) != serial) {
            return;
          }
          if (auto self = weak_self.get()) self->BeginStage(generation);
        });
      }).detach();
    } catch (...) {
      lifecycle_->terminal.store(false, std::memory_order_release);
      editor_.IsReadOnly(false);
      EmitState(last_revision_, "error", "stage_failed");
    }
  }

  void BeginStage(uint64_t generation) noexcept {
    if (!IsCurrent(lifecycle_, generation) || !Props() ||
        !lifecycle_->attached.load(std::memory_order_acquire) ||
        lifecycle_->terminal.load(std::memory_order_acquire)) {
      return;
    }
    bool expected = false;
    if (!lifecycle_->staging.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) return;
    const auto staged_serial = lifecycle_->edit_serial.load(std::memory_order_acquire);

    std::string text;
    try {
      text = winrt::to_string(editor_.Text());
    } catch (...) {
      lifecycle_->staging.store(false, std::memory_order_release);
      lifecycle_->terminal.store(true, std::memory_order_release);
      editor_.IsReadOnly(true);
      EmitState(last_revision_, "error", "invalid_text");
      return;
    }
    if (text.size() > kSecureEditorMaximumBytes) {
      lifecycle_->staging.store(false, std::memory_order_release);
      lifecycle_->terminal.store(true, std::memory_order_release);
      editor_.IsReadOnly(true);
      EmitState(last_revision_, "error", "invalid_text");
      return;
    }
    loaded_document_ = text;

    EmitState(last_revision_, "saving", "");
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    auto token = active_editor_token_;
    try {
      std::thread([
          lifecycle,
          dispatcher,
          weak_self,
          generation,
          staged_serial,
          token = std::move(token),
          text = std::move(text)]() mutable {
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation) {
          return;
        }
        auto result = LiteLLMMenu::CoreIPCBridge::Shared().StageEditorDocumentWithReplacement(token, text);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, staged_serial, result = std::move(result)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) {
            self->FinishStage(generation, staged_serial, std::move(result));
          }
        });
      }).detach();
    } catch (...) {
      lifecycle_->staging.store(false, std::memory_order_release);
      lifecycle_->terminal.store(false, std::memory_order_release);
      editor_.IsReadOnly(false);
      EmitState(last_revision_, "error", "stage_failed");
    }
  }

  void FinishStage(
      uint64_t generation,
      uint64_t staged_serial,
      std::optional<LiteLLMMenu::CoreIPCBridge::EditorStageResult> result) noexcept {
    if (!IsCurrent(lifecycle_, generation)) return;
    lifecycle_->staging.store(false, std::memory_order_release);
    if (!result || result->revision < 0 ||
        result->revision > std::numeric_limits<int32_t>::max() ||
        std::floor(result->revision) != result->revision || result->editor_token.empty() ||
        result->editor_token.size() > 256) {
      RecoverStage(generation, active_editor_token_);
      return;
    }
    // Core rotates the capability after each stage. Keep the replacement token
    // native-only so the user can continue editing without a React re-render.
    active_editor_token_ = std::move(result->editor_token);
    last_revision_ = static_cast<int32_t>(result->revision);
    if (lifecycle_->edit_serial.load(std::memory_order_acquire) != staged_serial) {
      lifecycle_->debounce_serial.fetch_add(1, std::memory_order_acq_rel);
      EmitState(last_revision_, "dirty", "");
      BeginStage(generation);
    } else {
      EmitState(last_revision_, "saved", "");
    }
  }

  void RecoverStage(uint64_t generation, std::string const& failed_token) noexcept {
    if (!IsCurrent(lifecycle_, generation) || stage_recovery_attempted_ ||
        failed_token != active_editor_token_) {
      lifecycle_->terminal.store(false, std::memory_order_release);
      if (lifecycle_->attached.load(std::memory_order_acquire)) editor_.IsReadOnly(false);
      EmitState(last_revision_, "error", "stage_failed");
      return;
    }
    stage_recovery_attempted_ = true;
    lifecycle_->staging.store(true, std::memory_order_release);
    EmitState(last_revision_, "saving", "");
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, failed_token] {
        auto refreshed = LiteLLMMenu::CoreIPCBridge::Shared().RefreshEditorDocument(failed_token);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, failed_token, refreshed = std::move(refreshed)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) {
            self->lifecycle_->staging.store(false, std::memory_order_release);
            if (!refreshed || failed_token != self->active_editor_token_) {
              self->lifecycle_->terminal.store(false, std::memory_order_release);
              if (self->lifecycle_->attached.load(std::memory_order_acquire)) self->editor_.IsReadOnly(false);
              self->EmitState(self->last_revision_, "error", "stage_failed");
              return;
            }
            // The Core read above only authorizes the fresh capability. Do not
            // assign its disk text: the TextBox is the native owner of edits
            // made while the prior capability expired.
            self->active_editor_token_ = std::move(refreshed->editor_token);
            if (!self->lifecycle_->attached.load(std::memory_order_acquire)) {
              self->EmitState(self->last_revision_, "dirty", "");
              return;
            }
            self->BeginStage(generation);
          }
        });
      }).detach();
    } catch (...) {
      lifecycle_->staging.store(false, std::memory_order_release);
      lifecycle_->terminal.store(false, std::memory_order_release);
      if (lifecycle_->attached.load(std::memory_order_acquire)) editor_.IsReadOnly(false);
      EmitState(last_revision_, "error", "stage_failed");
    }
  }

  void EmitState(int32_t revision, std::string status, std::string error) noexcept {
    if (!lifecycle_->alive.load(std::memory_order_acquire)) return;
    last_revision_ = revision;
    last_status_ = status;
    last_error_ = error;
    if (!lifecycle_->attached.load(std::memory_order_acquire)) return;
    try {
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextEditorEventEmitter::OnEditorState args;
        args.revision = revision;
        args.status = std::move(status);
        args.error = std::move(error);
        emitter->onEditorState(std::move(args));
      }
    } catch (...) {
    }
  }

  bool syncing_ = false;
  std::shared_ptr<SecureEditorLifecycle> lifecycle_{std::make_shared<SecureEditorLifecycle>()};
  winrt::Microsoft::UI::Dispatching::DispatcherQueue dispatcher_{nullptr};
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  TextBox editor_{nullptr};
  std::string active_editor_token_;
  std::optional<std::string> loaded_document_;
  bool load_recovery_attempted_ = false;
  bool stage_recovery_attempted_ = false;
  int32_t last_revision_ = 0;
  std::string last_status_;
  std::string last_error_;
  winrt::event_token text_changed_token_{};
  winrt::event_token loaded_token_{};
  winrt::event_token unloaded_token_{};
};

struct SplitterComponentView final
    : winrt::implements<SplitterComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISplitView<SplitterComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    root_ = Grid{};
    splitter_ = Thumb{};
    splitter_.HorizontalAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    splitter_.VerticalAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Stretch);
    splitter_.DragDelta([this](auto const&, auto const& args) {
      if (syncing_ || !Props() || Props()->disabled.value_or(false)) return;
      const auto next_width = ClampPaneWidth(current_pane_width_ + static_cast<float>(args.HorizontalChange()));
      current_pane_width_ = next_width;
      EmitPaneWidth(next_width);
    });
    root_.Children().Append(splitter_);
    island_.Content(root_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISplitViewProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISplitViewProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISplitView<SplitterComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  float ClampPaneWidth(float width) const noexcept {
    if (!Props()) return width;
    const auto min_width = Props()->minPaneWidth;
    const auto max_width = std::max(min_width, Props()->maxPaneWidth);
    return std::clamp(width, min_width, max_width);
  }

  void EmitPaneWidth(float width) noexcept {
    if (auto emitter = EventEmitter()) {
      winrt::LiteLLMMenu::Codegen::LiteLLMWinUISplitViewEventEmitter::OnPaneWidthChange args;
      args.width = width;
      emitter->onPaneWidthChange(std::move(args));
    }
  }

  void ApplyProps() noexcept {
    if (!root_ || !Props()) return;
    auto const& props = *Props();
    syncing_ = true;
    current_pane_width_ = ClampPaneWidth(props.paneWidth);
    splitter_.Visibility(
        props.paneOpen
            ? winrt::Microsoft::UI::Xaml::Visibility::Visible
            : winrt::Microsoft::UI::Xaml::Visibility::Collapsed);
    splitter_.IsEnabled(Enabled(props.disabled));
    syncing_ = false;
  }

  bool syncing_ = false;
  float current_pane_width_ = 0;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  Grid root_{nullptr};
  Thumb splitter_{nullptr};
};

struct TextInputComponentView final
    : winrt::implements<TextInputComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITextInput<TextInputComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    text_box_ = TextBox{};
    text_box_.FontSize(kUIFontSize);
    // React Native owns the outer field height.  Keep the native editor's
    // content box centered with enough vertical room for the shared 13pt
    // font instead of inheriting template padding that can clip the glyphs.
    text_box_.MinHeight(30.0);
    text_box_.Padding(winrt::Microsoft::UI::Xaml::Thickness{8, 0, 8, 0});
    text_box_.VerticalContentAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Center);
    text_box_.TextChanged([this](auto const&, auto const&) {
      if (syncing_) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputEventEmitter::OnChangeText args;
        args.text = winrt::to_string(text_box_.Text());
        emitter->onChangeText(std::move(args));
      }
    });
    text_box_.LostFocus([this](auto const&, auto const&) {
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputEventEmitter::OnBlur args;
        emitter->onBlur(std::move(args));
      }
    });
    text_box_.KeyDown([this](auto const&, winrt::Microsoft::UI::Xaml::Input::KeyRoutedEventArgs const& args) {
      if (args.Key() != winrt::Windows::System::VirtualKey::Enter || (Props() && Props()->multiline.value_or(false))) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputEventEmitter::OnSubmitEditing event;
        event.text = winrt::to_string(text_box_.Text());
        emitter->onSubmitEditing(std::move(event));
      }
    });
    island_.Content(text_box_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps(nullptr);
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUITextInput<TextInputComponentView>::UpdateProps(view, props, old_props);
    ApplyProps(old_props);
  }

 private:
  void ApplyProps(
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUITextInputProps> const& old_props) noexcept {
    if (!text_box_ || !Props()) return;
    auto const& props = *Props();
    // Ignore unrelated Fabric commits while an editor has locally accepted
    // text.  Reapplying a stale controlled value on every prop update causes
    // a visible flash and moves the caret under active typing.
    const bool text_changed = !old_props || old_props->value != props.value;
    const bool placeholder_changed = !old_props || old_props->placeholder != props.placeholder;
    const bool multiline_changed = !old_props || old_props->multiline != props.multiline;
    const bool keyboard_type_changed = !old_props || old_props->keyboardType != props.keyboardType;
    const bool secure_text_changed = !old_props || old_props->secureTextEntry != props.secureTextEntry;
    const bool disabled_changed = !old_props || old_props->disabled != props.disabled;
    auto const value = ToHString(props.value.value_or(""));
    if (text_changed && text_box_.Text() != value) {
      syncing_ = true;
      text_box_.Text(value);
      syncing_ = false;
    }
    if (placeholder_changed) {
      text_box_.PlaceholderText(ToHString(props.placeholder.value_or("")));
    }
    if (multiline_changed) {
      text_box_.AcceptsReturn(props.multiline.value_or(false));
      text_box_.TextWrapping(props.multiline.value_or(false)
          ? winrt::Microsoft::UI::Xaml::TextWrapping::Wrap
          : winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
    }
    if (keyboard_type_changed) {
      ApplyKeyboardType(text_box_, props.keyboardType);
    }
    if (secure_text_changed) {
      text_box_.PasswordChar(props.secureTextEntry.value_or(false) ? L'\x25cf' : L'\0');
    }
    if (disabled_changed) {
      text_box_.IsEnabled(Enabled(props.disabled));
    }
  }

  bool syncing_ = false;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  TextBox text_box_{nullptr};
};

struct SecureTextInputComponentView final
    : winrt::implements<SecureTextInputComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView> {
  SecureTextInputComponentView() = default;

  ~SecureTextInputComponentView() {
    if (lifecycle_) {
      lifecycle_->alive.store(false, std::memory_order_release);
      lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel);
    }
    try {
      if (password_box_ && password_changed_token_.value != 0) {
        password_box_.PasswordChanged(password_changed_token_);
      }
      if (password_box_ && lost_focus_token_.value != 0) {
        password_box_.LostFocus(lost_focus_token_);
      }
      if (password_box_ && key_down_token_.value != 0) {
        password_box_.KeyDown(key_down_token_);
      }
    } catch (...) {
    }
  }

  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    try {
      island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
      password_box_ = PasswordBox{};
      password_box_.FontSize(kUIFontSize);
      // Keep secret fields visually and typographically identical to regular
      // inputs. PasswordBox otherwise inherits template padding that differs
      // from TextBox and can make a 13pt value look vertically clipped.
      password_box_.MinHeight(30.0);
      password_box_.Padding(winrt::Microsoft::UI::Xaml::Thickness{8, 0, 8, 0});
      password_box_.VerticalContentAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Center);
      password_box_.PasswordRevealMode(winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Hidden);
      password_box_.MaxLength(16 * 1024);
      dispatcher_ = winrt::Microsoft::UI::Dispatching::DispatcherQueue::GetForCurrentThread();
      auto weak_self = get_weak();
      password_changed_token_ = password_box_.PasswordChanged([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->MarkDirty();
      });
      lost_focus_token_ = password_box_.LostFocus([weak_self](auto const&, auto const&) {
        if (auto self = weak_self.get()) self->StageOnBlur();
      });
      key_down_token_ = password_box_.KeyDown(
          [weak_self](auto const&, winrt::Microsoft::UI::Xaml::Input::KeyRoutedEventArgs const& args) {
            if (args.Key() != winrt::Windows::System::VirtualKey::Enter) return;
            if (auto self = weak_self.get()) self->StageOnSubmit();
          });
      island_.Content(password_box_);
      island_view.Connect(island_.ContentIsland());
      ApplyProps(nullptr);
    } catch (...) {
      EmitState(0, false, "error", "initialize_failed", last_commit_request_);
    }
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView>::UpdateProps(
        view, props, old_props);
    ApplyProps(old_props);
  }

  void UpdateEventEmitter(
      std::shared_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputEventEmitter> const& emitter) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISecureTextInput<SecureTextInputComponentView>::UpdateEventEmitter(emitter);
    if (emitter && !last_status_.empty()) {
      EmitState(last_revision_, last_present_, last_status_, last_error_, last_commit_request_);
    }
  }

 private:
  bool Current(uint64_t generation) const noexcept {
    return lifecycle_ && lifecycle_->alive.load(std::memory_order_acquire) &&
        lifecycle_->generation.load(std::memory_order_acquire) == generation;
  }

  bool IsPlainTextProviderKey(
      winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputProps const& props) const noexcept {
    return props.plainText.value_or(false) && props.autoCommit.value_or(false) &&
        props.domain == "providers_models" &&
        props.field == "api_key" && !props.target.empty();
  }

  bool IsPlainTextProviderKey() const noexcept {
    return Props() && IsPlainTextProviderKey(*Props());
  }

  void SetPassword(winrt::hstring const& value) noexcept {
    if (!password_box_) return;
    syncing_ = true;
    password_box_.Password(value);
    syncing_ = false;
  }

  void ApplyProps(
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputProps> const& old_props) noexcept {
    if (!password_box_ || !Props()) return;
    auto const& props = *Props();
    const bool identity_changed = !old_props || old_props->domain != props.domain ||
        old_props->field != props.field || old_props->target != props.target;
    const bool plain_key_mode_changed = !old_props ||
        old_props->plainText != props.plainText || old_props->autoCommit != props.autoCommit;
    const bool reset_identity = identity_changed || plain_key_mode_changed;
    const bool should_load_plaintext_provider_key = reset_identity && IsPlainTextProviderKey(props);
    const bool placeholder_changed = !old_props || old_props->placeholder != props.placeholder;
    const bool plain_text_changed = !old_props || old_props->plainText != props.plainText;
    const bool label_changed = !old_props || old_props->label != props.label;
    const bool disabled_changed = !old_props || old_props->disabled != props.disabled;
    if (reset_identity) {
      lifecycle_->generation.fetch_add(1, std::memory_order_acq_rel);
      lifecycle_->staging.store(false, std::memory_order_release);
      loading_ = false;
      active_domain_ = props.domain;
      active_field_ = props.field;
      active_target_ = props.target;
      SetPassword(winrt::hstring{});
      dirty_ = false;
      last_revision_ = 0;
      last_present_ = false;
      last_status_ = "ready";
      last_error_.clear();
    }
    if (placeholder_changed) {
      password_box_.PlaceholderText(ToHString(props.placeholder.value_or("")));
    }
    if (plain_text_changed) {
      password_box_.PasswordRevealMode(props.plainText.value_or(false)
          ? winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Visible
          : winrt::Microsoft::UI::Xaml::Controls::PasswordRevealMode::Hidden);
    }
    if (label_changed) {
      winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(password_box_, ToHString(props.label));
    }
    if (disabled_changed || reset_identity) {
      password_box_.IsEnabled(
          Enabled(props.disabled) && !lifecycle_->staging.load(std::memory_order_acquire) && !loading_);
    }
    const int32_t reset_request = props.resetRequest.value_or(0);
    if ((!old_props || old_props->resetRequest != props.resetRequest) && reset_request != last_reset_request_) {
      last_reset_request_ = reset_request;
      SetPassword(winrt::hstring{});
      dirty_ = false;
      if (!lifecycle_->staging.load(std::memory_order_acquire)) {
        EmitState(last_revision_, last_present_, "ready", "", last_commit_request_);
      }
    }
    const int32_t commit_request = props.commitRequest.value_or(0);
    if ((!old_props || old_props->commitRequest != props.commitRequest) && commit_request != last_commit_request_) {
      StageForRequest(commit_request, false);
    }
    if (should_load_plaintext_provider_key) {
      LoadProviderApiKey();
    }
  }

  void MarkDirty() noexcept {
    if (syncing_ || !lifecycle_ || lifecycle_->staging.load(std::memory_order_acquire)) return;
    try {
      if (password_box_.Password().empty() && !IsPlainTextProviderKey()) return;
      dirty_ = true;
      EmitState(last_revision_, last_present_, "dirty", "", last_commit_request_);
    } catch (...) {
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
    }
  }

  void StageOnBlur() noexcept {
    if (!IsPlainTextProviderKey() || !dirty_) return;
    StageForRequest(NextAutoCommitRequest(), true);
  }

  void StageOnSubmit() noexcept {
    if (!IsPlainTextProviderKey() || !dirty_) return;
    StageForRequest(NextAutoCommitRequest(), true);
  }

  int32_t NextAutoCommitRequest() noexcept {
    next_auto_commit_request_ = std::max(next_auto_commit_request_, last_commit_request_);
    if (next_auto_commit_request_ < std::numeric_limits<int32_t>::max()) {
      ++next_auto_commit_request_;
    }
    return next_auto_commit_request_;
  }

  void StageForRequest(int32_t requested_commit, bool allow_empty) noexcept {
    if (!Props() || !password_box_ || lifecycle_->staging.load(std::memory_order_acquire)) return;
    std::wstring password;
    try {
      password = password_box_.Password().c_str();
    } catch (...) {
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    if (password.empty() && !allow_empty) {
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      EmitState(last_revision_, last_present_, "ready", "", last_commit_request_);
      return;
    }
    if (active_domain_.empty() || active_field_.empty()) {
      SetPassword(winrt::hstring{});
      if (IsPlainTextProviderKey()) dirty_ = true;
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    std::optional<std::string> secret;
    try {
      secret = winrt::to_string(winrt::hstring{password});
    } catch (...) {
      secret.reset();
    }
    const bool preserve_input = IsPlainTextProviderKey();
    if (!preserve_input) {
      SetPassword(winrt::hstring{});
    }
    if (!secret || secret->size() > 16 * 1024) {
      last_commit_request_ = std::max(last_commit_request_, requested_commit);
      if (IsPlainTextProviderKey()) dirty_ = true;
      EmitState(last_revision_, last_present_, "error", "invalid_secret", last_commit_request_);
      return;
    }
    bool expected = false;
    if (!lifecycle_->staging.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) return;
    password_box_.IsEnabled(false);
    dirty_ = false;
    last_commit_request_ = std::max(last_commit_request_, requested_commit);
    const auto generation = lifecycle_->generation.load(std::memory_order_acquire);
    const bool disabled = Props()->disabled.value_or(false);
    EmitState(last_revision_, last_present_, "saving", "", last_commit_request_);
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    auto domain = active_domain_;
    auto field = active_field_;
    auto target = active_target_;
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, disabled, domain = std::move(domain), field = std::move(field), target = std::move(target), secret = std::move(*secret)]() mutable {
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation) return;
        auto capability = LiteLLMMenu::CoreIPCBridge::Shared().CreateSecretCapability(
            domain, field, target.empty() ? std::nullopt : std::optional<std::string>{target}, "settings");
        std::optional<LiteLLMMenu::CoreIPCBridge::SecretStageResult> result;
        if (capability) result = LiteLLMMenu::CoreIPCBridge::Shared().StageSecret(capability->token, secret, false);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) return;
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, disabled, result = std::move(result)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) return;
          if (auto self = weak_self.get()) self->FinishStage(generation, disabled, std::move(result));
        });
      }).detach();
    } catch (...) {
      lifecycle_->staging.store(false, std::memory_order_release);
      password_box_.IsEnabled(!disabled);
      if (IsPlainTextProviderKey()) dirty_ = true;
      EmitState(last_revision_, last_present_, "error", "stage_failed", last_commit_request_);
    }
  }

  void FinishStage(
      uint64_t generation,
      bool disabled,
      std::optional<LiteLLMMenu::CoreIPCBridge::SecretStageResult> result) noexcept {
    if (!Current(generation)) return;
    lifecycle_->staging.store(false, std::memory_order_release);
    password_box_.IsEnabled(!disabled);
    if (!result || result->revision < 0 || result->revision > std::numeric_limits<int32_t>::max() ||
        std::floor(result->revision) != result->revision) {
      EmitState(last_revision_, last_present_, "error", "stage_failed", last_commit_request_);
      if (IsPlainTextProviderKey()) dirty_ = true;
      return;
    }
    EmitState(static_cast<int32_t>(result->revision), result->present, "saved", "", last_commit_request_);
  }

  void LoadProviderApiKey() noexcept {
    if (!IsPlainTextProviderKey() || !lifecycle_ || !dispatcher_) return;
    const auto generation = lifecycle_->generation.load(std::memory_order_acquire);
    const bool disabled = Props()->disabled.value_or(false);
    const auto target = active_target_;
    auto lifecycle = lifecycle_;
    auto dispatcher = dispatcher_;
    auto weak_self = get_weak();
    loading_ = true;
    password_box_.IsEnabled(false);
    try {
      std::thread([lifecycle, dispatcher, weak_self, generation, disabled, target] {
        auto value = LiteLLMMenu::CoreIPCBridge::Shared().ReadProviderAPIKey(target);
        if (!lifecycle->alive.load(std::memory_order_acquire) ||
            lifecycle->generation.load(std::memory_order_acquire) != generation || !dispatcher) {
          return;
        }
        dispatcher.TryEnqueue([lifecycle, weak_self, generation, disabled, value = std::move(value)]() mutable {
          if (!lifecycle->alive.load(std::memory_order_acquire) ||
              lifecycle->generation.load(std::memory_order_acquire) != generation) {
            return;
          }
          if (auto self = weak_self.get()) self->FinishProviderApiKeyLoad(generation, disabled, std::move(value));
        });
      }).detach();
    } catch (...) {
      loading_ = false;
      password_box_.IsEnabled(!disabled);
      EmitState(last_revision_, last_present_, "error", "read_failed", last_commit_request_);
    }
  }

  void FinishProviderApiKeyLoad(
      uint64_t generation,
      bool disabled,
      std::optional<std::string> value) noexcept {
    if (!Current(generation) || !IsPlainTextProviderKey()) return;
    loading_ = false;
    SetPassword(value ? ToHString(*value) : winrt::hstring{});
    dirty_ = false;
    password_box_.IsEnabled(!disabled);
    if (!value) {
      EmitState(last_revision_, false, "error", "read_failed", last_commit_request_);
      return;
    }
    EmitState(last_revision_, !value->empty(), "ready", "", last_commit_request_);
  }

  void EmitState(int32_t revision, bool present, std::string status, std::string error, int32_t commit_request) noexcept {
    last_revision_ = std::max(0, revision);
    last_present_ = present;
    last_status_ = status;
    last_error_ = error;
    last_commit_request_ = std::max(0, commit_request);
    try {
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUISecureTextInputEventEmitter::OnSecretState args;
        args.revision = last_revision_;
        args.present = last_present_;
        args.status = std::move(status);
        args.error = std::move(error);
        args.commitRequest = last_commit_request_;
        emitter->onSecretState(std::move(args));
      }
    } catch (...) {
    }
  }

  bool syncing_ = false;
  bool dirty_ = false;
  bool loading_ = false;
  std::shared_ptr<SecureInputLifecycle> lifecycle_{std::make_shared<SecureInputLifecycle>()};
  winrt::Microsoft::UI::Dispatching::DispatcherQueue dispatcher_{nullptr};
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  PasswordBox password_box_{nullptr};
  std::string active_domain_;
  std::string active_field_;
  std::string active_target_;
  int32_t last_revision_ = 0;
  bool last_present_ = false;
  std::string last_status_;
  std::string last_error_;
  int32_t last_commit_request_ = 0;
  int32_t next_auto_commit_request_ = 0;
  int32_t last_reset_request_ = 0;
  winrt::event_token password_changed_token_{};
  winrt::event_token lost_focus_token_{};
  winrt::event_token key_down_token_{};
};

struct SwitchComponentView final
    : winrt::implements<SwitchComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISwitch<SwitchComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    toggle_ = ToggleSwitch{};
    toggle_.OnContent(nullptr);
    toggle_.OffContent(nullptr);
    toggle_.Toggled([this](auto const&, auto const&) {
      if (syncing_) return;
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUISwitchEventEmitter::OnValueChange args;
        args.value = toggle_.IsOn();
        emitter->onValueChange(std::move(args));
      }
    });
    island_.Content(toggle_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps(nullptr);
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISwitchProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISwitchProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISwitch<SwitchComponentView>::UpdateProps(view, props, old_props);
    ApplyProps(old_props);
  }

 private:
  void ApplyProps(
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISwitchProps> const& old_props) noexcept {
    if (!toggle_ || !Props()) return;
    auto const& props = *Props();
    const bool value_changed = !old_props || old_props->value != props.value;
    const bool disabled_changed = !old_props || old_props->disabled != props.disabled;
    if (value_changed) {
      syncing_ = true;
      toggle_.IsOn(props.value.value_or(false));
      syncing_ = false;
    }
    if (disabled_changed) toggle_.IsEnabled(Enabled(props.disabled));
  }

  bool syncing_ = false;
  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  ToggleSwitch toggle_{nullptr};
};

struct SelectableRowComponentView final
    : winrt::implements<SelectableRowComponentView, winrt::IInspectable>,
      winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISelectableRow<SelectableRowComponentView> {
  void InitializeContentIsland(ContentIslandComponentView const& island_view) noexcept {
    island_ = winrt::Microsoft::UI::Xaml::XamlIsland{};
    button_ = Button{};
    button_.HorizontalContentAlignment(winrt::Microsoft::UI::Xaml::HorizontalAlignment::Stretch);
    content_ = StackPanel{};
    content_.Spacing(2);
    title_ = TextBlock{};
    title_.FontSize(kUIFontSize);
    title_.TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
    title_.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
    detail_ = TextBlock{};
    detail_.FontSize(kUIFontSize);
    detail_.TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap);
    detail_.TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis);
    detail_.Opacity(0.68);
    content_.Children().Append(title_);
    content_.Children().Append(detail_);
    button_.Content(content_);
    button_.Click([this](auto const&, auto const&) {
      if (auto emitter = EventEmitter()) {
        winrt::LiteLLMMenu::Codegen::LiteLLMWinUISelectableRowEventEmitter::OnPress args;
        emitter->onPress(std::move(args));
      }
    });
    island_.Content(button_);
    island_view.Connect(island_.ContentIsland());
    ApplyProps();
  }

  void UpdateProps(
      winrt::Microsoft::ReactNative::ComponentView const& view,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISelectableRowProps> const& props,
      winrt::com_ptr<winrt::LiteLLMMenu::Codegen::LiteLLMWinUISelectableRowProps> const& old_props) noexcept override {
    winrt::LiteLLMMenu::Codegen::BaseLiteLLMWinUISelectableRow<SelectableRowComponentView>::UpdateProps(view, props, old_props);
    ApplyProps();
  }

 private:
  void ApplyProps() noexcept {
    if (!button_ || !Props()) return;
    auto const& props = *Props();
    title_.Text(ToHString(props.title));
    auto detail = ToHString(props.detail.value_or(""));
    detail_.Text(detail);
    detail_.Visibility(detail.empty() ? winrt::Microsoft::UI::Xaml::Visibility::Collapsed : winrt::Microsoft::UI::Xaml::Visibility::Visible);
    button_.IsEnabled(Enabled(props.disabled));
    if (props.selected.value_or(false)) button_.Background(SelectionBrush());
    else button_.Background(nullptr);
  }

  winrt::Microsoft::UI::Xaml::XamlIsland island_{nullptr};
  Button button_{nullptr};
  StackPanel content_{nullptr};
  TextBlock title_{nullptr};
  TextBlock detail_{nullptr};
};

template <typename TComponent>
void RegisterComponent(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder,
    void (*register_component)(
        winrt::Microsoft::ReactNative::IReactPackageBuilder const&,
        std::function<void(winrt::Microsoft::ReactNative::Composition::IReactCompositionViewComponentBuilder const&)>)) noexcept {
  register_component(package_builder, [](winrt::Microsoft::ReactNative::Composition::IReactCompositionViewComponentBuilder const& builder) {
    builder.SetContentIslandComponentViewInitializer([](ContentIslandComponentView const& island_view) noexcept {
      auto user_data = winrt::make_self<TComponent>();
      user_data->InitializeContentIsland(island_view);
      island_view.UserData(*user_data);
    });
  });
}

}  // namespace

namespace LiteLLMMenu {

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const& package_builder) noexcept {
  RegisterComponent<ButtonComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUIButtonNativeComponent<ButtonComponentView>);
  RegisterComponent<SegmentedComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISegmentedControlNativeComponent<SegmentedComponentView>);
  RegisterComponent<PickerComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUIPickerNativeComponent<PickerComponentView>);
  RegisterComponent<CheckboxComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUICheckboxNativeComponent<CheckboxComponentView>);
  RegisterComponent<TableComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUITableNativeComponent<TableComponentView>);
  RegisterComponent<TextEditorComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUITextEditorNativeComponent<TextEditorComponentView>);
  RegisterComponent<SecureTextEditorComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISecureTextEditorNativeComponent<SecureTextEditorComponentView>);
  RegisterComponent<SecureTextInputComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISecureTextInputNativeComponent<SecureTextInputComponentView>);
  // ContentIsland children are Composition visuals, not XAML UIElements that a
  // WinUI SplitView can accept as Pane/Content. This component is therefore a
  // narrow native drag leaf; React owns pane layout and positions the leaf.
  RegisterComponent<SplitterComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISplitViewNativeComponent<SplitterComponentView>);
  RegisterComponent<TextInputComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUITextInputNativeComponent<TextInputComponentView>);
  RegisterComponent<SwitchComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISwitchNativeComponent<SwitchComponentView>);
  RegisterComponent<SelectableRowComponentView>(
      package_builder,
      winrt::LiteLLMMenu::Codegen::RegisterLiteLLMWinUISelectableRowNativeComponent<SelectableRowComponentView>);
}

}  // namespace LiteLLMMenu

#else

namespace LiteLLMMenu {

void RegisterWinUIControls(
    winrt::Microsoft::ReactNative::IReactPackageBuilder const&) noexcept {}

}  // namespace LiteLLMMenu

#endif  // defined(RNW_NEW_ARCH)
