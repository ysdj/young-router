#import "AppKitControlViews.h"

#import <AppKit/AppKit.h>
#import <React/RCTComponent.h>
#import <React/RCTUIKit.h>
#import "LiteLLMMenu-Swift.h"

#import <react/renderer/components/LiteLLMMacControls/ComponentDescriptors.h>
#import <react/renderer/components/LiteLLMMacControls/EventEmitters.h>
#import <react/renderer/components/LiteLLMMacControls/Props.h>
#import <react/renderer/components/LiteLLMMacControls/RCTComponentViewHelpers.h>

#include <algorithm>
#include <memory>
#include <string>
#include <vector>

using namespace facebook::react;

@interface LiteLLMTextEditorViewportState : NSObject
@property(nonatomic) NSPoint origin;
@property(nonatomic) NSRange selection;
@property(nonatomic) BOOL followsBottom;
@end

@implementation LiteLLMTextEditorViewportState
@end

namespace {

NSString *StringFromStdString(const std::string &value)
{
  return [[NSString alloc] initWithBytes:value.data()
                                  length:value.size()
                                encoding:NSUTF8StringEncoding] ?: @"";
}

std::string StdStringFromString(NSString *value)
{
  const char *utf8 = value.UTF8String;
  return utf8 == nullptr ? std::string{} : std::string{utf8};
}

NSInteger SegmentIndex(const std::vector<std::string> &labels, const std::string &selectedValue)
{
  const auto selected = std::find(labels.begin(), labels.end(), selectedValue);
  return selected == labels.end() ? -1 : static_cast<NSInteger>(std::distance(labels.begin(), selected));
}

NSAttributedString *SelectableRowTitle(NSString *title, NSString *detail)
{
  NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
  paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
  paragraph.maximumLineHeight = 16;

  NSDictionary *titleAttributes = @{
    NSFontAttributeName: [NSFont systemFontOfSize:13 weight:NSFontWeightRegular],
    NSForegroundColorAttributeName: NSColor.labelColor,
    NSParagraphStyleAttributeName: paragraph,
  };
  NSMutableAttributedString *result = [[NSMutableAttributedString alloc] initWithString:title attributes:titleAttributes];
  if (detail.length > 0) {
    NSMutableParagraphStyle *detailParagraph = [paragraph mutableCopy];
    detailParagraph.maximumLineHeight = 14;
    [result appendAttributedString:[[NSAttributedString alloc] initWithString:[@"\n" stringByAppendingString:detail]
                                                                    attributes:@{
      NSFontAttributeName: [NSFont systemFontOfSize:11],
      NSForegroundColorAttributeName: NSColor.secondaryLabelColor,
      NSParagraphStyleAttributeName: detailParagraph,
    }]];
  }
  return result;
}

BOOL TextEditorIsFollowingBottom(NSScrollView *scrollView, NSTextView *textView)
{
  [textView.layoutManager ensureLayoutForTextContainer:textView.textContainer];
  NSRect visibleRect = scrollView.contentView.bounds;
  NSRect documentRect = textView.bounds;
  return NSHeight(documentRect) <= NSHeight(visibleRect) ||
      NSMaxY(visibleRect) >= NSMaxY(documentRect) - 4.0;
}

void RestoreTextEditorViewport(NSScrollView *scrollView,
                               NSTextView *textView,
                               NSPoint previousOrigin,
                               BOOL followBottom)
{
  [textView.layoutManager ensureLayoutForTextContainer:textView.textContainer];
  NSClipView *clipView = scrollView.contentView;
  NSRect documentRect = textView.bounds;
  CGFloat maximumX = MAX(NSMinX(documentRect), NSMaxX(documentRect) - NSWidth(clipView.bounds));
  CGFloat maximumY = MAX(NSMinY(documentRect), NSMaxY(documentRect) - NSHeight(clipView.bounds));
  NSPoint restoredOrigin = NSMakePoint(
      MIN(MAX(previousOrigin.x, NSMinX(documentRect)), maximumX),
      followBottom
          ? maximumY
          : MIN(MAX(previousOrigin.y, NSMinY(documentRect)), maximumY));
  [clipView scrollToPoint:restoredOrigin];
  [scrollView reflectScrolledClipView:clipView];
}

LiteLLMTextEditorViewportState *CaptureTextEditorViewport(NSScrollView *scrollView,
                                                          NSTextView *textView)
{
  LiteLLMTextEditorViewportState *state = [LiteLLMTextEditorViewportState new];
  state.origin = scrollView.contentView.bounds.origin;
  state.selection = textView.selectedRange;
  state.followsBottom = TextEditorIsFollowingBottom(scrollView, textView);
  return state;
}

void RestoreTextEditorState(NSScrollView *scrollView,
                            NSTextView *textView,
                            LiteLLMTextEditorViewportState *state)
{
  if (state == nil) {
    [textView setSelectedRange:NSMakeRange(0, 0)];
    RestoreTextEditorViewport(scrollView, textView, NSZeroPoint, YES);
    return;
  }

  NSUInteger selectionLocation = MIN(state.selection.location, textView.string.length);
  NSUInteger selectionLength = MIN(state.selection.length, textView.string.length - selectionLocation);
  [textView setSelectedRange:NSMakeRange(selectionLocation, selectionLength)];
  RestoreTextEditorViewport(scrollView, textView, state.origin, state.followsBottom);
}

} // namespace

static void LayoutAppKitControlInBounds(NSView *control, NSRect bounds, BOOL fillsHeight)
{
  if (control == nil) {
    return;
  }

  if (fillsHeight) {
    control.frame = bounds;
    return;
  }

  CGFloat preferredHeight = control.intrinsicContentSize.height;
  if (!(preferredHeight > 0)) {
    preferredHeight = control.fittingSize.height;
  }
  if (!(preferredHeight > 0)) {
    preferredHeight = NSHeight(bounds);
  }
  const CGFloat height = MIN(NSHeight(bounds), preferredHeight);
  control.frame = NSMakeRect(
      NSMinX(bounds),
      NSMidY(bounds) - height / 2.0,
      NSWidth(bounds),
      MAX(0, height));
}

@interface LiteLLMAppKitControlHostView : NSView
@property(nonatomic, strong, nullable) NSView *control;
@property(nonatomic) BOOL fillsHeight;
@end

@implementation LiteLLMAppKitControlHostView

- (void)setControl:(NSView *)control
{
  if (_control == control) {
    return;
  }
  [_control removeFromSuperview];
  _control = control;
  if (_control != nil) {
    _control.autoresizingMask = NSViewWidthSizable;
    [self addSubview:_control];
  }
  [self setNeedsLayout:YES];
}

- (void)setFillsHeight:(BOOL)fillsHeight
{
  if (_fillsHeight == fillsHeight) {
    return;
  }
  _fillsHeight = fillsHeight;
  [self setNeedsLayout:YES];
}

- (void)layout
{
  [super layout];
  LayoutAppKitControlInBounds(_control, self.bounds, _fillsHeight);
}

@end

@interface LiteLLMNavigationLinkButton : NSButton
@property(nonatomic) BOOL linkMode;
@end

@implementation LiteLLMNavigationLinkButton {
  NSTrackingArea *_hoverTrackingArea;
  BOOL _hovering;
}

- (void)setLinkMode:(BOOL)linkMode
{
  _linkMode = linkMode;
  if (!linkMode) {
    _hovering = NO;
  }
  [self updateLinkAppearance];
  [self.window invalidateCursorRectsForView:self];
}

- (void)setEnabled:(BOOL)enabled
{
  [super setEnabled:enabled];
  if (!enabled) {
    _hovering = NO;
  }
  [self updateLinkAppearance];
}

- (void)setTitle:(NSString *)title
{
  [super setTitle:title];
  [self updateLinkAppearance];
}

- (void)updateTrackingAreas
{
  [super updateTrackingAreas];
  if (_hoverTrackingArea != nil) {
    [self removeTrackingArea:_hoverTrackingArea];
  }
  _hoverTrackingArea = [[NSTrackingArea alloc] initWithRect:NSZeroRect
                                                    options:NSTrackingActiveInKeyWindow |
                                                            NSTrackingInVisibleRect |
                                                            NSTrackingMouseEnteredAndExited
                                                      owner:self
                                                   userInfo:nil];
  [self addTrackingArea:_hoverTrackingArea];
}

- (void)resetCursorRects
{
  [super resetCursorRects];
  if (self.linkMode && self.enabled) {
    [self addCursorRect:self.bounds cursor:NSCursor.pointingHandCursor];
  }
}

- (void)mouseEntered:(NSEvent *)event
{
  if (self.linkMode && self.enabled) {
    _hovering = YES;
    [self updateLinkAppearance];
  }
}

- (void)mouseExited:(NSEvent *)event
{
  _hovering = NO;
  [self updateLinkAppearance];
}

- (void)updateLinkAppearance
{
  if (!self.linkMode) {
    return;
  }
  NSColor *color = self.enabled
      ? (_hovering ? NSColor.controlAccentColor : NSColor.linkColor)
      : NSColor.secondaryLabelColor;
  self.attributedTitle = [[NSAttributedString alloc] initWithString:self.title ?: @"" attributes:@{
    NSFontAttributeName: self.font ?: [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold],
    NSForegroundColorAttributeName: color,
    NSUnderlineStyleAttributeName: _hovering && self.enabled && self.title.length > 0
        ? @(NSUnderlineStyleSingle)
        : @0,
  }];
  self.contentTintColor = color;
}

@end

@interface LiteLLMAppKitButtonComponentView () <RCTLiteLLMAppKitButtonViewProtocol>
@end

@implementation LiteLLMAppKitButtonComponentView {
  NSButton *_button;
  LiteLLMAppKitControlHostView *_host;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitButtonComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitButtonProps>();
    _props = defaultProps;

    _button = [LiteLLMNavigationLinkButton buttonWithTitle:@"" target:self action:@selector(pressed:)];
    _button.bezelStyle = NSBezelStyleRounded;
    _button.controlSize = NSControlSizeRegular;
    _button.buttonType = NSButtonTypeMomentaryPushIn;
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    _host.control = _button;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitButtonProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitButtonProps>(props);
  const BOOL titleChanged = oldViewProps.title != newViewProps.title;
  const BOOL linkChanged = oldViewProps.link != newViewProps.link;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;
  BOOL link = newViewProps.link;
  BOOL useCompactControl = newViewProps.compact && !link;

  if (titleChanged) {
    NSString *title = StringFromStdString(newViewProps.title);
    _button.title = title;
    if (newViewProps.toolTip.empty()) {
      _button.toolTip = title;
    }
    if (newViewProps.accessibilityLabel.empty()) {
      _button.accessibilityLabel = title;
    }
  }
  if (oldViewProps.toolTip != newViewProps.toolTip) {
    _button.toolTip = newViewProps.toolTip.empty() ? StringFromStdString(newViewProps.title) : StringFromStdString(newViewProps.toolTip);
  }
  if (oldViewProps.accessibilityLabel != newViewProps.accessibilityLabel) {
    _button.accessibilityLabel = newViewProps.accessibilityLabel.empty() ? StringFromStdString(newViewProps.title) : StringFromStdString(newViewProps.accessibilityLabel);
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _button.enabled = !newViewProps.disabled;
  }
  if (linkChanged ||
      oldViewProps.primary != newViewProps.primary ||
      oldViewProps.destructive != newViewProps.destructive ||
      compactChanged) {
    _button.bezelStyle = link ? NSBezelStyleInline : NSBezelStyleRounded;
    _button.keyEquivalent = !link && newViewProps.primary ? @"\r" : @"";
    _button.hasDestructiveAction = !link && newViewProps.destructive;
    _button.controlSize = useCompactControl ? NSControlSizeSmall : NSControlSizeRegular;
    _button.font = link
        ? [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold]
        : [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_button.controlSize]];
    ((LiteLLMNavigationLinkButton *)_button).linkMode = link;
  }
  if (!link && linkChanged) {
    _button.contentTintColor = nil;
    _button.title = StringFromStdString(newViewProps.title);
  }

  if (titleChanged || linkChanged || compactChanged) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)pressed:(__unused id)sender
{
  if (_eventEmitter) {
    std::static_pointer_cast<const LiteLLMAppKitButtonEventEmitter>(_eventEmitter)->onPress({});
  }
}

- (NSView *)accessibilityElement
{
  return _button;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitButtonCls(void)
{
  return LiteLLMAppKitButtonComponentView.class;
}

@interface LiteLLMAppKitCheckboxComponentView () <RCTLiteLLMAppKitCheckboxViewProtocol>
@end

@implementation LiteLLMAppKitCheckboxComponentView {
  NSButton *_checkbox;
  LiteLLMAppKitControlHostView *_host;
  BOOL _synchronizing;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitCheckboxComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitCheckboxProps>();
    _props = defaultProps;
    _checkbox = [NSButton checkboxWithTitle:@"" target:self action:@selector(changed:)];
    _checkbox.controlSize = NSControlSizeRegular;
    _checkbox.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_checkbox.controlSize]];
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    _host.control = _checkbox;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitCheckboxProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitCheckboxProps>(props);
  const BOOL labelChanged = oldViewProps.label != newViewProps.label;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;
  if (labelChanged) {
    NSString *label = StringFromStdString(newViewProps.label);
    _checkbox.title = label;
    _checkbox.accessibilityLabel = label;
  }
  _synchronizing = YES;
  if (oldViewProps.value != newViewProps.value) {
    _checkbox.state = newViewProps.value ? NSControlStateValueOn : NSControlStateValueOff;
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _checkbox.enabled = !newViewProps.disabled;
  }
  if (compactChanged) {
    _checkbox.controlSize = newViewProps.compact ? NSControlSizeSmall : NSControlSizeRegular;
    _checkbox.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_checkbox.controlSize]];
  }
  _synchronizing = NO;
  // A checked value or enabled state does not alter intrinsic geometry.  Do
  // not request a host layout while AppKit is handling a user click: that
  // needless pass is visible as a control flash on dense provider screens.
  if (labelChanged || compactChanged) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)changed:(__unused id)sender
{
  if (!_synchronizing && _eventEmitter) {
    const BOOL value = _checkbox.state == NSControlStateValueOn;
    LiteLLMAppKitCheckboxEventEmitter::OnValueChange event{static_cast<bool>(value)};
    std::static_pointer_cast<const LiteLLMAppKitCheckboxEventEmitter>(_eventEmitter)->onValueChange(event);
  }
}

- (NSView *)accessibilityElement
{
  return _checkbox;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitCheckboxCls(void)
{
  return LiteLLMAppKitCheckboxComponentView.class;
}

@interface LiteLLMAppKitPickerComponentView () <RCTLiteLLMAppKitPickerViewProtocol>
@end

@implementation LiteLLMAppKitPickerComponentView {
  NSPopUpButton *_picker;
  LiteLLMAppKitControlHostView *_host;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitPickerComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitPickerProps>();
    _props = defaultProps;
    _picker = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    _picker.controlSize = NSControlSizeRegular;
    _picker.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_picker.controlSize]];
    _picker.target = self;
    _picker.action = @selector(changed:);
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    _host.control = _picker;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitPickerProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitPickerProps>(props);
  const BOOL labelsChanged = oldViewProps.labels != newViewProps.labels;
  const BOOL selectedChanged = oldViewProps.selectedValue != newViewProps.selectedValue;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;
  if (labelsChanged) {
    [_picker removeAllItems];
    for (const auto &label : newViewProps.labels) {
      [_picker addItemWithTitle:StringFromStdString(label)];
    }
  }
  if (labelsChanged || selectedChanged) {
    const NSInteger selectedIndex = SegmentIndex(newViewProps.labels, newViewProps.selectedValue);
    if (selectedIndex >= 0) {
      [_picker selectItemAtIndex:selectedIndex];
    } else {
      [_picker selectItem:nil];
    }
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _picker.enabled = !newViewProps.disabled;
  }
  if (compactChanged) {
    _picker.controlSize = newViewProps.compact ? NSControlSizeSmall : NSControlSizeRegular;
    _picker.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_picker.controlSize]];
  }
  if (labelsChanged || compactChanged) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)changed:(__unused id)sender
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitPickerProps>(_props);
  const NSInteger index = _picker.indexOfSelectedItem;
  if (!_eventEmitter || index < 0 || static_cast<size_t>(index) >= viewProps.labels.size()) {
    return;
  }
  LiteLLMAppKitPickerEventEmitter::OnChange event{static_cast<int>(index), viewProps.labels[static_cast<size_t>(index)]};
  std::static_pointer_cast<const LiteLLMAppKitPickerEventEmitter>(_eventEmitter)->onChange(event);
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitPickerCls(void)
{
  return LiteLLMAppKitPickerComponentView.class;
}

@interface LiteLLMAppKitSegmentedControlComponentView () <RCTLiteLLMAppKitSegmentedControlViewProtocol>
@end

@implementation LiteLLMAppKitSegmentedControlComponentView {
  NSSegmentedControl *_control;
  LiteLLMAppKitControlHostView *_host;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSegmentedControlComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSegmentedControlProps>();
    _props = defaultProps;

    _control = [[NSSegmentedControl alloc] initWithFrame:NSZeroRect];
    _control.segmentStyle = NSSegmentStyleRounded;
    _control.trackingMode = NSSegmentSwitchTrackingSelectOne;
    _control.controlSize = NSControlSizeRegular;
    _control.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_control.controlSize]];
    _control.target = self;
    _control.action = @selector(changed:);
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    _host.control = _control;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitSegmentedControlProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSegmentedControlProps>(props);
  const BOOL labelsChanged = oldViewProps.labels != newViewProps.labels;
  const BOOL selectedChanged = oldViewProps.selectedValue != newViewProps.selectedValue;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;

  if (labelsChanged) {
    _control.segmentCount = newViewProps.labels.size();
    for (NSUInteger index = 0; index < newViewProps.labels.size(); index++) {
      [_control setLabel:StringFromStdString(newViewProps.labels[index]) forSegment:index];
    }
  }
  if (labelsChanged || selectedChanged) {
    _control.selectedSegment = SegmentIndex(newViewProps.labels, newViewProps.selectedValue);
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _control.enabled = !newViewProps.disabled;
  }
  if (compactChanged) {
    _control.controlSize = newViewProps.compact ? NSControlSizeSmall : NSControlSizeRegular;
    _control.font = [NSFont systemFontOfSize:[NSFont systemFontSizeForControlSize:_control.controlSize]];
  }

  if (labelsChanged || compactChanged) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)changed:(__unused id)sender
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitSegmentedControlProps>(_props);
  const NSInteger index = _control.selectedSegment;
  if (!_eventEmitter || index < 0 || static_cast<size_t>(index) >= viewProps.labels.size()) {
    return;
  }
  LiteLLMAppKitSegmentedControlEventEmitter::OnChange event{
    static_cast<int>(index), viewProps.labels[static_cast<size_t>(index)]};
  std::static_pointer_cast<const LiteLLMAppKitSegmentedControlEventEmitter>(_eventEmitter)->onChange(event);
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSegmentedControlCls(void)
{
  return LiteLLMAppKitSegmentedControlComponentView.class;
}

@interface LiteLLMAppKitTextFieldHostView : NSView
@property(nonatomic, strong, nullable) NSView *activeControl;
@end

@implementation LiteLLMAppKitTextFieldHostView

- (void)setActiveControl:(NSView *)activeControl
{
  if (_activeControl == activeControl) {
    return;
  }
  [_activeControl removeFromSuperview];
  _activeControl = activeControl;
  if (_activeControl) {
    _activeControl.autoresizingMask = NSViewWidthSizable;
    [self addSubview:_activeControl];
  }
  [self setNeedsLayout:YES];
}

- (void)layout
{
  [super layout];
  // Multiline editors own the full allocated rectangle. AppKit single-line
  // fields keep their intrinsic bezel height and are centered in the shared
  // 30pt row so the text baseline and focus ring stay visually centered.
  LayoutAppKitControlInBounds(
      _activeControl,
      self.bounds,
      [_activeControl isKindOfClass:NSScrollView.class]);
}

@end

@interface LiteLLMAppKitTextFieldComponentView () <NSTextFieldDelegate, NSTextViewDelegate, RCTLiteLLMAppKitTextFieldViewProtocol>
@end

@implementation LiteLLMAppKitTextFieldComponentView {
  LiteLLMAppKitTextFieldHostView *_host;
  NSTextField *_field;
  NSSecureTextField *_secureField;
  NSScrollView *_scrollView;
  NSTextView *_multilineField;
  BOOL _synchronizing;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitTextFieldComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitTextFieldProps>();
    _props = defaultProps;

    _host = [[LiteLLMAppKitTextFieldHostView alloc] initWithFrame:NSZeroRect];
    _field = [[NSTextField alloc] initWithFrame:NSZeroRect];
    _field.delegate = self;
    _field.target = self;
    _field.action = @selector(submitted:);
    _field.bezelStyle = NSTextFieldRoundedBezel;
    _field.font = [NSFont systemFontOfSize:13];

    _secureField = [[NSSecureTextField alloc] initWithFrame:NSZeroRect];
    _secureField.delegate = self;
    _secureField.target = self;
    _secureField.action = @selector(submitted:);
    _secureField.bezelStyle = NSTextFieldRoundedBezel;
    _secureField.font = [NSFont systemFontOfSize:13];

    _multilineField = [[NSTextView alloc] initWithFrame:NSZeroRect];
    _multilineField.delegate = self;
    _multilineField.font = [NSFont systemFontOfSize:13];
    _multilineField.usesFindPanel = YES;
    _multilineField.richText = NO;
    _multilineField.allowsUndo = YES;
    _multilineField.verticallyResizable = YES;
    _multilineField.horizontallyResizable = YES;
    _multilineField.textContainer.containerSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);
    _multilineField.textContainer.widthTracksTextView = YES;
    _multilineField.textContainerInset = NSMakeSize(5, 4);
    _scrollView = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    _scrollView.borderType = NSBezelBorder;
    _scrollView.hasVerticalScroller = YES;
    _scrollView.autohidesScrollers = YES;
    _scrollView.documentView = _multilineField;

    _host.activeControl = _field;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitTextFieldProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitTextFieldProps>(props);

  const BOOL isMultiline = newViewProps.multiline;
  NSView *activeControl = isMultiline ? _scrollView : (newViewProps.secureTextEntry ? _secureField : _field);
  const BOOL activeControlChanged = _host.activeControl != activeControl;
  if (activeControlChanged) {
    _host.activeControl = activeControl;
  }

  // Fabric also calls updateProps for event-emitter and layout changes.  Do
  // not write an older controlled value back into an active AppKit editor on
  // those updates: doing so redraws the field and disturbs its insertion
  // point while the user is typing.
  const BOOL shouldSynchronizeText = activeControlChanged || oldViewProps.value != newViewProps.value;
  const BOOL shouldUpdatePlaceholder = activeControlChanged || oldViewProps.placeholder != newViewProps.placeholder;
  const BOOL shouldUpdateDisabled = activeControlChanged || oldViewProps.disabled != newViewProps.disabled;
  NSString *value = StringFromStdString(newViewProps.value);
  NSString *placeholder = StringFromStdString(newViewProps.placeholder);
  _synchronizing = YES;
  if (isMultiline) {
    if (shouldSynchronizeText && ![_multilineField.string isEqualToString:value]) {
      _multilineField.string = value;
    }
    if (shouldUpdateDisabled) {
      _multilineField.editable = !newViewProps.disabled;
      _multilineField.selectable = !newViewProps.disabled;
    }
  } else {
    NSTextField *activeField = newViewProps.secureTextEntry ? _secureField : _field;
    if (shouldSynchronizeText && ![activeField.stringValue isEqualToString:value]) {
      activeField.stringValue = value;
    }
    if (shouldUpdatePlaceholder) {
      activeField.placeholderString = placeholder;
    }
    if (shouldUpdateDisabled) {
      activeField.enabled = !newViewProps.disabled;
    }
  }
  _synchronizing = NO;

  if (activeControlChanged || shouldUpdatePlaceholder || shouldUpdateDisabled) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  _synchronizing = YES;
  _field.stringValue = @"";
  _secureField.stringValue = @"";
  _multilineField.string = @"";
  _synchronizing = NO;
  _field.placeholderString = nil;
  _secureField.placeholderString = nil;
  _field.enabled = YES;
  _secureField.enabled = YES;
  _multilineField.editable = YES;
  _multilineField.selectable = YES;
  _host.activeControl = _field;
}

- (void)invalidate
{
  _field.delegate = nil;
  _secureField.delegate = nil;
  _multilineField.delegate = nil;
  [super invalidate];
}

- (void)controlTextDidChange:(NSNotification *)notification
{
  if (_synchronizing || (notification.object != _field && notification.object != _secureField)) {
    return;
  }
  [self emitTextChanged:((NSTextField *)notification.object).stringValue];
}

- (void)controlTextDidEndEditing:(NSNotification *)notification
{
  if (notification.object == _field || notification.object == _secureField) {
    [self emitBlur];
  }
}

- (void)textDidChange:(NSNotification *)notification
{
  if (!_synchronizing && notification.object == _multilineField) {
    [self emitTextChanged:_multilineField.string];
  }
}

- (void)textDidEndEditing:(NSNotification *)notification
{
  if (notification.object == _multilineField) {
    [self emitBlur];
  }
}

- (void)submitted:(__unused id)sender
{
  if (!_eventEmitter) {
    return;
  }
  NSString *text = _host.activeControl == _scrollView ? _multilineField.string :
      (_host.activeControl == _secureField ? _secureField.stringValue : _field.stringValue);
  LiteLLMAppKitTextFieldEventEmitter::OnSubmitEditing event{StdStringFromString(text)};
  std::static_pointer_cast<const LiteLLMAppKitTextFieldEventEmitter>(_eventEmitter)->onSubmitEditing(event);
}

- (void)emitTextChanged:(NSString *)text
{
  if (_eventEmitter) {
    LiteLLMAppKitTextFieldEventEmitter::OnChangeText event{StdStringFromString(text)};
    std::static_pointer_cast<const LiteLLMAppKitTextFieldEventEmitter>(_eventEmitter)->onChangeText(event);
  }
}

- (void)emitBlur
{
  if (_eventEmitter) {
    std::static_pointer_cast<const LiteLLMAppKitTextFieldEventEmitter>(_eventEmitter)->onBlur({});
  }
}

- (NSView *)accessibilityElement
{
  return _host.activeControl == _scrollView ? _multilineField :
      (_host.activeControl == _secureField ? _secureField : _field);
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitTextFieldCls(void)
{
  return LiteLLMAppKitTextFieldComponentView.class;
}

@interface LiteLLMAppKitSwitchComponentView () <RCTLiteLLMAppKitSwitchViewProtocol>
@end

@implementation LiteLLMAppKitSwitchComponentView {
  NSSwitch *_switch;
  LiteLLMAppKitControlHostView *_host;
  BOOL _synchronizing;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSwitchComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSwitchProps>();
    _props = defaultProps;
    _switch = [[NSSwitch alloc] initWithFrame:NSZeroRect];
    _switch.target = self;
    _switch.action = @selector(changed:);
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    _host.control = _switch;
    self.contentView = _host;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitSwitchProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSwitchProps>(props);
  _synchronizing = YES;
  if (oldViewProps.value != newViewProps.value) {
    _switch.state = newViewProps.value ? NSControlStateValueOn : NSControlStateValueOff;
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _switch.enabled = !newViewProps.disabled;
  }
  _synchronizing = NO;
  [super updateProps:props oldProps:oldProps];
}

- (void)changed:(__unused id)sender
{
  if (!_synchronizing && _eventEmitter) {
    const BOOL value = _switch.state == NSControlStateValueOn;
    LiteLLMAppKitSwitchEventEmitter::OnValueChange event{static_cast<bool>(value)};
    std::static_pointer_cast<const LiteLLMAppKitSwitchEventEmitter>(_eventEmitter)->onValueChange(event);
  }
}

- (NSView *)accessibilityElement
{
  return _switch;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSwitchCls(void)
{
  return LiteLLMAppKitSwitchComponentView.class;
}

@interface LiteLLMAppKitSelectableRowComponentView () <RCTLiteLLMAppKitSelectableRowViewProtocol>
@end

@implementation LiteLLMAppKitSelectableRowComponentView {
  NSButton *_button;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSelectableRowComponentDescriptor>();
}

+ (void)load
{
  [super load];
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSelectableRowProps>();
    _props = defaultProps;
    _button = [NSButton buttonWithTitle:@"" target:self action:@selector(pressed:)];
    _button.bezelStyle = NSBezelStyleTexturedRounded;
    _button.buttonType = NSButtonTypePushOnPushOff;
    _button.alignment = NSTextAlignmentLeft;
    _button.lineBreakMode = NSLineBreakByTruncatingTail;
    _button.imagePosition = NSNoImage;
    self.contentView = _button;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitSelectableRowProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSelectableRowProps>(props);
  if (oldViewProps.title != newViewProps.title || oldViewProps.detail != newViewProps.detail) {
    NSString *title = StringFromStdString(newViewProps.title);
    _button.attributedTitle = SelectableRowTitle(title, StringFromStdString(newViewProps.detail));
    _button.toolTip = title;
    _button.accessibilityLabel = title;
  }
  if (oldViewProps.selected != newViewProps.selected) {
    _button.state = newViewProps.selected ? NSControlStateValueOn : NSControlStateValueOff;
  }
  if (oldViewProps.disabled != newViewProps.disabled) {
    _button.enabled = !newViewProps.disabled;
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)pressed:(__unused id)sender
{
  if (_eventEmitter) {
    std::static_pointer_cast<const LiteLLMAppKitSelectableRowEventEmitter>(_eventEmitter)->onPress({});
    const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitSelectableRowProps>(_props);
    _button.state = viewProps.selected ? NSControlStateValueOn : NSControlStateValueOff;
  }
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSelectableRowCls(void)
{
  return LiteLLMAppKitSelectableRowComponentView.class;
}

@interface LiteLLMAppKitTableComponentView () <NSTableViewDataSource, NSTableViewDelegate, RCTLiteLLMAppKitTableViewProtocol>
- (void)updateScrollerVisibility;
@end

@implementation LiteLLMAppKitTableComponentView {
  NSScrollView *_scrollView;
  NSTableView *_tableView;
  BOOL _synchronizingSelection;
  std::string _dataSignature;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitTableComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitTableProps>();
    _props = defaultProps;

    _tableView = [[NSTableView alloc] initWithFrame:NSZeroRect];
    _tableView.delegate = self;
    _tableView.dataSource = self;
    _tableView.target = self;
    _tableView.doubleAction = @selector(handleDoubleClick:);
    _tableView.allowsMultipleSelection = NO;
    _tableView.allowsEmptySelection = YES;
    _tableView.allowsColumnReordering = NO;
    _tableView.columnAutoresizingStyle = NSTableViewLastColumnOnlyAutoresizingStyle;
    _tableView.focusRingType = NSFocusRingTypeExterior;
    _tableView.intercellSpacing = NSZeroSize;
    _tableView.rowHeight = 28;
    _tableView.selectionHighlightStyle = NSTableViewSelectionHighlightStyleRegular;
    _tableView.usesAlternatingRowBackgroundColors = NO;

    _scrollView = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    _scrollView.autohidesScrollers = YES;
    _scrollView.borderType = NSBezelBorder;
    // Table columns are deliberately clipped and truncated rather than
    // horizontally scrolled. A horizontal track looks like an empty gutter
    // in the fixed-width provider pane; vertical scrolling is enabled only
    // when the rows actually exceed the viewport.
    _scrollView.hasHorizontalScroller = NO;
    _scrollView.hasVerticalScroller = NO;
    _scrollView.horizontalScrollElasticity = NSScrollElasticityNone;
    _scrollView.verticalScrollElasticity = NSScrollElasticityAutomatic;
    _scrollView.documentView = _tableView;
    self.contentView = _scrollView;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(props);
  const bool columnsChanged = oldViewProps.columnLabels != newViewProps.columnLabels ||
      oldViewProps.columnWidths != newViewProps.columnWidths;
  const bool compactChanged = oldViewProps.compact != newViewProps.compact;
  std::string nextDataSignature;
  nextDataSignature.reserve(newViewProps.rowKeys.size() * 24 + newViewProps.cells.size() * 32);
  for (const auto &key : newViewProps.rowKeys) {
    nextDataSignature.append(key).append("\x1f");
  }
  for (const auto &cell : newViewProps.cells) {
    nextDataSignature.append(cell).append("\x1f");
  }
  for (const auto &key : newViewProps.disabledRowKeys) {
    nextDataSignature.append("disabled:").append(key).append("\x1f");
  }
  const bool dataChanged = columnsChanged || compactChanged || nextDataSignature != _dataSignature;

  if (oldViewProps.alternatingRows != newViewProps.alternatingRows) {
    _tableView.usesAlternatingRowBackgroundColors = newViewProps.alternatingRows;
  }
  if (compactChanged) {
    _tableView.rowHeight = newViewProps.compact ? 22 : 28;
  }

  [super updateProps:props oldProps:oldProps];
  _synchronizingSelection = YES;

  if (columnsChanged) {
    while (_tableView.tableColumns.count > 0) {
      [_tableView removeTableColumn:_tableView.tableColumns.lastObject];
    }
    for (NSUInteger index = 0; index < newViewProps.columnLabels.size(); index++) {
      NSString *identifierValue = [NSString stringWithFormat:@"column-%lu", (unsigned long)index];
      NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:identifierValue];
      column.title = StringFromStdString(newViewProps.columnLabels[index]);
      const CGFloat width = index < newViewProps.columnWidths.size() && newViewProps.columnWidths[index] > 0
          ? static_cast<CGFloat>(newViewProps.columnWidths[index])
          : 160;
      // NSTableViewLastColumnOnlyAutoresizingStyle lets the final column
      // absorb a bordered clip view and a visible vertical scroller.  This
      // keeps fixed-width provider/model tables inside the viewport.
      column.minWidth = index + 1 == newViewProps.columnLabels.size() ? 1 : 48;
      column.width = width;
      [_tableView addTableColumn:column];
    }
  }

  if (dataChanged) {
    [_tableView reloadData];
    _dataSignature = std::move(nextDataSignature);
  }

  [self updateScrollerVisibility];
  if (newViewProps.followBottom && dataChanged) {
    dispatch_async(dispatch_get_main_queue(), ^{
      if (self->_tableView.numberOfRows > 0) {
        [self->_tableView scrollRowToVisible:self->_tableView.numberOfRows - 1];
      }
    });
  }

  const NSInteger selectedIndex = SegmentIndex(newViewProps.rowKeys, newViewProps.selectedKey);
  const BOOL selectionChanged = oldViewProps.selectedKey != newViewProps.selectedKey;
  if (selectedIndex >= 0) {
    if (selectionChanged || _tableView.selectedRow != selectedIndex) {
      [_tableView selectRowIndexes:[NSIndexSet indexSetWithIndex:selectedIndex] byExtendingSelection:NO];
    }
    if (selectionChanged) {
      [_tableView scrollRowToVisible:selectedIndex];
    }
  } else if (_tableView.selectedRow >= 0) {
    [_tableView deselectAll:nil];
  }
  _synchronizingSelection = NO;
}

- (void)layout
{
  [super layout];
  [self updateScrollerVisibility];
}

- (void)updateScrollerVisibility
{
  NSClipView *clipView = _scrollView.contentView;
  const CGFloat viewportHeight = NSHeight(clipView.bounds);
  if (!(viewportHeight > 0)) {
    return;
  }

  const CGFloat rowsHeight = _tableView.rowHeight * _tableView.numberOfRows;
  const BOOL needsVerticalScroller = rowsHeight > viewportHeight + 0.5;
  if (_scrollView.hasVerticalScroller != needsVerticalScroller) {
    _scrollView.hasVerticalScroller = needsVerticalScroller;
    [_scrollView tile];
  }

  const NSRect visibleBounds = _scrollView.contentView.bounds;
  const NSSize documentSize = NSMakeSize(
      NSWidth(visibleBounds),
      MAX(NSHeight(visibleBounds), rowsHeight));
  if (!NSEqualSizes(_tableView.frame.size, documentSize)) {
    _tableView.frame = NSMakeRect(0, 0, documentSize.width, documentSize.height);
  }
}

- (NSInteger)numberOfRowsInTableView:(__unused NSTableView *)tableView
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  return static_cast<NSInteger>(viewProps.rowKeys.size());
}

- (NSView *)tableView:(NSTableView *)tableView
   viewForTableColumn:(NSTableColumn *)tableColumn
                  row:(NSInteger)row
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSUInteger columnIndex = [tableView.tableColumns indexOfObject:tableColumn];
  const size_t columnCount = viewProps.columnLabels.size();
  if (row < 0 || columnIndex == NSNotFound || columnCount == 0) {
    return nil;
  }
  const size_t cellIndex = static_cast<size_t>(row) * columnCount + static_cast<size_t>(columnIndex);
  NSString *value = cellIndex < viewProps.cells.size() ? StringFromStdString(viewProps.cells[cellIndex]) : @"";
  NSUserInterfaceItemIdentifier identifier = @"LiteLLMAppKitTableCell";
  NSTableCellView *cell = [tableView makeViewWithIdentifier:identifier owner:self];
  if (cell == nil) {
    cell = [[NSTableCellView alloc] initWithFrame:NSZeroRect];
    cell.identifier = identifier;
    NSTextField *label = [NSTextField labelWithString:@""];
    label.translatesAutoresizingMaskIntoConstraints = NO;
    label.font = [NSFont systemFontOfSize:13];
    label.lineBreakMode = NSLineBreakByTruncatingTail;
    label.maximumNumberOfLines = 1;
    [cell addSubview:label];
    cell.textField = label;
    [NSLayoutConstraint activateConstraints:@[
      [label.leadingAnchor constraintEqualToAnchor:cell.leadingAnchor constant:8],
      [label.trailingAnchor constraintEqualToAnchor:cell.trailingAnchor constant:-8],
      [label.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor],
    ]];
  }
  NSTextField *label = cell.textField;
  label.stringValue = value;
  const bool disabled = std::find(viewProps.disabledRowKeys.begin(), viewProps.disabledRowKeys.end(), viewProps.rowKeys[static_cast<size_t>(row)]) != viewProps.disabledRowKeys.end();
  label.textColor = disabled ? NSColor.secondaryLabelColor : NSColor.labelColor;
  label.toolTip = value;
  label.accessibilityLabel = value;
  cell.toolTip = value;
  cell.accessibilityLabel = value;
  return cell;
}

- (void)tableViewSelectionDidChange:(NSNotification *)notification
{
  if (_synchronizingSelection || notification.object != _tableView || !_eventEmitter) {
    return;
  }
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSInteger selectedRow = _tableView.selectedRow;
  if (selectedRow < 0 || static_cast<size_t>(selectedRow) >= viewProps.rowKeys.size()) {
    return;
  }
  LiteLLMAppKitTableEventEmitter::OnSelectionChange event{
      viewProps.rowKeys[static_cast<size_t>(selectedRow)], static_cast<int>(selectedRow)};
  std::static_pointer_cast<const LiteLLMAppKitTableEventEmitter>(_eventEmitter)->onSelectionChange(event);
}

- (void)handleDoubleClick:(__unused id)sender
{
  if (!_eventEmitter) {
    return;
  }
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSInteger row = _tableView.clickedRow;
  if (row < 0 || static_cast<size_t>(row) >= viewProps.rowKeys.size()) {
    return;
  }
  LiteLLMAppKitTableEventEmitter::OnRowDoublePress event{
      viewProps.rowKeys[static_cast<size_t>(row)], static_cast<int>(row)};
  std::static_pointer_cast<const LiteLLMAppKitTableEventEmitter>(_eventEmitter)->onRowDoublePress(event);
}

- (NSView *)accessibilityElement
{
  return _tableView;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitTableCls(void)
{
  return LiteLLMAppKitTableComponentView.class;
}

@interface LiteLLMAppKitTextEditorComponentView () <NSTextViewDelegate, RCTLiteLLMAppKitTextEditorViewProtocol>
@end

@implementation LiteLLMAppKitTextEditorComponentView {
  NSScrollView *_scrollView;
  NSTextView *_textView;
  NSMutableDictionary<NSString *, LiteLLMTextEditorViewportState *> *_viewportStates;
  NSString *_documentKey;
  BOOL _synchronizingText;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitTextEditorComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitTextEditorProps>();
    _props = defaultProps;

    _textView = [[NSTextView alloc] initWithFrame:NSZeroRect];
    _textView.allowsUndo = YES;
    _textView.delegate = self;
    _textView.font = [NSFont monospacedSystemFontOfSize:12 weight:NSFontWeightRegular];
    _textView.richText = NO;
    _textView.textContainerInset = NSMakeSize(6, 6);
    _textView.usesFindPanel = YES;
    _textView.verticallyResizable = YES;
    _textView.horizontallyResizable = NO;
    _textView.autoresizingMask = NSViewWidthSizable;
    _textView.textContainer.widthTracksTextView = YES;
    _textView.textContainer.containerSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);

    _scrollView = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    _scrollView.autohidesScrollers = YES;
    _scrollView.borderType = NSBezelBorder;
    _scrollView.hasVerticalScroller = YES;
    _scrollView.documentView = _textView;
    self.contentView = _scrollView;
    _viewportStates = [NSMutableDictionary new];
    _documentKey = @"";
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitTextEditorProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitTextEditorProps>(props);
  NSString *nextDocumentKey = StringFromStdString(newViewProps.documentKey);
  BOOL documentChanged = ![_documentKey isEqualToString:nextDocumentKey];
  if (documentChanged && _documentKey.length > 0) {
    _viewportStates[_documentKey] = CaptureTextEditorViewport(_scrollView, _textView);
  }

  if (oldViewProps.value != newViewProps.value) {
    NSString *value = StringFromStdString(newViewProps.value);
    if (![_textView.string isEqualToString:value]) {
      LiteLLMTextEditorViewportState *previousState = CaptureTextEditorViewport(_scrollView, _textView);
      _synchronizingText = YES;
      _textView.string = value;
      _synchronizingText = NO;
      RestoreTextEditorState(
          _scrollView,
          _textView,
          documentChanged ? _viewportStates[nextDocumentKey] : previousState);
    }
  } else if (documentChanged) {
    RestoreTextEditorState(_scrollView, _textView, _viewportStates[nextDocumentKey]);
  }
  _documentKey = nextDocumentKey;
  if (oldViewProps.readOnly != newViewProps.readOnly) {
    _textView.editable = !newViewProps.readOnly;
    _textView.selectable = YES;
  }
  if (oldViewProps.wrap != newViewProps.wrap) {
    _scrollView.hasHorizontalScroller = !newViewProps.wrap;
    _textView.horizontallyResizable = !newViewProps.wrap;
    _textView.autoresizingMask = newViewProps.wrap ? NSViewWidthSizable : NSViewNotSizable;
    _textView.textContainer.widthTracksTextView = newViewProps.wrap;
    _textView.textContainer.containerSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);
  }

  [super updateProps:props oldProps:oldProps];
}

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  [_viewportStates removeAllObjects];
  _documentKey = @"";
  _synchronizingText = YES;
  _textView.string = @"";
  _synchronizingText = NO;
}

- (void)textDidChange:(NSNotification *)notification
{
  if (_synchronizingText || notification.object != _textView || !_eventEmitter) {
    return;
  }
  LiteLLMAppKitTextEditorEventEmitter::OnChangeText event{StdStringFromString(_textView.string)};
  std::static_pointer_cast<const LiteLLMAppKitTextEditorEventEmitter>(_eventEmitter)->onChangeText(event);
}

- (NSView *)accessibilityElement
{
  return _textView;
}

@end


Class<RCTComponentViewProtocol> LiteLLMAppKitTextEditorCls(void)
{
  return LiteLLMAppKitTextEditorComponentView.class;
}

@interface LiteLLMAppKitSecureTextEditorComponentView () <NSTextViewDelegate, RCTLiteLLMAppKitSecureTextEditorViewProtocol>
@end

@implementation LiteLLMAppKitSecureTextEditorComponentView {
  NSScrollView *_scrollView;
  NSTextView *_textView;
  NSString *_editorToken;
  BOOL _synchronizingText;
  BOOL _stageInFlight;
  BOOL _stageQueued;
  NSUInteger _lifecycleGeneration;
  NSUInteger _debounceGeneration;
  NSUInteger _editGeneration;
  BOOL _loadRecoveryAttempted;
  BOOL _stageRecoveryAttempted;
  NSInteger _lastRevision;
  NSString *_lastStatus;
  NSString *_lastError;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSecureTextEditorComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSecureTextEditorProps>();
    _props = defaultProps;

    _textView = [[NSTextView alloc] initWithFrame:NSZeroRect];
    _textView.allowsUndo = YES;
    _textView.automaticDashSubstitutionEnabled = NO;
    _textView.automaticQuoteSubstitutionEnabled = NO;
    _textView.automaticSpellingCorrectionEnabled = NO;
    _textView.automaticTextReplacementEnabled = NO;
    _textView.delegate = self;
    _textView.editable = NO;
    _textView.font = [NSFont monospacedSystemFontOfSize:12 weight:NSFontWeightRegular];
    _textView.horizontallyResizable = YES;
    _textView.maxSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);
    _textView.minSize = NSZeroSize;
    _textView.richText = NO;
    _textView.selectable = YES;
    _textView.textContainer.containerSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);
    _textView.textContainer.widthTracksTextView = NO;
    _textView.textContainerInset = NSMakeSize(6, 6);
    _textView.usesFindPanel = YES;
    _textView.verticallyResizable = YES;

    _scrollView = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    _scrollView.autohidesScrollers = YES;
    _scrollView.borderType = NSBezelBorder;
    _scrollView.hasHorizontalScroller = YES;
    _scrollView.hasVerticalScroller = YES;
    _scrollView.documentView = _textView;
    self.contentView = _scrollView;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitSecureTextEditorProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSecureTextEditorProps>(props);

  if (oldViewProps.language != newViewProps.language) {
    NSString *language = StringFromStdString(newViewProps.language);
    _textView.accessibilityLabel = language.length > 0
        ? [NSString stringWithFormat:@"%@ source editor", language]
        : @"Source editor";
  }
  if (oldViewProps.editorToken != newViewProps.editorToken) {
    [self loadEditorToken:StringFromStdString(newViewProps.editorToken)];
  }

  [super updateProps:props oldProps:oldProps];
}

- (void)updateEventEmitter:(const EventEmitter::Shared &)eventEmitter
{
  [super updateEventEmitter:eventEmitter];
  if (_lastStatus.length > 0) {
    [self emitRevision:_lastRevision status:_lastStatus error:_lastError ?: @""];
  }
}

- (void)loadEditorToken:(NSString *)editorToken
{
  _lifecycleGeneration += 1;
  _debounceGeneration += 1;
  _editGeneration = 0;
  _loadRecoveryAttempted = NO;
  _stageRecoveryAttempted = NO;
  _stageInFlight = NO;
  _stageQueued = NO;
  _lastRevision = 0;
  _editorToken = [editorToken copy];
  _textView.editable = NO;
  _synchronizingText = YES;
  _textView.string = @"";
  _synchronizingText = NO;

  if (_editorToken.length == 0) {
    [self emitRevision:0 status:@"error" error:@"invalid_token"];
    return;
  }

  const NSUInteger generation = _lifecycleGeneration;
  NSString *requestedToken = [_editorToken copy];
  [self emitRevision:0 status:@"loading" error:@""];
  __weak LiteLLMAppKitSecureTextEditorComponentView *weakSelf = self;
  [CoreIPCBridge.shared readEditorDocument:requestedToken completion:^(NSString *_Nullable text, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextEditorComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_lifecycleGeneration ||
        ![requestedToken isEqualToString:strongSelf->_editorToken]) {
      return;
    }
    if (error.length > 0 || text == nil) {
      [strongSelf recoverInitialLoadForGeneration:generation failedToken:requestedToken];
      return;
    }
    strongSelf->_synchronizingText = YES;
    strongSelf->_textView.string = text;
    strongSelf->_synchronizingText = NO;
    strongSelf->_textView.editable = YES;
    [strongSelf emitRevision:0 status:@"ready" error:@""];
  }];
}

- (void)recoverInitialLoadForGeneration:(NSUInteger)generation failedToken:(NSString *)failedToken
{
  if (generation != _lifecycleGeneration || _loadRecoveryAttempted ||
      ![failedToken isEqualToString:_editorToken]) {
    [self emitRevision:0 status:@"error" error:@"read_failed"];
    return;
  }
  _loadRecoveryAttempted = YES;
  __weak LiteLLMAppKitSecureTextEditorComponentView *weakSelf = self;
  [CoreIPCBridge.shared refreshEditorDocument:failedToken completion:^(NSString *_Nullable replacementToken, NSString *_Nullable text, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextEditorComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_lifecycleGeneration ||
        ![failedToken isEqualToString:strongSelf->_editorToken]) {
      return;
    }
    if (error.length > 0 || replacementToken.length == 0 || text == nil) {
      [strongSelf emitRevision:0 status:@"error" error:@"read_failed"];
      return;
    }
    strongSelf->_editorToken = [replacementToken copy];
    strongSelf->_synchronizingText = YES;
    strongSelf->_textView.string = text;
    strongSelf->_synchronizingText = NO;
    strongSelf->_textView.editable = YES;
    [strongSelf emitRevision:0 status:@"ready" error:@""];
  }];
}

- (void)textDidChange:(NSNotification *)notification
{
  if (_synchronizingText || notification.object != _textView || _editorToken.length == 0) {
    return;
  }
  _editGeneration += 1;
  _stageRecoveryAttempted = NO;
  [self emitRevision:_lastRevision status:@"dirty" error:@""];
  [self scheduleStageAfter:0.35];
}

- (void)scheduleStageAfter:(NSTimeInterval)delay
{
  const NSUInteger generation = _lifecycleGeneration;
  const NSUInteger debounceGeneration = ++_debounceGeneration;
  __weak LiteLLMAppKitSecureTextEditorComponentView *weakSelf = self;
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
    LiteLLMAppKitSecureTextEditorComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_lifecycleGeneration ||
        debounceGeneration != strongSelf->_debounceGeneration) {
      return;
    }
    [strongSelf stageCurrentTextForGeneration:generation];
  });
}

- (void)stageCurrentTextForGeneration:(NSUInteger)generation
{
  if (generation != _lifecycleGeneration || _editorToken.length == 0) {
    return;
  }
  if (_stageInFlight) {
    _stageQueued = YES;
    return;
  }

  _stageInFlight = YES;
  NSString *stagedToken = [_editorToken copy];
  NSString *stagedText = [_textView.string copy];
  const NSUInteger stagedEditGeneration = _editGeneration;
  [self emitRevision:_lastRevision status:@"saving" error:@""];
  __weak LiteLLMAppKitSecureTextEditorComponentView *weakSelf = self;
  [CoreIPCBridge.shared stageEditorDocument:stagedToken text:stagedText completion:^(NSNumber *_Nullable revision, NSString *_Nullable replacementToken, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextEditorComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_lifecycleGeneration ||
        ![stagedToken isEqualToString:strongSelf->_editorToken]) {
      return;
    }

    strongSelf->_stageInFlight = NO;
    const BOOL hasNewerText = strongSelf->_stageQueued ||
        stagedEditGeneration != strongSelf->_editGeneration;
    strongSelf->_stageQueued = NO;
    if (error.length > 0 || revision == nil || replacementToken.length == 0 ||
        replacementToken.length > 256) {
      [strongSelf recoverStageForGeneration:generation failedToken:stagedToken];
      return;
    }

    strongSelf->_lastRevision = MAX(0, revision.integerValue);
    // The replacement capability is intentionally retained in this native
    // view. It never becomes a prop or event payload visible to React.
    strongSelf->_editorToken = [replacementToken copy];
    if (hasNewerText) {
      [strongSelf emitRevision:strongSelf->_lastRevision status:@"dirty" error:@""];
      [strongSelf scheduleStageAfter:0];
    } else {
      [strongSelf emitRevision:strongSelf->_lastRevision status:@"saved" error:@""];
    }
  }];
}

- (void)recoverStageForGeneration:(NSUInteger)generation failedToken:(NSString *)failedToken
{
  if (generation != _lifecycleGeneration || _stageRecoveryAttempted ||
      ![failedToken isEqualToString:_editorToken]) {
    _textView.editable = YES;
    [self emitRevision:_lastRevision status:@"error" error:@"stage_failed"];
    return;
  }
  _stageRecoveryAttempted = YES;
  _stageInFlight = YES;
  [self emitRevision:_lastRevision status:@"saving" error:@""];
  __weak LiteLLMAppKitSecureTextEditorComponentView *weakSelf = self;
  [CoreIPCBridge.shared refreshEditorDocument:failedToken completion:^(NSString *_Nullable replacementToken, __unused NSString *_Nullable diskText, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextEditorComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_lifecycleGeneration ||
        ![failedToken isEqualToString:strongSelf->_editorToken]) {
      return;
    }
    strongSelf->_stageInFlight = NO;
    if (error.length > 0 || replacementToken.length == 0) {
      strongSelf->_textView.editable = YES;
      [strongSelf emitRevision:strongSelf->_lastRevision status:@"error" error:@"stage_failed"];
      return;
    }
    strongSelf->_editorToken = [replacementToken copy];
    [strongSelf stageCurrentTextForGeneration:generation];
  }];
}

- (void)emitRevision:(NSInteger)revision status:(NSString *)status error:(NSString *)error
{
  _lastRevision = MIN(MAX(0, revision), INT32_MAX);
  _lastStatus = [status copy];
  _lastError = [error copy];
  if (!_eventEmitter) {
    return;
  }
  LiteLLMAppKitSecureTextEditorEventEmitter::OnEditorState event{
      static_cast<int>(_lastRevision), StdStringFromString(_lastStatus), StdStringFromString(_lastError)};
  std::static_pointer_cast<const LiteLLMAppKitSecureTextEditorEventEmitter>(_eventEmitter)->onEditorState(event);
}

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  _lifecycleGeneration += 1;
  _debounceGeneration += 1;
  _editorToken = nil;
  _stageInFlight = NO;
  _stageQueued = NO;
  _lastRevision = 0;
  _loadRecoveryAttempted = NO;
  _stageRecoveryAttempted = NO;
  _lastStatus = nil;
  _lastError = nil;
  _synchronizingText = YES;
  _textView.string = @"";
  _synchronizingText = NO;
  _textView.editable = NO;
}

- (void)invalidate
{
  _lifecycleGeneration += 1;
  _debounceGeneration += 1;
  _editorToken = nil;
  _lastStatus = nil;
  _lastError = nil;
  _textView.delegate = nil;
  [super invalidate];
}

- (NSView *)accessibilityElement
{
  return _textView;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSecureTextEditorCls(void)
{
  return LiteLLMAppKitSecureTextEditorComponentView.class;
}

@interface LiteLLMAppKitSecureTextInputComponentView () <NSTextFieldDelegate, RCTLiteLLMAppKitSecureTextInputViewProtocol>
- (NSTextField *)activeField;
- (void)stageCurrentSecretForRequest:(NSInteger)commitRequest;
- (void)loadProviderAPIKeyForTarget:(NSString *)target generation:(NSUInteger)generation;
@end

@implementation LiteLLMAppKitSecureTextInputComponentView {
  NSSecureTextField *_field;
  NSTextField *_plainField;
  LiteLLMAppKitControlHostView *_host;
  NSString *_domain;
  NSString *_secretField;
  NSString *_target;
  NSString *_label;
  NSInteger _lastCommitRequest;
  NSInteger _lastResetRequest;
  NSInteger _lastRevision;
  BOOL _lastPresent;
  NSString *_lastStatus;
  NSString *_lastError;
  NSUInteger _generation;
  BOOL _stageInFlight;
  BOOL _autoCommit;
  BOOL _secretDirty;
  BOOL _synchronizingField;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSecureTextInputComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSecureTextInputProps>();
    _props = defaultProps;
    _field = [[NSSecureTextField alloc] initWithFrame:NSZeroRect];
    _field.delegate = self;
    _field.target = self;
    _field.action = @selector(submitSecret:);
    _field.bezelStyle = NSTextFieldRoundedBezel;
    _field.font = [NSFont systemFontOfSize:13];
    _field.maximumNumberOfLines = 1;
    _field.usesSingleLineMode = YES;
    _field.lineBreakMode = NSLineBreakByTruncatingTail;
    NSTextFieldCell *fieldCell = (NSTextFieldCell *)_field.cell;
    fieldCell.wraps = NO;
    fieldCell.scrollable = YES;
    _plainField = [[NSTextField alloc] initWithFrame:NSZeroRect];
    _plainField.delegate = self;
    _plainField.target = self;
    _plainField.action = @selector(submitSecret:);
    _plainField.bezelStyle = NSTextFieldRoundedBezel;
    _plainField.font = [NSFont systemFontOfSize:13];
    _plainField.maximumNumberOfLines = 1;
    _plainField.usesSingleLineMode = YES;
    _plainField.lineBreakMode = NSLineBreakByTruncatingTail;
    NSTextFieldCell *plainFieldCell = (NSTextFieldCell *)_plainField.cell;
    plainFieldCell.wraps = NO;
    plainFieldCell.scrollable = YES;
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    // Match ordinary text fields: preserve the native bezel height and center
    // it inside the shared 30pt form row.
    _host.fillsHeight = NO;
    _host.control = _field;
    self.contentView = _host;
    _domain = @"";
    _secretField = @"";
    _target = @"";
    _label = @"";
    _lastStatus = @"";
    _lastError = @"";
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitSecureTextInputProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSecureTextInputProps>(props);
  NSString *domain = StringFromStdString(newViewProps.domain);
  NSString *field = StringFromStdString(newViewProps.field);
  NSString *target = StringFromStdString(newViewProps.target);
  NSString *label = StringFromStdString(newViewProps.label);
  NSTextField *activeField = newViewProps.plainText ? _plainField : _field;
  NSTextField *inactiveField = newViewProps.plainText ? _field : _plainField;
  if (_host.control != activeField) {
    _host.control = activeField;
  }
  BOOL identityChanged = ![_domain isEqualToString:domain] || ![_secretField isEqualToString:field] ||
      ![_target isEqualToString:target];
  if (identityChanged) {
    _generation += 1;
    _stageInFlight = NO;
    _secretDirty = NO;
    _domain = domain;
    _secretField = field;
    _target = target;
    _synchronizingField = YES;
    _field.stringValue = @"";
    _plainField.stringValue = @"";
    _synchronizingField = NO;
    _lastRevision = 0;
    _lastPresent = NO;
    _lastStatus = @"ready";
    _lastError = @"";
  }
  _autoCommit = newViewProps.autoCommit;
  _label = label;
  activeField.placeholderString = StringFromStdString(newViewProps.placeholder);
  activeField.accessibilityLabel = label;
  activeField.enabled = !newViewProps.disabled && !_stageInFlight;
  inactiveField.enabled = NO;

  if (newViewProps.resetRequest != oldViewProps.resetRequest &&
      newViewProps.resetRequest != _lastResetRequest) {
    _lastResetRequest = newViewProps.resetRequest;
    _synchronizingField = YES;
    _field.stringValue = @"";
    _plainField.stringValue = @"";
    _synchronizingField = NO;
    _secretDirty = NO;
    if (!_stageInFlight) [self emitRevision:_lastRevision present:_lastPresent status:@"ready" error:@"" commitRequest:_lastCommitRequest];
  }
  if (newViewProps.commitRequest != oldViewProps.commitRequest &&
      newViewProps.commitRequest != _lastCommitRequest) {
    [self stageCurrentSecretForRequest:newViewProps.commitRequest];
  }
  if (identityChanged && newViewProps.plainText && newViewProps.autoCommit &&
      [domain isEqualToString:@"providers_models"] && [field isEqualToString:@"api_key"] &&
      target.length > 0) {
    [self loadProviderAPIKeyForTarget:target generation:_generation];
  }
  [_host setNeedsLayout:YES];
  [super updateProps:props oldProps:oldProps];
}

- (void)updateEventEmitter:(const EventEmitter::Shared &)eventEmitter
{
  [super updateEventEmitter:eventEmitter];
  if (_lastStatus.length > 0) {
    [self emitRevision:_lastRevision present:_lastPresent status:_lastStatus error:_lastError commitRequest:_lastCommitRequest];
  }
}

- (void)stageCurrentSecretForRequest:(NSInteger)commitRequest
{
  if (_stageInFlight) return;
  NSString *value = [[self activeField].stringValue copy];
  const BOOL allowEmptyValue = _autoCommit && [_domain isEqualToString:@"providers_models"] &&
      [_secretField isEqualToString:@"api_key"];
  if (value.length == 0 && !allowEmptyValue) {
    _lastCommitRequest = MAX(_lastCommitRequest, commitRequest);
    [self emitRevision:_lastRevision present:_lastPresent status:@"ready" error:@"" commitRequest:_lastCommitRequest];
    return;
  }
  if (_domain.length == 0 || _secretField.length == 0 || value.length > 16 * 1024) {
    _synchronizingField = YES;
    _field.stringValue = @"";
    _plainField.stringValue = @"";
    _synchronizingField = NO;
    _lastCommitRequest = MAX(_lastCommitRequest, commitRequest);
    [self emitRevision:_lastRevision present:_lastPresent status:@"error" error:@"invalid_secret" commitRequest:_lastCommitRequest];
    return;
  }

  _stageInFlight = YES;
  [self activeField].enabled = NO;
  _lastCommitRequest = MAX(_lastCommitRequest, commitRequest);
  const NSUInteger generation = _generation;
  NSString *domain = [_domain copy];
  NSString *field = [_secretField copy];
  NSString *target = [_target copy];
  NSString *secret = value;
  const BOOL preservePlainText = [_host.control isEqual:_plainField];
  if (!preservePlainText) {
    _synchronizingField = YES;
    _field.stringValue = @"";
    _plainField.stringValue = @"";
    _synchronizingField = NO;
  }
  _secretDirty = NO;
  [self emitRevision:_lastRevision present:_lastPresent status:@"saving" error:@"" commitRequest:_lastCommitRequest];
  __weak LiteLLMAppKitSecureTextInputComponentView *weakSelf = self;
  [CoreIPCBridge.shared stageSecretForDomain:domain
                                        field:field
                                       target:(target.length > 0 ? target : nil)
                                       value:secret
                                  completion:^(NSNumber *_Nullable revision, NSNumber *_Nullable present, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextInputComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_generation) return;
    strongSelf->_stageInFlight = NO;
    [strongSelf activeField].enabled = !(std::static_pointer_cast<const LiteLLMAppKitSecureTextInputProps>(strongSelf->_props)->disabled);
    if (error.length > 0 || revision == nil || present == nil) {
      strongSelf->_secretDirty = preservePlainText;
      [strongSelf emitRevision:strongSelf->_lastRevision present:strongSelf->_lastPresent status:@"error" error:@"stage_failed" commitRequest:strongSelf->_lastCommitRequest];
      return;
    }
    [strongSelf emitRevision:revision.integerValue present:present.boolValue status:@"saved" error:@"" commitRequest:strongSelf->_lastCommitRequest];
  }];
}

- (void)loadProviderAPIKeyForTarget:(NSString *)target generation:(NSUInteger)generation
{
  __weak LiteLLMAppKitSecureTextInputComponentView *weakSelf = self;
  [CoreIPCBridge.shared readProviderAPIKeyForTarget:target completion:^(NSString *_Nullable value, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextInputComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_generation ||
        ![strongSelf->_host.control isEqual:strongSelf->_plainField]) return;
    if (error.length > 0 || value == nil) {
      [strongSelf emitRevision:strongSelf->_lastRevision present:strongSelf->_lastPresent status:@"error" error:@"read_failed" commitRequest:strongSelf->_lastCommitRequest];
      return;
    }
    strongSelf->_synchronizingField = YES;
    strongSelf->_plainField.stringValue = value;
    strongSelf->_synchronizingField = NO;
    strongSelf->_secretDirty = NO;
  }];
}

- (void)submitSecret:(__unused id)sender
{
  if (!_autoCommit || !_secretDirty) return;
  [self stageCurrentSecretForRequest:_lastCommitRequest + 1];
}

- (void)controlTextDidChange:(NSNotification *)notification
{
  if (_synchronizingField || notification.object != [self activeField]) return;
  _secretDirty = YES;
  [self emitRevision:_lastRevision present:_lastPresent status:@"dirty" error:@"" commitRequest:_lastCommitRequest];
}

- (void)controlTextDidEndEditing:(NSNotification *)notification
{
  if (_synchronizingField || notification.object != [self activeField] || !_autoCommit || !_secretDirty) return;
  [self stageCurrentSecretForRequest:_lastCommitRequest + 1];
}

- (void)emitRevision:(NSInteger)revision present:(BOOL)present status:(NSString *)status error:(NSString *)error commitRequest:(NSInteger)commitRequest
{
  _lastRevision = MIN(MAX(0, revision), INT32_MAX);
  _lastPresent = present;
  _lastStatus = [status copy];
  _lastError = [error copy];
  _lastCommitRequest = MIN(MAX(0, commitRequest), INT32_MAX);
  if (!_eventEmitter) return;
  LiteLLMAppKitSecureTextInputEventEmitter::OnSecretState event{
      static_cast<int>(_lastRevision), static_cast<bool>(_lastPresent), StdStringFromString(_lastStatus),
      StdStringFromString(_lastError), static_cast<int>(_lastCommitRequest)};
  std::static_pointer_cast<const LiteLLMAppKitSecureTextInputEventEmitter>(_eventEmitter)->onSecretState(event);
}

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  _generation += 1;
  _stageInFlight = NO;
  _field.stringValue = @"";
  _plainField.stringValue = @"";
  _domain = @"";
  _secretField = @"";
  _target = @"";
  _lastStatus = @"";
  _lastError = @"";
}

- (void)invalidate
{
  _generation += 1;
  _field.stringValue = @"";
  _plainField.stringValue = @"";
  [super invalidate];
}

- (NSTextField *)activeField
{
  return [_host.control isKindOfClass:NSTextField.class] ? (NSTextField *)_host.control : _field;
}

- (NSView *)accessibilityElement
{
  return [self activeField];
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSecureTextInputCls(void)
{
  return LiteLLMAppKitSecureTextInputComponentView.class;
}

@interface LiteLLMAppKitSplitHostView : NSSplitView
@property(nonatomic, assign) BOOL dividerEnabled;
@end

@implementation LiteLLMAppKitSplitHostView

- (void)mouseDown:(NSEvent *)event
{
  if (!self.dividerEnabled) {
    return;
  }
  [super mouseDown:event];
}

@end

@interface LiteLLMAppKitSplitViewComponentView () <NSSplitViewDelegate, RCTLiteLLMAppKitSplitViewViewProtocol>
@end

@implementation LiteLLMAppKitSplitViewComponentView {
  LiteLLMAppKitSplitHostView *_splitView;
  CGFloat _minimumPaneWidth;
  CGFloat _maximumPaneWidth;
  CGFloat _requestedPaneWidth;
  BOOL _paneOpen;
  BOOL _synchronizingDivider;
  BOOL _needsInitialPaneLayout;
  BOOL _pendingInitialPaneReplay;
  NSUInteger _paneReplayGeneration;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitSplitViewComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitSplitViewProps>();
    _props = defaultProps;
    _splitView = [[LiteLLMAppKitSplitHostView alloc] initWithFrame:NSZeroRect];
    _splitView.arrangesAllSubviews = NO;
    _splitView.delegate = self;
    _splitView.dividerEnabled = YES;
    _splitView.dividerStyle = NSSplitViewDividerStyleThin;
    _splitView.vertical = YES;
    _needsInitialPaneLayout = YES;
    _paneReplayGeneration = 1;
    self.contentView = _splitView;
  }
  return self;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitSplitViewProps>(props);
  _minimumPaneWidth = std::max<CGFloat>(0, static_cast<CGFloat>(newViewProps.minPaneWidth));
  _maximumPaneWidth = std::max<CGFloat>(_minimumPaneWidth, static_cast<CGFloat>(newViewProps.maxPaneWidth));
  _requestedPaneWidth = std::clamp(
      static_cast<CGFloat>(newViewProps.paneWidth), _minimumPaneWidth, _maximumPaneWidth);
  _paneOpen = newViewProps.paneOpen;
  _splitView.dividerEnabled = !newViewProps.disabled;
  [self applyRequestedPaneWidth];
  [super updateProps:props oldProps:oldProps];
}

- (void)mountChildComponentView:(RCTPlatformView<RCTComponentViewProtocol> *)childComponentView
                          index:(NSInteger)index
{
  if (_splitView.arrangedSubviews.count == 0) {
    _needsInitialPaneLayout = YES;
  }
  [_splitView insertArrangedSubview:childComponentView atIndex:index];
  // Mount-time constraint resolution chooses a provisional divider position.
  // Do not feed that implementation detail back through React before the
  // requested pane width has been applied.
  _synchronizingDivider = YES;
  [_splitView adjustSubviews];
  _synchronizingDivider = NO;
  [self applyRequestedPaneWidth];
  if (_splitView.arrangedSubviews.count == 2) {
    [self scheduleInitialPaneReplay];
  }
}

- (void)layout
{
  [super layout];
  if (!_needsInitialPaneLayout || _splitView.arrangedSubviews.count < 2 ||
      _splitView.bounds.size.width <= 0) {
    return;
  }
  _needsInitialPaneLayout = NO;
  // NSSplitView may choose an equal split while its children first acquire
  // their real frames. Reapply the React value once that layout has settled.
  [self applyRequestedPaneWidth];
}

- (void)updateLayoutMetrics:(const LayoutMetrics &)layoutMetrics
           oldLayoutMetrics:(const LayoutMetrics &)oldLayoutMetrics
{
  [super updateLayoutMetrics:layoutMetrics oldLayoutMetrics:oldLayoutMetrics];
  // Fabric owns the outer component frame, but NSSplitView owns the frames
  // of its arranged panes. Keep its content view in the new bounds and then
  // restore the controlled divider after Fabric has committed a resize.
  _splitView.frame = self.bounds;
  [self applyRequestedPaneWidth];
}

- (void)unmountChildComponentView:(RCTPlatformView<RCTComponentViewProtocol> *)childComponentView
                            index:(__unused NSInteger)index
{
  _paneReplayGeneration += 1;
  _pendingInitialPaneReplay = NO;
  [_splitView removeArrangedSubview:childComponentView];
  [childComponentView removeFromSuperview];
  _synchronizingDivider = YES;
  [_splitView adjustSubviews];
  _synchronizingDivider = NO;
}

- (void)scheduleInitialPaneReplay
{
  if (_pendingInitialPaneReplay) {
    return;
  }
  _pendingInitialPaneReplay = YES;
  const NSUInteger generation = _paneReplayGeneration;
  __weak LiteLLMAppKitSplitViewComponentView *weakSelf = self;
  dispatch_async(dispatch_get_main_queue(), ^{
    LiteLLMAppKitSplitViewComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || strongSelf->_paneReplayGeneration != generation) {
      return;
    }
    strongSelf->_pendingInitialPaneReplay = NO;
    if (strongSelf->_splitView.arrangedSubviews.count == 2) {
      // Fabric finishes attaching the child views after mountChildComponentView.
      // Replay the controlled divider on the next main-loop turn, once that
      // AppKit layout pass has settled.
      [strongSelf applyRequestedPaneWidth];
    }
  });
}

- (void)applyRequestedPaneWidth
{
  if (_splitView.arrangedSubviews.count < 2) {
    return;
  }
  _synchronizingDivider = YES;
  NSView *leadingPane = _splitView.arrangedSubviews.firstObject;
  leadingPane.hidden = !_paneOpen;
  if (_paneOpen) {
    [_splitView setPosition:_requestedPaneWidth ofDividerAtIndex:0];
  } else {
    [_splitView setPosition:0 ofDividerAtIndex:0];
  }
  _synchronizingDivider = NO;
}

- (CGFloat)splitView:(__unused NSSplitView *)splitView
    constrainMinCoordinate:(CGFloat)proposedMinimumPosition
           ofSubviewAt:(NSInteger)dividerIndex
{
  return dividerIndex == 0 ? std::max(proposedMinimumPosition, _minimumPaneWidth) : proposedMinimumPosition;
}

- (CGFloat)splitView:(__unused NSSplitView *)splitView
    constrainMaxCoordinate:(CGFloat)proposedMaximumPosition
           ofSubviewAt:(NSInteger)dividerIndex
{
  return dividerIndex == 0 ? std::min(proposedMaximumPosition, _maximumPaneWidth) : proposedMaximumPosition;
}

- (CGFloat)splitView:(__unused NSSplitView *)splitView
    constrainSplitPosition:(CGFloat)proposedPosition
           ofSubviewAt:(NSInteger)dividerIndex
{
  return dividerIndex == 0
      ? std::clamp(proposedPosition, _minimumPaneWidth, _maximumPaneWidth)
      : proposedPosition;
}

- (void)splitViewDidResizeSubviews:(NSNotification *)notification
{
  if (_synchronizingDivider || notification.object != _splitView || !_eventEmitter ||
      _splitView.arrangedSubviews.count < 2 ||
      ![notification.userInfo[@"NSSplitViewDividerIndex"] isKindOfClass:NSNumber.class]) {
    return;
  }
  const CGFloat width = _splitView.arrangedSubviews.firstObject.frame.size.width;
  LiteLLMAppKitSplitViewEventEmitter::OnPaneWidthChange event{static_cast<float>(width)};
  std::static_pointer_cast<const LiteLLMAppKitSplitViewEventEmitter>(_eventEmitter)->onPaneWidthChange(event);
}

- (void)splitView:(NSSplitView *)splitView resizeSubviewsWithOldSize:(NSSize)oldSize
{
  if (splitView != _splitView) {
    return;
  }
  _synchronizingDivider = YES;
  [splitView adjustSubviews];
  [self applyRequestedPaneWidth];
  _synchronizingDivider = NO;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitSplitViewCls(void)
{
  return LiteLLMAppKitSplitViewComponentView.class;
}
