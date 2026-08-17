#import "AppKitControlViews.h"

#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <React/RCTComponent.h>
#import <React/RCTUIKit.h>
#import <WebKit/WebKit.h>
#import "LiteLLMMenu-Swift.h"

#import <react/renderer/components/LiteLLMMacControls/ComponentDescriptors.h>
#import <react/renderer/components/LiteLLMMacControls/EventEmitters.h>
#import <react/renderer/components/LiteLLMMacControls/Props.h>
#import <react/renderer/components/LiteLLMMacControls/RCTComponentViewHelpers.h>

#include <algorithm>
#include <cmath>
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

constexpr CGFloat LiteLLMUIFontSize = 13.0;
constexpr CGFloat LiteLLMTableMinimumHorizontalPadding = 8.0;
constexpr CGFloat LiteLLMTableHeaderHorizontalPadding = 6.0;

NSDictionary<NSString *, id> *ImmediateLayerActions()
{
  static NSDictionary<NSString *, id> *actions;
  static dispatch_once_t onceToken;
  dispatch_once(&onceToken, ^{
    id disabled = NSNull.null;
    actions = @{
      @"backgroundColor": disabled,
      @"bounds": disabled,
      @"contents": disabled,
      @"opacity": disabled,
      @"position": disabled,
      @"shadowColor": disabled,
      @"shadowOffset": disabled,
      @"shadowOpacity": disabled,
      @"shadowPath": disabled,
      @"shadowRadius": disabled,
      @"transform": disabled,
    };
  });
  return actions;
}

void ConfigureImmediateView(NSView *view)
{
  if (view == nil) return;
  view.wantsLayer = YES;
  view.layer.actions = ImmediateLayerActions();
}

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

void ConfigureSingleLineTextField(NSTextField *field)
{
  field.focusRingType = NSFocusRingTypeNone;
  field.maximumNumberOfLines = 1;
  field.usesSingleLineMode = YES;
  field.lineBreakMode = NSLineBreakByTruncatingTail;
  NSTextFieldCell *cell = (NSTextFieldCell *)field.cell;
  cell.wraps = NO;
  cell.scrollable = YES;
}

NSInteger SegmentIndex(const std::vector<std::string> &labels, const std::string &selectedValue)
{
  const auto selected = std::find(labels.begin(), labels.end(), selectedValue);
  return selected == labels.end() ? -1 : static_cast<NSInteger>(std::distance(labels.begin(), selected));
}

NSAttributedString *TableHeaderTitle(NSString *title)
{
  NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
  paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
  paragraph.firstLineHeadIndent = LiteLLMTableHeaderHorizontalPadding;
  paragraph.headIndent = LiteLLMTableHeaderHorizontalPadding;
  paragraph.tailIndent = -LiteLLMTableHeaderHorizontalPadding;

  return [[NSAttributedString alloc] initWithString:title attributes:@{
    NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightMedium],
    NSForegroundColorAttributeName: NSColor.labelColor,
    NSParagraphStyleAttributeName: paragraph,
  }];
}

NSImage *ButtonSymbolImage(const std::string &symbol)
{
  NSString *symbolName = nil;
  if (symbol == "check") {
    symbolName = @"checkmark";
  } else if (symbol == "close") {
    symbolName = @"xmark";
  } else if (symbol == "copy") {
    symbolName = @"doc.on.doc";
  } else if (symbol == "edit") {
    symbolName = @"pencil";
  } else if (symbol == "import") {
    symbolName = @"tray.and.arrow.down";
  } else if (symbol == "power-off") {
    symbolName = @"power";
  } else if (symbol == "power-on") {
    symbolName = @"power.circle.fill";
  } else if (symbol == "minus") {
    symbolName = @"minus";
  } else if (symbol == "pause") {
    symbolName = @"pause.fill";
  } else if (symbol == "play") {
    symbolName = @"play.fill";
  } else if (symbol == "plus") {
    symbolName = @"plus";
  } else if (symbol == "refresh") {
    symbolName = @"arrow.clockwise";
  } else if (symbol == "trash") {
    symbolName = @"trash";
  }
  return symbolName == nil
      ? nil
      : [NSImage imageWithSystemSymbolName:symbolName accessibilityDescription:nil];
}

NSFont *TableCellFont()
{
  return [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightRegular];
}

NSAttributedString *TableCellTitle(NSString *title, NSColor *color)
{
  NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
  paragraph.lineBreakMode = NSLineBreakByTruncatingTail;

  return [[NSAttributedString alloc] initWithString:title attributes:@{
    NSFontAttributeName: TableCellFont(),
    NSForegroundColorAttributeName: color,
    NSParagraphStyleAttributeName: paragraph,
  }];
}

NSAttributedString *SelectableRowTitle(NSString *title, NSString *detail)
{
  NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
  paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
  paragraph.maximumLineHeight = 16;

  NSDictionary *titleAttributes = @{
    NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightRegular],
    NSForegroundColorAttributeName: NSColor.labelColor,
    NSParagraphStyleAttributeName: paragraph,
  };
  NSMutableAttributedString *result = [[NSMutableAttributedString alloc] initWithString:title attributes:titleAttributes];
  if (detail.length > 0) {
    NSMutableParagraphStyle *detailParagraph = [paragraph mutableCopy];
    detailParagraph.maximumLineHeight = 14;
    [result appendAttributedString:[[NSAttributedString alloc] initWithString:[@"\n" stringByAppendingString:detail]
                                                                    attributes:@{
      NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize],
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

BOOL TableIsFollowingBottom(NSScrollView *scrollView, NSTableView *tableView)
{
  NSRect visibleRect = scrollView.contentView.bounds;
  NSRect documentRect = tableView.bounds;
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

static NSUserInterfaceItemIdentifier const LiteLLMTabStopIdentifier = @"LiteLLMTabStop";

static BOOL LiteLLMTabStopIsEligible(NSView *view)
{
  if (view == nil || view.window == nil || view.hiddenOrHasHiddenAncestor || NSIsEmptyRect(view.visibleRect)) {
    return NO;
  }
  if ([view isKindOfClass:NSControl.class] && !((NSControl *)view).enabled) {
    return NO;
  }
  if ([view isKindOfClass:NSTextField.class] && !((NSTextField *)view).editable) {
    return NO;
  }
  if ([view isKindOfClass:NSTextView.class] && !((NSTextView *)view).editable) {
    return NO;
  }
  return YES;
}

static void CollectLiteLLMTabStops(NSView *view, NSMutableArray<NSView *> *result)
{
  if ([view.identifier isEqualToString:LiteLLMTabStopIdentifier] && LiteLLMTabStopIsEligible(view)) {
    [result addObject:view];
  }
  for (NSView *subview in view.subviews) {
    CollectLiteLLMTabStops(subview, result);
  }
}

static BOOL FocusAdjacentLiteLLMTabStop(NSView *source, BOOL backwards)
{
  NSWindow *window = source.window;
  if (window == nil || window.contentView == nil) {
    return NO;
  }
  NSMutableArray<NSView *> *stops = [NSMutableArray array];
  CollectLiteLLMTabStops(window.contentView, stops);
  [stops sortUsingComparator:^NSComparisonResult(NSView *left, NSView *right) {
    NSRect leftRect = [left convertRect:left.bounds toView:nil];
    NSRect rightRect = [right convertRect:right.bounds toView:nil];
    const CGFloat vertical = NSMaxY(leftRect) - NSMaxY(rightRect);
    if (fabs(vertical) > 1.0) {
      return vertical > 0 ? NSOrderedAscending : NSOrderedDescending;
    }
    const CGFloat horizontal = NSMinX(leftRect) - NSMinX(rightRect);
    if (fabs(horizontal) > 1.0) {
      return horizontal < 0 ? NSOrderedAscending : NSOrderedDescending;
    }
    return NSOrderedSame;
  }];
  if (stops.count == 0) {
    return NO;
  }
  NSInteger current = [stops indexOfObjectIdenticalTo:source];
  if (current == NSNotFound) {
    current = backwards ? 0 : stops.count - 1;
  }
  for (NSInteger offset = 1; offset <= stops.count; offset += 1) {
    NSInteger next = backwards
        ? (current - offset + stops.count) % stops.count
        : (current + offset) % stops.count;
    if ([window makeFirstResponder:stops[static_cast<NSUInteger>(next)]]) {
      return YES;
    }
  }
  return NO;
}

static BOOL HandleLiteLLMTabKey(NSView *source, NSEvent *event)
{
  if (event.type != NSEventTypeKeyDown || event.keyCode != 48) {
    return NO;
  }
  return FocusAdjacentLiteLLMTabStop(source, (event.modifierFlags & NSEventModifierFlagShift) != 0);
}

static BOOL HandleLiteLLMTabCommand(NSView *source, SEL commandSelector)
{
  if (commandSelector == @selector(insertTab:)) {
    return FocusAdjacentLiteLLMTabStop(source, NO);
  }
  if (commandSelector == @selector(insertBacktab:)) {
    return FocusAdjacentLiteLLMTabStop(source, YES);
  }
  return NO;
}

@interface LiteLLMTabButton : NSButton
@end

@implementation LiteLLMTabButton

- (BOOL)acceptsFirstResponder
{
  return self.enabled && !self.hidden;
}

- (void)keyDown:(NSEvent *)event
{
  if (HandleLiteLLMTabKey(self, event)) return;
  [super keyDown:event];
}

@end

@interface LiteLLMTabPopUpButton : NSPopUpButton
@end

@implementation LiteLLMTabPopUpButton

- (BOOL)acceptsFirstResponder
{
  return self.enabled && !self.hidden;
}

- (void)keyDown:(NSEvent *)event
{
  if (HandleLiteLLMTabKey(self, event)) return;
  [super keyDown:event];
}

@end

@interface LiteLLMTabSegmentedControl : NSSegmentedControl
@end

@implementation LiteLLMTabSegmentedControl

- (BOOL)acceptsFirstResponder
{
  return self.enabled && !self.hidden;
}

- (void)keyDown:(NSEvent *)event
{
  if (HandleLiteLLMTabKey(self, event)) return;
  [super keyDown:event];
}

@end


@interface LiteLLMTabSwitch : NSButton
@end

@implementation LiteLLMTabSwitch

- (BOOL)acceptsFirstResponder
{
  return self.enabled && !self.hidden;
}

- (void)keyDown:(NSEvent *)event
{
  if (HandleLiteLLMTabKey(self, event)) return;
  [super keyDown:event];
}

@end

@interface LiteLLMTabTextField : NSTextField
@end

@implementation LiteLLMTabTextField

- (BOOL)acceptsFirstResponder
{
  return self.enabled && self.editable;
}

- (void)resetCursorRects
{
  [super resetCursorRects];
  if (self.enabled && self.editable) {
    [self addCursorRect:self.bounds cursor:[NSCursor IBeamCursor]];
  }
}

@end

@interface LiteLLMTabSecureTextField : NSSecureTextField
@end

@implementation LiteLLMTabSecureTextField

- (BOOL)acceptsFirstResponder
{
  return self.enabled && self.editable;
}

- (void)resetCursorRects
{
  [super resetCursorRects];
  if (self.enabled && self.editable) {
    [self addCursorRect:self.bounds cursor:[NSCursor IBeamCursor]];
  }
}

@end

@interface LiteLLMTabTextView : NSTextView
@end

@implementation LiteLLMTabTextView

- (BOOL)acceptsFirstResponder
{
  return self.editable;
}

- (void)keyDown:(NSEvent *)event
{
  if (HandleLiteLLMTabKey(self, event)) return;
  [super keyDown:event];
}

@end

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

- (instancetype)initWithFrame:(NSRect)frame
{
  if (self = [super initWithFrame:frame]) {
    ConfigureImmediateView(self);
  }
  return self;
}

- (void)setControl:(NSView *)control
{
  if (_control == control) {
    return;
  }
  [_control removeFromSuperview];
  _control = control;
  if (_control != nil) {
    ConfigureImmediateView(_control);
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

// NSTableView's data viewport begins below its header, while NSClipView uses a
// negative Y origin to keep that header visible. Treat that AppKit origin as
// the locked resting position when all rows fit.  A table is nested inside the
// settings ScrollView, so a wheel event must continue to that ancestor when
// the table has no overflow or is already at the relevant edge.
NSScrollView *ParentScrollView(NSView *view);
BOOL TableScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event, BOOL acceptsVerticalScroll, BOOL acceptsHorizontalScroll);
BOOL ForwardWheelToParent(NSView *view, NSEvent *event);

@interface LiteLLMTableScrollView : NSScrollView
@property(nonatomic) BOOL acceptsVerticalScroll;
@property(nonatomic) BOOL acceptsHorizontalScroll;
@end

@implementation LiteLLMTableScrollView

- (void)scrollWheel:(NSEvent *)event
{
  if (TableScrollViewCanConsume(self, event, _acceptsVerticalScroll, _acceptsHorizontalScroll)) {
    [super scrollWheel:event];
    return;
  }
  if (ForwardWheelToParent(self, event)) return;
  if (_acceptsVerticalScroll || _acceptsHorizontalScroll) [super scrollWheel:event];
}

@end

// Wheel events in the unused part of an NSScrollView's viewport are delivered
// to its clip view rather than to the document view.  Swallowing the event in
// just the table therefore still permits the empty lower area to rubber-band.
// Keep the same overflow contract on that intermediate responder as well.
@interface LiteLLMTableClipView : NSClipView
@property(nonatomic) BOOL acceptsVerticalScroll;
@property(nonatomic) BOOL acceptsHorizontalScroll;
@end

@implementation LiteLLMTableClipView

- (void)scrollWheel:(NSEvent *)event
{
  NSScrollView *owner = ParentScrollView(self);
  if (owner != nil && [owner isKindOfClass:LiteLLMTableScrollView.class] &&
      TableScrollViewCanConsume(owner, event, ((LiteLLMTableScrollView *)owner).acceptsVerticalScroll, ((LiteLLMTableScrollView *)owner).acceptsHorizontalScroll)) {
    [super scrollWheel:event];
    return;
  }
  if (ForwardWheelToParent(self, event)) return;
  if (_acceptsVerticalScroll || _acceptsHorizontalScroll) [super scrollWheel:event];
}

@end

@interface LiteLLMTableView : NSTableView
@property(nonatomic) BOOL acceptsVerticalScroll;
@property(nonatomic) BOOL acceptsHorizontalScroll;
@end

@implementation LiteLLMTableView

- (void)scrollWheel:(NSEvent *)event
{
  NSScrollView *owner = ParentScrollView(self);
  if (owner != nil && [owner isKindOfClass:LiteLLMTableScrollView.class] &&
      TableScrollViewCanConsume(owner, event, ((LiteLLMTableScrollView *)owner).acceptsVerticalScroll, ((LiteLLMTableScrollView *)owner).acceptsHorizontalScroll)) {
    [super scrollWheel:event];
    return;
  }
  if (ForwardWheelToParent(self, event)) return;
  if (_acceptsVerticalScroll || _acceptsHorizontalScroll) [super scrollWheel:event];
}

@end

// AppKit applies its compact/bold group-row typography to the textField of an
// NSTableCellView whenever a row is marked as a group.  The route table still
// needs the group-row marker so the title spans all columns, but the title
// itself should use the same compact regular font as every ordinary cell.  A
// plain NSView keeps the label out of AppKit's automatic group-cell styling
// while preserving the native spanning-row layout.
@interface LiteLLMTableGroupCellView : NSView
@property(nonatomic, strong) NSTextField *label;
@end

@implementation LiteLLMTableGroupCellView

- (instancetype)initWithFrame:(NSRect)frame
{
  if (self = [super initWithFrame:frame]) {
    _label = [NSTextField labelWithString:@""];
    _label.translatesAutoresizingMaskIntoConstraints = NO;
    _label.font = TableCellFont();
    _label.lineBreakMode = NSLineBreakByTruncatingTail;
    _label.maximumNumberOfLines = 1;
    [self addSubview:_label];
    [NSLayoutConstraint activateConstraints:@[
      [_label.leadingAnchor constraintEqualToAnchor:self.leadingAnchor constant:8],
      [_label.trailingAnchor constraintEqualToAnchor:self.trailingAnchor constant:-8],
      [_label.centerYAnchor constraintEqualToAnchor:self.centerYAnchor],
    ]];
  }
  return self;
}

@end

// Keep the table border outside the scroll view.  NSScrollView's bezel is
// tiled together with its clip view and scrollbars, so its trailing edge can
// disappear behind a vertical scroller or become thicker at the header/body
// seam.  A fixed frame layer leaves the scrolling chrome entirely inside one
// continuous border.
@interface LiteLLMTableFrameView : NSView
@property(nonatomic, strong, nullable) NSView *framedContentView;
@property(nonatomic, assign, getter=isFramed) BOOL framed;
@end

@implementation LiteLLMTableFrameView

- (instancetype)initWithFrame:(NSRect)frame
{
  if (self = [super initWithFrame:frame]) {
    self.wantsLayer = YES;
    self.layer.masksToBounds = YES;
    _framed = YES;
    self.layer.borderWidth = 1.0;
  }
  return self;
}

- (BOOL)wantsUpdateLayer
{
  return YES;
}

- (void)updateLayer
{
  self.layer.backgroundColor = NSColor.clearColor.CGColor;
  self.layer.borderColor = NSColor.separatorColor.CGColor;
}

- (void)setFramed:(BOOL)framed
{
  if (_framed == framed) {
    return;
  }
  _framed = framed;
  self.layer.borderWidth = framed ? 1.0 : 0.0;
  [self setNeedsLayout:YES];
}

- (void)setFramedContentView:(NSView *)framedContentView
{
  if (_framedContentView == framedContentView) {
    return;
  }
  [_framedContentView removeFromSuperview];
  _framedContentView = framedContentView;
  if (_framedContentView != nil) {
    _framedContentView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    [self addSubview:_framedContentView];
  }
  [self setNeedsLayout:YES];
}

- (void)layout
{
  [super layout];
  const CGFloat inset = self.framed ? 1.0 : 0.0;
  _framedContentView.frame = NSInsetRect(self.bounds, inset, inset);
}

@end

@interface LiteLLMNavigationLinkButton : LiteLLMTabButton
@property(nonatomic) BOOL linkMode;
@property(nonatomic) BOOL defaultAction;
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

- (void)applyDefaultAction
{
  self.keyEquivalent = self.defaultAction ? @"\r" : @"";
  if (!self.defaultAction && self.window.defaultButtonCell == self.cell) {
    [self.window setDefaultButtonCell:nil];
  }
}

- (void)setDefaultAction:(BOOL)defaultAction
{
  _defaultAction = defaultAction;
  [self applyDefaultAction];
}

- (void)viewDidMoveToWindow
{
  [super viewDidMoveToWindow];
  [self applyDefaultAction];
  __weak LiteLLMNavigationLinkButton *weakSelf = self;
  dispatch_async(dispatch_get_main_queue(), ^{
    [weakSelf applyDefaultAction];
  });
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
    NSFontAttributeName: self.font ?: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightSemibold],
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
    _button.identifier = LiteLLMTabStopIdentifier;
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
  const BOOL symbolChanged = oldViewProps.symbol != newViewProps.symbol;
  const BOOL linkChanged = oldViewProps.link != newViewProps.link;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;
  const BOOL disabledChanged = oldViewProps.disabled != newViewProps.disabled;
  BOOL link = newViewProps.link;
  BOOL useCompactControl = newViewProps.compact && !link;
  BOOL defaultAction = !link && newViewProps.primary && !newViewProps.disabled;

  if (titleChanged || symbolChanged) {
    NSString *title = StringFromStdString(newViewProps.title);
    NSImage *symbolImage = ButtonSymbolImage(newViewProps.symbol);
    _button.image = symbolImage;
    _button.imagePosition = symbolImage == nil ? NSNoImage : NSImageOnly;
    _button.title = symbolImage == nil ? title : @"";
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
  if (disabledChanged) {
    _button.enabled = !newViewProps.disabled;
  }
  if (linkChanged ||
      oldViewProps.primary != newViewProps.primary ||
      oldViewProps.destructive != newViewProps.destructive ||
      compactChanged ||
      disabledChanged) {
    _button.bezelStyle = link ? NSBezelStyleInline : NSBezelStyleRounded;
    _button.bezelColor = nil;
    ((LiteLLMNavigationLinkButton *)_button).defaultAction = defaultAction;
    _button.hasDestructiveAction = !link && newViewProps.destructive;
    _button.controlSize = useCompactControl ? NSControlSizeSmall : NSControlSizeRegular;
    _button.font = link
        ? [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightSemibold]
        : [NSFont systemFontOfSize:LiteLLMUIFontSize];
    ((LiteLLMNavigationLinkButton *)_button).linkMode = link;
  }
  if (!link && !newViewProps.primary) {
    _button.bezelColor = nil;
    ((LiteLLMNavigationLinkButton *)_button).defaultAction = NO;
    _button.contentTintColor = nil;
  }
  if (!link && linkChanged) {
    _button.contentTintColor = nil;
    _button.title = ButtonSymbolImage(newViewProps.symbol) == nil
        ? StringFromStdString(newViewProps.title)
        : @"";
  }

  if (titleChanged || symbolChanged || linkChanged || compactChanged) {
    [_host setNeedsLayout:YES];
  }
  [super updateProps:props oldProps:oldProps];
}

- (void)prepareForRecycle
{
  if (_button.window.defaultButtonCell == _button.cell) {
    [_button.window setDefaultButtonCell:nil];
  }
  [super prepareForRecycle];
  static const auto defaultProps = std::make_shared<const LiteLLMAppKitButtonProps>();
  _props = defaultProps;
  ((LiteLLMNavigationLinkButton *)_button).linkMode = NO;
  _button.image = nil;
  _button.imagePosition = NSNoImage;
  _button.title = @"";
  _button.toolTip = nil;
  _button.accessibilityLabel = nil;
  _button.enabled = YES;
  _button.bezelStyle = NSBezelStyleRounded;
  _button.bezelColor = nil;
  _button.buttonType = NSButtonTypeMomentaryPushIn;
  ((LiteLLMNavigationLinkButton *)_button).defaultAction = NO;
  _button.hasDestructiveAction = NO;
  _button.controlSize = NSControlSizeRegular;
  _button.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
  _button.contentTintColor = nil;
  [_host setNeedsLayout:YES];
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
    _checkbox = [[LiteLLMTabButton alloc] initWithFrame:NSZeroRect];
    _checkbox.buttonType = NSButtonTypeSwitch;
    _checkbox.title = @"";
    _checkbox.target = self;
    _checkbox.action = @selector(changed:);
    _checkbox.identifier = LiteLLMTabStopIdentifier;
    _checkbox.controlSize = NSControlSizeRegular;
    _checkbox.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
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
  const BOOL labelVisibilityChanged = oldViewProps.labelVisible != newViewProps.labelVisible;
  const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;
  if (labelChanged || labelVisibilityChanged) {
    NSString *label = StringFromStdString(newViewProps.label);
    _checkbox.title = newViewProps.labelVisible ? label : @"";
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
    _checkbox.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
  }
  _synchronizing = NO;
  // A checked value or enabled state does not alter intrinsic geometry.  Do
  // not request a host layout while AppKit is handling a user click: that
  // needless pass is visible as a control flash on dense provider screens.
  if (labelChanged || labelVisibilityChanged || compactChanged) {
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
    _picker = [[LiteLLMTabPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    _picker.identifier = LiteLLMTabStopIdentifier;
    _picker.controlSize = NSControlSizeRegular;
    _picker.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
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
    _picker.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
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

    _control = [[LiteLLMTabSegmentedControl alloc] initWithFrame:NSZeroRect];
    _control.identifier = LiteLLMTabStopIdentifier;
    _control.segmentStyle = NSSegmentStyleRounded;
    _control.trackingMode = NSSegmentSwitchTrackingSelectOne;
    _control.controlSize = NSControlSizeRegular;
    _control.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
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
    _control.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
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

// Structured settings place multiline fields inside the outer settings
// ScrollView.  Forward wheel events to that ancestor when the small field
// viewport is already at its top/bottom (or has no overflow), so scrolling
// over a textarea continues moving the settings page instead of stopping.
@interface LiteLLMTextFieldScrollView : NSScrollView
@end

NSScrollView *ParentScrollView(NSView *view)
{
  NSView *candidate = view.superview;
  while (candidate != nil) {
    if ([candidate isKindOfClass:NSScrollView.class]) {
      return (NSScrollView *)candidate;
    }
    candidate = candidate.superview;
  }
  return nil;
}

BOOL TableScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event, BOOL acceptsVerticalScroll, BOOL acceptsHorizontalScroll)
{
  if (scrollView == nil || scrollView.documentView == nil || scrollView.contentView == nil) {
    return NO;
  }

  const CGFloat deltaY = event.scrollingDeltaY;
  const CGFloat deltaX = event.scrollingDeltaX;
  const NSRect visible = [scrollView.documentView convertRect:scrollView.contentView.bounds fromView:scrollView.contentView];
  const NSRect document = scrollView.documentView.bounds;

  if (fabs(deltaY) >= 0.01) {
    if (!acceptsVerticalScroll) return NO;
    const CGFloat minimumY = NSMinY(document);
    const CGFloat maximumY = NSMaxY(document) - NSHeight(visible);
    if (maximumY <= minimumY + 0.5) return NO;
    const CGFloat offsetY = NSMinY(visible);
    const BOOL atTop = offsetY <= minimumY + 0.5;
    const BOOL atBottom = offsetY >= maximumY - 0.5;
    const BOOL movingUp = deltaY > 0;
    if (movingUp ? atTop : atBottom) return NO;
  }

  if (fabs(deltaX) >= 0.01) {
    if (!acceptsHorizontalScroll) return NO;
    const CGFloat minimumX = NSMinX(document);
    const CGFloat maximumX = NSMaxX(document) - NSWidth(visible);
    if (maximumX <= minimumX + 0.5) return NO;
    const CGFloat offsetX = NSMinX(visible);
    const BOOL atLeft = offsetX <= minimumX + 0.5;
    const BOOL atRight = offsetX >= maximumX - 0.5;
    const BOOL movingLeft = deltaX > 0;
    if (movingLeft ? atLeft : atRight) return NO;
  }

  return YES;
}

BOOL ForwardWheelToParent(NSView *view, NSEvent *event)
{
  NSScrollView *parent = ParentScrollView(view);
  if (parent != nil && parent != view) {
    [parent scrollWheel:event];
    return YES;
  }
  return NO;
}

BOOL TextFieldScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event)
{
  const CGFloat deltaY = event.scrollingDeltaY;
  if (fabs(deltaY) < 0.01) {
    return YES;
  }
  NSClipView *clipView = scrollView.contentView;
  NSView *documentView = scrollView.documentView;
  if (clipView == nil || documentView == nil) {
    return NO;
  }
  const CGFloat maxOffset = MAX(0.0, NSHeight(documentView.bounds) - NSHeight(clipView.bounds));
  if (maxOffset <= 0.5) {
    return NO;
  }
  const CGFloat offset = clipView.bounds.origin.y;
  const BOOL atTop = offset <= 0.5;
  const BOOL atBottom = offset >= maxOffset - 0.5;
  const BOOL movingUp = deltaY > 0;
  return !(movingUp ? atTop : atBottom);
}

@implementation LiteLLMTextFieldScrollView

- (void)scrollWheel:(NSEvent *)event
{
  if (TextFieldScrollViewCanConsume(self, event)) {
    [super scrollWheel:event];
    return;
  }
  NSScrollView *parent = ParentScrollView(self);
  if (parent != nil && parent != self) {
    [parent scrollWheel:event];
    return;
  }
  [super scrollWheel:event];
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
    _field = [[LiteLLMTabTextField alloc] initWithFrame:NSZeroRect];
    _field.identifier = LiteLLMTabStopIdentifier;
    _field.delegate = self;
    _field.target = self;
    _field.action = @selector(submitted:);
    _field.bezelStyle = NSTextFieldRoundedBezel;
    _field.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
    ConfigureSingleLineTextField(_field);

    _secureField = [[LiteLLMTabSecureTextField alloc] initWithFrame:NSZeroRect];
    _secureField.identifier = LiteLLMTabStopIdentifier;
    _secureField.delegate = self;
    _secureField.target = self;
    _secureField.action = @selector(submitted:);
    _secureField.bezelStyle = NSTextFieldRoundedBezel;
    _secureField.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
    ConfigureSingleLineTextField(_secureField);

    _multilineField = [[LiteLLMTabTextView alloc] initWithFrame:NSZeroRect];
    _multilineField.identifier = LiteLLMTabStopIdentifier;
    _multilineField.delegate = self;
    _multilineField.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
    _multilineField.usesFindPanel = YES;
    _multilineField.richText = NO;
    _multilineField.allowsUndo = YES;
    _multilineField.verticallyResizable = YES;
    _multilineField.horizontallyResizable = YES;
    _multilineField.textContainer.containerSize = NSMakeSize(CGFLOAT_MAX, CGFLOAT_MAX);
    _multilineField.textContainer.widthTracksTextView = YES;
    _multilineField.textContainerInset = NSMakeSize(5, 4);
    _scrollView = [[LiteLLMTextFieldScrollView alloc] initWithFrame:NSZeroRect];
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

- (BOOL)control:(__unused NSControl *)control
       textView:(__unused NSTextView *)textView
doCommandBySelector:(SEL)commandSelector
{
  return HandleLiteLLMTabCommand(_host.activeControl, commandSelector);
}

- (BOOL)textView:(NSTextView *)textView doCommandBySelector:(SEL)commandSelector
{
  return HandleLiteLLMTabCommand(textView, commandSelector);
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
  NSButton *_switch;
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
    _switch = [LiteLLMTabSwitch checkboxWithTitle:@"" target:self action:@selector(changed:)];
    _switch.identifier = LiteLLMTabStopIdentifier;
    _switch.controlSize = NSControlSizeRegular;
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
- (void)updateColumnMinimumWidths;
- (void)updateScrollerVisibility;
- (void)tableColumnDidResize:(NSNotification *)notification;
- (BOOL)isSpanningRow:(NSInteger)row;
@end

@implementation LiteLLMAppKitTableComponentView {
  LiteLLMTableFrameView *_frameView;
  LiteLLMTableScrollView *_scrollView;
  LiteLLMTableClipView *_clipView;
  LiteLLMTableView *_tableView;
  BOOL _synchronizingSelection;
  BOOL _settingColumnWidths;
  std::vector<CGFloat> _automaticColumnAdjustments;
  std::vector<CGFloat> _measuredColumnWidths;
  std::vector<CGFloat> _requestedColumnWidths;
  std::vector<bool> _userResizedColumns;
  BOOL _hasLoadedData;
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

    _tableView = [[LiteLLMTableView alloc] initWithFrame:NSZeroRect];
    _tableView.delegate = self;
    _tableView.dataSource = self;
    _tableView.target = self;
    _tableView.action = @selector(handleRowClick:);
    _tableView.doubleAction = @selector(handleDoubleClick:);
    _tableView.allowsMultipleSelection = NO;
    _tableView.allowsEmptySelection = YES;
    _tableView.allowsColumnReordering = NO;
    _tableView.columnAutoresizingStyle = NSTableViewNoColumnAutoresizing;
    _tableView.focusRingType = NSFocusRingTypeExterior;
    _tableView.style = NSTableViewStylePlain;
    _tableView.intercellSpacing = NSZeroSize;
    _tableView.rowHeight = 28;
    _tableView.selectionHighlightStyle = NSTableViewSelectionHighlightStyleRegular;
    _tableView.usesAlternatingRowBackgroundColors = NO;
    _tableView.floatsGroupRows = NO;
    // Keep the body as an AppKit list, rather than a boxed spreadsheet.  The
    // optional outer bezel is disabled when a surrounding split view already
    // owns the structural divider.
    _tableView.gridStyleMask = NSTableViewGridNone;
    _hasLoadedData = NO;

    _scrollView = [[LiteLLMTableScrollView alloc] initWithFrame:NSZeroRect];
    _scrollView.scrollerStyle = NSScrollerStyleLegacy;
    _scrollView.autohidesScrollers = YES;
    _scrollView.borderType = NSNoBorder;
    _scrollView.hasHorizontalScroller = NO;
    _scrollView.hasVerticalScroller = NO;
    _scrollView.horizontalScrollElasticity = NSScrollElasticityNone;
    _scrollView.verticalScrollElasticity = NSScrollElasticityNone;
    _scrollView.acceptsVerticalScroll = NO;
    _scrollView.acceptsHorizontalScroll = NO;
    _tableView.acceptsVerticalScroll = NO;
    _tableView.acceptsHorizontalScroll = NO;
    _clipView = [[LiteLLMTableClipView alloc] initWithFrame:NSZeroRect];
    _clipView.acceptsVerticalScroll = NO;
    _clipView.acceptsHorizontalScroll = NO;
    _scrollView.contentView = _clipView;
    _scrollView.documentView = _tableView;
    _frameView = [[LiteLLMTableFrameView alloc] initWithFrame:NSZeroRect];
    _frameView.framedContentView = _scrollView;
    self.contentView = _frameView;
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(tableColumnDidResize:)
                                                 name:NSTableViewColumnDidResizeNotification
                                               object:_tableView];
  }
  return self;
}

- (void)dealloc
{
  [[NSNotificationCenter defaultCenter] removeObserver:self
                                                  name:NSTableViewColumnDidResizeNotification
                                                object:_tableView];
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(props);
  const bool columnsChanged = oldViewProps.columnLabels != newViewProps.columnLabels ||
      oldViewProps.columnWidths != newViewProps.columnWidths;
  const bool compactChanged = oldViewProps.compact != newViewProps.compact;
  const bool paddingChanged = oldViewProps.cellHorizontalPadding != newViewProps.cellHorizontalPadding;
  const bool firstColumnPaddingChanged = oldViewProps.firstColumnHorizontalPadding != newViewProps.firstColumnHorizontalPadding;
  const bool overflowBehaviorChanged = oldViewProps.preserveColumnWidths != newViewProps.preserveColumnWidths ||
      oldViewProps.scrollTrailingColumnOverflow != newViewProps.scrollTrailingColumnOverflow;
  const bool rowsChanged = oldViewProps.rowKeys != newViewProps.rowKeys ||
      oldViewProps.cells != newViewProps.cells ||
      oldViewProps.disabledRowKeys != newViewProps.disabledRowKeys ||
      oldViewProps.secondaryCellKeys != newViewProps.secondaryCellKeys ||
      oldViewProps.spanningRowKeys != newViewProps.spanningRowKeys;
  const bool dataChanged = columnsChanged || compactChanged || paddingChanged || firstColumnPaddingChanged ||
      overflowBehaviorChanged || rowsChanged;
  const BOOL initialDataLoad = !_hasLoadedData;
  const BOOL wasFollowingBottom = newViewProps.followBottom && dataChanged
      ? (initialDataLoad || TableIsFollowingBottom(_scrollView, _tableView))
      : NO;

  _tableView.usesAlternatingRowBackgroundColors = newViewProps.alternatingRows;
  _frameView.framed = !newViewProps.borderless;
  if (compactChanged) {
    _tableView.rowHeight = newViewProps.compact ? 22 : 28;
    if (_tableView.headerView != nil) {
      NSRect headerFrame = _tableView.headerView.frame;
      headerFrame.size.height = newViewProps.compact ? 24 : 28;
      _tableView.headerView.frame = headerFrame;
      [_scrollView tile];
    }
  }

  [super updateProps:props oldProps:oldProps];
  _synchronizingSelection = YES;

  if (columnsChanged) {
    _settingColumnWidths = YES;
    while (_tableView.tableColumns.count > 0) {
      [_tableView removeTableColumn:_tableView.tableColumns.lastObject];
    }
    _automaticColumnAdjustments.clear();
    _measuredColumnWidths.clear();
    _requestedColumnWidths.clear();
    _userResizedColumns.clear();
    for (NSUInteger index = 0; index < newViewProps.columnLabels.size(); index++) {
      NSString *identifierValue = [NSString stringWithFormat:@"column-%lu", (unsigned long)index];
      NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:identifierValue];
      NSString *columnTitle = StringFromStdString(newViewProps.columnLabels[index]);
      column.title = columnTitle;
      column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);
      column.headerCell.bordered = NO;
      column.headerCell.bezeled = NO;
      const CGFloat width = index < newViewProps.columnWidths.size() && newViewProps.columnWidths[index] > 0
          ? static_cast<CGFloat>(newViewProps.columnWidths[index])
          : 160;
      column.minWidth = 1;
      column.maxWidth = CGFLOAT_MAX;
      column.width = width;
      [_tableView addTableColumn:column];
      _automaticColumnAdjustments.push_back(0);
      _requestedColumnWidths.push_back(width);
      _userResizedColumns.push_back(false);
    }
    _settingColumnWidths = NO;
  }

  if (dataChanged) {
    [_tableView reloadData];
    _hasLoadedData = YES;
    if (newViewProps.scrollTrailingColumnOverflow) {
      [self updateColumnMinimumWidths];
    } else {
      _measuredColumnWidths.clear();
      _settingColumnWidths = YES;
      for (NSTableColumn *column in _tableView.tableColumns) column.minWidth = 1;
      _settingColumnWidths = NO;
    }
  }

  [self updateScrollerVisibility];
  if (newViewProps.followBottom && dataChanged && wasFollowingBottom) {
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
    if (selectionChanged && _scrollView.hasVerticalScroller) {
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

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  static const auto defaultProps = std::make_shared<const LiteLLMAppKitTableProps>();
  _synchronizingSelection = YES;
  _props = defaultProps;
  [_tableView deselectAll:nil];
  while (_tableView.tableColumns.count > 0) {
    [_tableView removeTableColumn:_tableView.tableColumns.lastObject];
  }
  [_tableView reloadData];
  _tableView.usesAlternatingRowBackgroundColors = NO;
  _synchronizingSelection = NO;
  _settingColumnWidths = NO;
  _automaticColumnAdjustments.clear();
  _measuredColumnWidths.clear();
  _requestedColumnWidths.clear();
  _userResizedColumns.clear();
  _hasLoadedData = NO;
  _scrollView.hasHorizontalScroller = NO;
  _scrollView.hasVerticalScroller = NO;
  _scrollView.acceptsHorizontalScroll = NO;
  _scrollView.acceptsVerticalScroll = NO;
  _clipView.acceptsHorizontalScroll = NO;
  _clipView.acceptsVerticalScroll = NO;
  _tableView.acceptsHorizontalScroll = NO;
  _tableView.acceptsVerticalScroll = NO;
}

- (void)updateColumnMinimumWidths
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSUInteger columnCount = _tableView.tableColumns.count;
  if (!viewProps.scrollTrailingColumnOverflow || columnCount == 0 || viewProps.columnLabels.size() != columnCount ||
      _requestedColumnWidths.size() != columnCount) return;

  const CGFloat horizontalPadding = MAX(
      LiteLLMTableMinimumHorizontalPadding,
      static_cast<CGFloat>(viewProps.cellHorizontalPadding));
  const CGFloat firstColumnHorizontalPadding = MAX(
      LiteLLMTableMinimumHorizontalPadding,
      static_cast<CGFloat>(viewProps.firstColumnHorizontalPadding));
  std::vector<CGFloat> minimumWidths(columnCount, 1);
  for (NSUInteger index = 0; index < columnCount; index++) {
    minimumWidths[index] = ceil(_tableView.tableColumns[index].headerCell.cellSize.width);
  }

  CGFloat spanningWidth = 0;
  for (NSUInteger row = 0; row < viewProps.rowKeys.size(); row++) {
    const BOOL spanning = [self isSpanningRow:static_cast<NSInteger>(row)];
    for (NSUInteger column = 0; column < columnCount; column++) {
      if (spanning && column > 0) break;
      const size_t cellIndex = static_cast<size_t>(row) * columnCount + column;
      NSString *value = cellIndex < viewProps.cells.size() ? StringFromStdString(viewProps.cells[cellIndex]) : @"";
      const CGFloat columnPadding = column == 0 ? firstColumnHorizontalPadding : horizontalPadding;
      const CGFloat textWidth = ceil([value sizeWithAttributes:@{NSFontAttributeName: TableCellFont()}].width) +
          columnPadding * 2;
      if (spanning) spanningWidth = MAX(spanningWidth, textWidth);
      else minimumWidths[column] = MAX(minimumWidths[column], textWidth);
    }
  }

  CGFloat minimumContentWidth = 0;
  for (CGFloat width : minimumWidths) minimumContentWidth += width;
  if (spanningWidth > minimumContentWidth) {
    minimumWidths.back() += spanningWidth - minimumContentWidth;
  }
  _measuredColumnWidths = minimumWidths;

  _settingColumnWidths = YES;
  for (NSUInteger index = 0; index < columnCount; index++) {
    _tableView.tableColumns[index].minWidth = MAX(
        1,
        MIN(_requestedColumnWidths[index], minimumWidths[index]));
  }
  _settingColumnWidths = NO;
}

- (void)updateScrollerVisibility
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const CGFloat trailingContentInset = viewProps.scrollTrailingColumnOverflow ? 10 : 0;
  const NSInteger rowCount = _tableView.numberOfRows;
  const CGFloat rowsHeight = rowCount > 0
      ? NSMaxY([_tableView rectOfRow:rowCount - 1])
      : 0;
  const CGFloat headerHeight = _tableView.headerView == nil ? 0 : NSHeight(_tableView.headerView.frame);
  const NSUInteger columnCount = _tableView.tableColumns.count;
  if (columnCount == 0 || _requestedColumnWidths.size() != columnCount ||
      !(NSHeight(_scrollView.contentView.bounds) > 0)) {
    return;
  }

  const auto minimumContentWidth = [&]() {
    CGFloat width = 0;
    for (NSTableColumn *column in _tableView.tableColumns) width += column.minWidth;
    return width;
  };
  const auto requestedContentWidth = [&]() {
    CGFloat width = 0;
    for (CGFloat columnWidth : _requestedColumnWidths) width += columnWidth;
    return width;
  };
  const auto trailingOverflowWidth = [&]() {
    if (!viewProps.scrollTrailingColumnOverflow || _measuredColumnWidths.size() != columnCount ||
        _userResizedColumns.size() != columnCount || _userResizedColumns.back()) {
      return static_cast<CGFloat>(0);
    }
    return MAX(0, _measuredColumnWidths.back() + trailingContentInset - _requestedColumnWidths.back());
  };
  const auto hasUserColumnResize = [&]() {
    return std::any_of(_userResizedColumns.begin(), _userResizedColumns.end(), [](bool resized) {
      return resized;
    });
  };

  for (NSUInteger index = 0; index < 4; index++) {
    const NSRect visibleBounds = _scrollView.contentView.bounds;
    const CGFloat availableColumnWidth = NSWidth(visibleBounds);
    const CGFloat preferredContentWidth = requestedContentWidth() + trailingOverflowWidth();
    const BOOL needsHorizontalScroller = minimumContentWidth() > availableColumnWidth + 0.5 ||
        ((viewProps.preserveColumnWidths || viewProps.scrollTrailingColumnOverflow || hasUserColumnResize()) &&
         preferredContentWidth > availableColumnWidth + 0.5);
    if (_scrollView.hasHorizontalScroller != needsHorizontalScroller) {
      _scrollView.hasHorizontalScroller = needsHorizontalScroller;
      [_scrollView tile];
      continue;
    }
    const CGFloat dataViewportHeight = MAX(0, NSHeight(_scrollView.contentView.bounds) - headerHeight);
    const BOOL needsVerticalScroller = rowsHeight > dataViewportHeight;
    if (_scrollView.hasVerticalScroller != needsVerticalScroller) {
      _scrollView.hasVerticalScroller = needsVerticalScroller;
      [_scrollView tile];
      continue;
    }
    break;
  }

  const NSRect visibleBounds = _scrollView.contentView.bounds;
  std::vector<CGFloat> laidOutColumnWidths = _requestedColumnWidths;
  if (viewProps.scrollTrailingColumnOverflow && _measuredColumnWidths.size() == columnCount &&
      _userResizedColumns.size() == columnCount && !_userResizedColumns.back()) {
    laidOutColumnWidths.back() = MAX(
        laidOutColumnWidths.back(),
        _measuredColumnWidths.back() + trailingContentInset);
  }
  CGFloat contentWidth = 0;
  for (NSUInteger index = 0; index < columnCount; index++) {
    laidOutColumnWidths[index] = MAX(laidOutColumnWidths[index], _tableView.tableColumns[index].minWidth);
    contentWidth += laidOutColumnWidths[index];
  }
  if (!_scrollView.hasHorizontalScroller && !laidOutColumnWidths.empty()) {
    const CGFloat viewportWidth = NSWidth(visibleBounds);
    if (contentWidth < viewportWidth) {
      laidOutColumnWidths.back() += viewportWidth - contentWidth;
    } else if (contentWidth > viewportWidth) {
      CGFloat deficit = contentWidth - viewportWidth;
      for (NSUInteger index = columnCount; index-- > 0 && deficit > 0.5;) {
        const CGFloat minimumWidth = _tableView.tableColumns[index].minWidth;
        const CGFloat reduction = MIN(deficit, MAX(0, laidOutColumnWidths[index] - minimumWidth));
        laidOutColumnWidths[index] -= reduction;
        deficit -= reduction;
      }
    }
  }
  _settingColumnWidths = YES;
  _automaticColumnAdjustments.resize(columnCount);
  CGFloat laidOutContentWidth = 0;
  for (NSUInteger index = 0; index < columnCount; index++) {
    const CGFloat width = laidOutColumnWidths[index];
    _automaticColumnAdjustments[index] = width - _requestedColumnWidths[index];
    laidOutContentWidth += width;
    NSTableColumn *column = _tableView.tableColumns[index];
    if (fabs(column.width - width) > 0.5) column.width = width;
  }
  _settingColumnWidths = NO;
  const CGFloat dataViewportHeight = MAX(0, NSHeight(visibleBounds) - headerHeight);
  const BOOL needsVerticalScroller = _scrollView.hasVerticalScroller;
  const BOOL needsHorizontalScroller = _scrollView.hasHorizontalScroller;
  _scrollView.verticalScrollElasticity = NSScrollElasticityNone;
  _scrollView.horizontalScrollElasticity = NSScrollElasticityNone;
  _scrollView.acceptsVerticalScroll = needsVerticalScroller;
  _scrollView.acceptsHorizontalScroll = needsHorizontalScroller;
  _clipView.acceptsVerticalScroll = needsVerticalScroller;
  _clipView.acceptsHorizontalScroll = needsHorizontalScroller;
  _tableView.acceptsVerticalScroll = needsVerticalScroller;
  _tableView.acceptsHorizontalScroll = needsHorizontalScroller;

  const NSSize documentSize = NSMakeSize(
      MAX(NSWidth(visibleBounds), laidOutContentWidth),
      MAX(dataViewportHeight, rowsHeight));
  if (!NSEqualSizes(_tableView.frame.size, documentSize)) {
    _tableView.frame = NSMakeRect(0, 0, documentSize.width, documentSize.height);
  }
}

- (void)tableColumnDidResize:(NSNotification *)notification
{
  if (_settingColumnWidths || notification.object != _tableView ||
      _requestedColumnWidths.size() != _tableView.tableColumns.count) {
    return;
  }
  const NSUInteger columnCount = _tableView.tableColumns.count;
  NSUInteger resizedColumn = NSNotFound;
  for (NSUInteger index = 0; index < columnCount; index++) {
    const CGFloat automaticAdjustment = index < _automaticColumnAdjustments.size()
        ? _automaticColumnAdjustments[index]
        : 0;
    const CGFloat expectedWidth = _requestedColumnWidths[index] + automaticAdjustment;
    if (std::fabs(_tableView.tableColumns[index].width - expectedWidth) > 0.5) {
      resizedColumn = index;
      break;
    }
  }
  if (resizedColumn == NSNotFound) {
    return;
  }
  const CGFloat width = _tableView.tableColumns[resizedColumn].width;
  _requestedColumnWidths[resizedColumn] = MAX(_tableView.tableColumns[resizedColumn].minWidth, width);
  if (_userResizedColumns.size() == columnCount) {
    _userResizedColumns[resizedColumn] = true;
  }
  [self updateScrollerVisibility];
}

- (NSInteger)numberOfRowsInTableView:(__unused NSTableView *)tableView
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  return static_cast<NSInteger>(viewProps.rowKeys.size());
}

- (BOOL)isSpanningRow:(NSInteger)row
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  if (row < 0 || static_cast<size_t>(row) >= viewProps.rowKeys.size()) {
    return NO;
  }
  const std::string &rowKey = viewProps.rowKeys[static_cast<size_t>(row)];
  return std::find(viewProps.spanningRowKeys.begin(), viewProps.spanningRowKeys.end(), rowKey) !=
      viewProps.spanningRowKeys.end();
}

- (BOOL)tableView:(__unused NSTableView *)tableView isGroupRow:(NSInteger)row
{
  return [self isSpanningRow:row];
}

- (BOOL)tableView:(__unused NSTableView *)tableView shouldSelectRow:(NSInteger)row
{
  return ![self isSpanningRow:row];
}

- (CGFloat)tableView:(NSTableView *)tableView heightOfRow:(NSInteger)row
{
  return [self isSpanningRow:row] ? tableView.rowHeight + 6 : tableView.rowHeight;
}

- (NSView *)tableView:(NSTableView *)tableView
   viewForTableColumn:(NSTableColumn *)tableColumn
                  row:(NSInteger)row
{
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const size_t columnCount = viewProps.columnLabels.size();
  if (row < 0 || static_cast<size_t>(row) >= viewProps.rowKeys.size()) {
    return nil;
  }
  if ([self isSpanningRow:row]) {
    const size_t cellIndex = static_cast<size_t>(row) * columnCount;
    NSString *value = cellIndex < viewProps.cells.size() ? StringFromStdString(viewProps.cells[cellIndex]) : @"";
    NSUserInterfaceItemIdentifier identifier = @"LiteLLMAppKitTableGroupCell";
    LiteLLMTableGroupCellView *cell = (LiteLLMTableGroupCellView *)[tableView makeViewWithIdentifier:identifier owner:self];
    if (cell == nil) {
      cell = [[LiteLLMTableGroupCellView alloc] initWithFrame:NSZeroRect];
      cell.identifier = identifier;
    }
    const CGFloat firstColumnHorizontalPadding = MAX(
        LiteLLMTableMinimumHorizontalPadding,
        static_cast<CGFloat>(viewProps.firstColumnHorizontalPadding));
    for (NSLayoutConstraint *constraint in cell.constraints) {
      if (constraint.firstItem != cell.label || constraint.secondItem != cell) continue;
      if (constraint.firstAttribute == NSLayoutAttributeLeading) constraint.constant = firstColumnHorizontalPadding;
      else if (constraint.firstAttribute == NSLayoutAttributeTrailing) constraint.constant = -firstColumnHorizontalPadding;
    }
    cell.label.font = TableCellFont();
    cell.label.textColor = NSColor.labelColor;
    cell.label.attributedStringValue = TableCellTitle(value, NSColor.labelColor);
    cell.label.toolTip = value;
    cell.label.accessibilityLabel = value;
    cell.toolTip = value;
    cell.accessibilityLabel = value;
    return cell;
  }
  const NSUInteger columnIndex = [tableView.tableColumns indexOfObject:tableColumn];
  if (
      columnIndex == NSNotFound ||
      static_cast<size_t>(columnIndex) >= columnCount) {
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
    label.font = TableCellFont();
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
  const CGFloat horizontalPadding = MAX(
      LiteLLMTableMinimumHorizontalPadding,
      static_cast<CGFloat>(viewProps.cellHorizontalPadding));
  const CGFloat firstColumnHorizontalPadding = MAX(
      LiteLLMTableMinimumHorizontalPadding,
      static_cast<CGFloat>(viewProps.firstColumnHorizontalPadding));
  const CGFloat columnPadding = columnIndex == 0 ? firstColumnHorizontalPadding : horizontalPadding;
  for (NSLayoutConstraint *constraint in cell.constraints) {
    if (constraint.firstItem != cell.textField || constraint.secondItem != cell) continue;
    if (constraint.firstAttribute == NSLayoutAttributeLeading) constraint.constant = columnPadding;
    else if (constraint.firstAttribute == NSLayoutAttributeTrailing) constraint.constant = -columnPadding;
  }
  NSTextField *label = cell.textField;
  label.font = TableCellFont();
  const std::string &rowKey = viewProps.rowKeys[static_cast<size_t>(row)];
  const bool disabled = std::find(viewProps.disabledRowKeys.begin(), viewProps.disabledRowKeys.end(), rowKey) != viewProps.disabledRowKeys.end();
  const std::string cellKey = rowKey + "\x1f" + std::to_string(columnIndex);
  const bool secondary = std::find(viewProps.secondaryCellKeys.begin(), viewProps.secondaryCellKeys.end(), cellKey) != viewProps.secondaryCellKeys.end();
  NSColor *textColor = disabled || secondary ? NSColor.secondaryLabelColor : NSColor.labelColor;
  label.textColor = textColor;
  label.attributedStringValue = TableCellTitle(value, textColor);
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
  if (selectedRow < 0 || static_cast<size_t>(selectedRow) >= viewProps.rowKeys.size() ||
      [self isSpanningRow:selectedRow]) {
    return;
  }
  LiteLLMAppKitTableEventEmitter::OnSelectionChange event{
      viewProps.rowKeys[static_cast<size_t>(selectedRow)], static_cast<int>(selectedRow)};
  std::static_pointer_cast<const LiteLLMAppKitTableEventEmitter>(_eventEmitter)->onSelectionChange(event);
}

- (void)handleRowClick:(__unused id)sender
{
  if (!_eventEmitter) {
    return;
  }
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSInteger row = _tableView.clickedRow;
  if (row < 0 || static_cast<size_t>(row) >= viewProps.rowKeys.size() || [self isSpanningRow:row]) {
    return;
  }
  LiteLLMAppKitTableEventEmitter::OnSelectionChange event{
      viewProps.rowKeys[static_cast<size_t>(row)], static_cast<int>(row)};
  std::static_pointer_cast<const LiteLLMAppKitTableEventEmitter>(_eventEmitter)->onSelectionChange(event);
}

- (void)handleDoubleClick:(__unused id)sender
{
  if (!_eventEmitter) {
    return;
  }
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitTableProps>(_props);
  const NSInteger row = _tableView.clickedRow;
  if (row < 0 || static_cast<size_t>(row) >= viewProps.rowKeys.size() || [self isSpanningRow:row]) {
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
    _textView.font = [NSFont monospacedSystemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightRegular];
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

@interface LiteLLMWeakScriptMessageHandler : NSObject <WKScriptMessageHandler>
@property(nonatomic, weak) id<WKScriptMessageHandler> target;
@end

@implementation LiteLLMWeakScriptMessageHandler

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message
{
  [self.target userContentController:userContentController didReceiveScriptMessage:message];
}

@end

// RCTViewComponentView does not participate in AppKit's responder chain on
// every macOS release.  WKWebView normally accepts the first responder, but
// the wrapper can otherwise leave the embedded content visible yet unable to
// receive the first key event.  Keep the code pane explicitly focusable and
// make a click on its native surface establish the responder before WebKit
// dispatches the event into the page.
@interface LiteLLMCodeEditorWebView : WKWebView
@end

@implementation LiteLLMCodeEditorWebView

- (BOOL)acceptsFirstResponder
{
  return YES;
}

- (void)mouseDown:(NSEvent *)event
{
  [self.window makeFirstResponder:self];
  [super mouseDown:event];
}

@end

WKProcessPool *LiteLLMCodeEditorProcessPool(void)
{
  static WKProcessPool *processPool;
  static dispatch_once_t onceToken;
  dispatch_once(&onceToken, ^{
    processPool = [WKProcessPool new];
  });
  return processPool;
}

@interface LiteLLMAppKitCodeWebViewComponentView ()
    <RCTLiteLLMAppKitCodeWebViewViewProtocol, WKNavigationDelegate, WKScriptMessageHandler>
- (void)loadEditorHTML:(NSString *)html;
- (void)recoverEditorPageWithError:(NSString *)error;
@end

@implementation LiteLLMAppKitCodeWebViewComponentView {
  WKWebView *_webView;
  LiteLLMWeakScriptMessageHandler *_messageHandler;
  WKNavigation *_activeNavigation;
  NSString *_lastEditorText;
  BOOL _editorReady;
  BOOL _pendingSync;
  NSUInteger _editorStateGeneration;
  NSUInteger _htmlStateGeneration;
  NSUInteger _pageRecoveryAttempts;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitCodeWebViewComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps = std::make_shared<const LiteLLMAppKitCodeWebViewProps>();
    _props = defaultProps;

    _messageHandler = [LiteLLMWeakScriptMessageHandler new];
    _messageHandler.target = self;

    WKUserContentController *userContentController = [WKUserContentController new];
    [userContentController addScriptMessageHandler:_messageHandler name:@"litellmCodeEditor"];
    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    configuration.userContentController = userContentController;
    configuration.processPool = LiteLLMCodeEditorProcessPool();
    configuration.preferences.javaScriptCanOpenWindowsAutomatically = NO;

    _webView = [[LiteLLMCodeEditorWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    _webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    _webView.navigationDelegate = self;
    ConfigureImmediateView(_webView);
    self.contentView = _webView;
  }
  return self;
}

- (BOOL)acceptsFirstResponder
{
  return YES;
}

- (BOOL)becomeFirstResponder
{
  return [_webView becomeFirstResponder] || [super becomeFirstResponder];
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &oldViewProps = *std::static_pointer_cast<const LiteLLMAppKitCodeWebViewProps>(_props);
  const auto &newViewProps = *std::static_pointer_cast<const LiteLLMAppKitCodeWebViewProps>(props);

  const BOOL documentChanged = oldViewProps.documentKey != newViewProps.documentKey;
  const BOOL valueChanged = oldViewProps.value != newViewProps.value;
  const BOOL echoedEditorText = valueChanged && !documentChanged && _lastEditorText != nil &&
      [_lastEditorText isEqualToString:StringFromStdString(newViewProps.value)];
  if (documentChanged) {
    _lastEditorText = nil;
  }
  const BOOL editorStateChanged = documentChanged ||
      (valueChanged && !echoedEditorText) ||
      oldViewProps.baseline != newViewProps.baseline ||
      oldViewProps.language != newViewProps.language ||
      oldViewProps.readOnly != newViewProps.readOnly ||
      oldViewProps.showDiff != newViewProps.showDiff;
  if (editorStateChanged) {
    _editorStateGeneration += 1;
    _pendingSync = YES;
  }

  if (oldViewProps.html != newViewProps.html) {
    _htmlStateGeneration = _editorStateGeneration;
    _pageRecoveryAttempts = 0;
    [self loadEditorHTML:StringFromStdString(newViewProps.html)];
  }

  [super updateProps:props oldProps:oldProps];
  [self synchronizeEditorIfReady];
}

- (void)loadEditorHTML:(NSString *)html
{
  _editorReady = NO;
  _pendingSync = YES;
  _activeNavigation = [_webView loadHTMLString:html baseURL:nil];
}

- (void)recoverEditorPageWithError:(NSString *)error
{
  if (_pageRecoveryAttempts >= 1) {
    [self emitEditorError:error];
    return;
  }
  _pageRecoveryAttempts += 1;
  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitCodeWebViewProps>(_props);
  NSString *html = StringFromStdString(viewProps.html);
  if (html.length == 0) {
    [self emitEditorError:error];
    return;
  }
  dispatch_async(dispatch_get_main_queue(), ^{
    [self loadEditorHTML:html];
  });
}

- (void)synchronizeEditorIfReady
{
  if (!_editorReady || !_pendingSync) {
    return;
  }

  const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitCodeWebViewProps>(_props);
  NSDictionary<NSString *, id> *payload = @{
    @"type": @"replace",
    @"documentKey": StringFromStdString(viewProps.documentKey),
    @"value": StringFromStdString(viewProps.value),
    @"baseline": StringFromStdString(viewProps.baseline),
    @"language": StringFromStdString(viewProps.language),
    @"readOnly": @(viewProps.readOnly),
    @"showDiff": @(viewProps.showDiff),
  };
  NSError *serializationError = nil;
  NSData *jsonData = [NSJSONSerialization dataWithJSONObject:payload options:0 error:&serializationError];
  if (jsonData == nil) {
    _pendingSync = NO;
    [self emitEditorError:serializationError.localizedDescription ?: @"editor_payload_serialization_failed"];
    return;
  }
  NSString *json = [[NSString alloc] initWithData:jsonData encoding:NSUTF8StringEncoding];
  NSString *script = [NSString stringWithFormat:
      @"window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(%@);", json];
  _pendingSync = NO;
  __weak LiteLLMAppKitCodeWebViewComponentView *weakSelf = self;
  [_webView evaluateJavaScript:script completionHandler:^(__unused id result, NSError *error) {
    LiteLLMAppKitCodeWebViewComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || error == nil) {
      return;
    }
    [strongSelf emitEditorError:error.localizedDescription ?: @"javascript_evaluation_failed"];
  }];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation
{
  if (webView != _webView || navigation != _activeNavigation) {
    return;
  }
}

- (void)webView:(WKWebView *)webView
    didFailProvisionalNavigation:(WKNavigation *)navigation
                       withError:(NSError *)error
{
  [self handleNavigationFailureForWebView:webView navigation:navigation error:error];
}

- (void)webView:(WKWebView *)webView
    didFailNavigation:(WKNavigation *)navigation
             withError:(NSError *)error
{
  [self handleNavigationFailureForWebView:webView navigation:navigation error:error];
}

- (void)webViewWebContentProcessDidTerminate:(WKWebView *)webView
{
  if (webView != _webView) {
    return;
  }
  [self recoverEditorPageWithError:@"web_content_process_terminated"];
}

- (void)handleNavigationFailureForWebView:(WKWebView *)webView
                               navigation:(WKNavigation *)navigation
                                    error:(NSError *)error
{
  if (webView != _webView || navigation != _activeNavigation) {
    return;
  }
  [self recoverEditorPageWithError:error.localizedDescription ?: @"page_load_failed"];
}

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message
{
  if (userContentController != _webView.configuration.userContentController ||
      ![message.name isEqualToString:@"litellmCodeEditor"]) {
    return;
  }

  NSDictionary<NSString *, id> *payload = nil;
  if ([message.body isKindOfClass:NSString.class]) {
    NSData *json = [(NSString *)message.body dataUsingEncoding:NSUTF8StringEncoding];
    id parsed = json == nil ? nil : [NSJSONSerialization JSONObjectWithData:json options:0 error:nil];
    if ([parsed isKindOfClass:NSDictionary.class]) {
      payload = (NSDictionary<NSString *, id> *)parsed;
    }
  } else if ([message.body isKindOfClass:NSDictionary.class]) {
    payload = (NSDictionary<NSString *, id> *)message.body;
  }
  if (payload == nil) {
    [self emitEditorError:@"invalid_editor_message"];
    return;
  }

  NSString *type = [payload[@"type"] isKindOfClass:NSString.class] ? payload[@"type"] : @"";
  if ([type isEqualToString:@"ready"]) {
    _editorReady = YES;
    _pageRecoveryAttempts = 0;
    const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitCodeWebViewProps>(_props);
    NSString *documentKey = [payload[@"documentKey"] isKindOfClass:NSString.class]
        ? payload[@"documentKey"]
        : @"";
    const BOOL initialDocumentIsCurrent =
        [documentKey isEqualToString:StringFromStdString(viewProps.documentKey)] &&
        _htmlStateGeneration == _editorStateGeneration;
    _pendingSync = !initialDocumentIsCurrent;
    [self synchronizeEditorIfReady];
    return;
  }
  if ([type isEqualToString:@"error"]) {
    NSString *error = [payload[@"message"] isKindOfClass:NSString.class]
        ? payload[@"message"]
        : @"editor_error";
    [self emitEditorError:error];
    return;
  }
  if (![type isEqualToString:@"change"]) {
    [self emitEditorError:@"unknown_editor_message"];
    return;
  }

  NSString *text = [payload[@"text"] isKindOfClass:NSString.class] ? payload[@"text"] : nil;
  if (text == nil) {
    [self emitEditorError:@"invalid_editor_change"];
    return;
  }
  _lastEditorText = [text copy];
  if (!_eventEmitter) {
    return;
  }
  const auto boundedCount = ^int(id value) {
    if (![value isKindOfClass:NSNumber.class]) {
      return 0;
    }
    return static_cast<int>(MIN(MAX(0, [(NSNumber *)value integerValue]), INT32_MAX));
  };
  LiteLLMAppKitCodeWebViewEventEmitter::OnEditorChange event{
      StdStringFromString(text),
      boundedCount(payload[@"added"]),
      boundedCount(payload[@"changed"]),
      boundedCount(payload[@"deleted"])};
  std::static_pointer_cast<const LiteLLMAppKitCodeWebViewEventEmitter>(_eventEmitter)->onEditorChange(event);
}

- (void)emitEditorError:(NSString *)message
{
  if (!_eventEmitter) {
    return;
  }
  LiteLLMAppKitCodeWebViewEventEmitter::OnEditorError event{StdStringFromString(message)};
  std::static_pointer_cast<const LiteLLMAppKitCodeWebViewEventEmitter>(_eventEmitter)->onEditorError(event);
}

- (void)prepareForRecycle
{
  [super prepareForRecycle];
  static const auto defaultProps = std::make_shared<const LiteLLMAppKitCodeWebViewProps>();
  _props = defaultProps;
  _editorReady = NO;
  _pendingSync = NO;
  _editorStateGeneration = 0;
  _htmlStateGeneration = 0;
  _pageRecoveryAttempts = 0;
  _lastEditorText = nil;
  [_webView stopLoading];
  _activeNavigation = [_webView loadHTMLString:@"" baseURL:nil];
}

- (void)invalidate
{
  [_webView stopLoading];
  _webView.navigationDelegate = nil;
  [_webView.configuration.userContentController removeScriptMessageHandlerForName:@"litellmCodeEditor"];
  _messageHandler.target = nil;
  _activeNavigation = nil;
  _lastEditorText = nil;
  [super invalidate];
}

- (NSView *)accessibilityElement
{
  return _webView;
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitCodeWebViewCls(void)
{
  return LiteLLMAppKitCodeWebViewComponentView.class;
}


@interface LiteLLMAppKitPersistentScrollIndicatorComponentView ()
    <RCTLiteLLMAppKitPersistentScrollIndicatorViewProtocol>
@end

@implementation LiteLLMAppKitPersistentScrollIndicatorComponentView {
  __weak NSScrollView *_managedScrollView;
  BOOL _enabled;
}

+ (ComponentDescriptorProvider)componentDescriptorProvider
{
  return concreteComponentDescriptorProvider<LiteLLMAppKitPersistentScrollIndicatorComponentDescriptor>();
}

- (instancetype)initWithFrame:(CGRect)frame
{
  if (self = [super initWithFrame:frame]) {
    static const auto defaultProps =
        std::make_shared<const LiteLLMAppKitPersistentScrollIndicatorProps>();
    _props = defaultProps;
    _enabled = YES;
    self.accessibilityElement = NO;
  }
  return self;
}

- (NSScrollView *)nearestScrollView
{
  for (NSView *view = self.superview; view != nil; view = view.superview) {
    if ([view isKindOfClass:NSScrollView.class]) {
      return (NSScrollView *)view;
    }
  }
  return nil;
}

- (void)restoreManagedScrollView
{
  NSScrollView *scrollView = _managedScrollView;
  if (scrollView == nil) {
    return;
  }
  scrollView.scrollerStyle = NSScrollerStyleOverlay;
  scrollView.autohidesScrollers = YES;
  [scrollView tile];
  _managedScrollView = nil;
}

- (void)applyPersistentScrollIndicator
{
  NSScrollView *scrollView = _enabled ? [self nearestScrollView] : nil;
  if (_managedScrollView != scrollView) {
    [self restoreManagedScrollView];
    _managedScrollView = scrollView;
  }
  if (scrollView == nil) {
    return;
  }
  BOOL needsTile = NO;
  if (!scrollView.hasVerticalScroller) {
    scrollView.hasVerticalScroller = YES;
    needsTile = YES;
  }
  if (scrollView.scrollerStyle != NSScrollerStyleLegacy) {
    scrollView.scrollerStyle = NSScrollerStyleLegacy;
    needsTile = YES;
  }
  if (scrollView.autohidesScrollers) {
    scrollView.autohidesScrollers = NO;
    needsTile = YES;
  }
  if (needsTile) [scrollView tile];
  scrollView.verticalScroller.hidden = NO;
  scrollView.verticalScroller.alphaValue = 1;
}

- (void)updateProps:(const Props::Shared &)props oldProps:(const Props::Shared &)oldProps
{
  const auto &newViewProps =
      *std::static_pointer_cast<const LiteLLMAppKitPersistentScrollIndicatorProps>(props);
  _enabled = newViewProps.enabled;
  [super updateProps:props oldProps:oldProps];
  [self applyPersistentScrollIndicator];
}

- (void)viewDidMoveToSuperview
{
  [super viewDidMoveToSuperview];
  [self applyPersistentScrollIndicator];
  __weak LiteLLMAppKitPersistentScrollIndicatorComponentView *weakSelf = self;
  dispatch_async(dispatch_get_main_queue(), ^{
    [weakSelf applyPersistentScrollIndicator];
  });
}

- (void)viewDidMoveToWindow
{
  [super viewDidMoveToWindow];
  [self applyPersistentScrollIndicator];
}

- (void)layout
{
  [super layout];
  [self applyPersistentScrollIndicator];
}

- (void)prepareForRecycle
{
  [self restoreManagedScrollView];
  [super prepareForRecycle];
  static const auto defaultProps =
      std::make_shared<const LiteLLMAppKitPersistentScrollIndicatorProps>();
  _props = defaultProps;
  _enabled = YES;
}

- (void)invalidate
{
  [self restoreManagedScrollView];
  [super invalidate];
}

@end

Class<RCTComponentViewProtocol> LiteLLMAppKitPersistentScrollIndicatorCls(void)
{
  return LiteLLMAppKitPersistentScrollIndicatorComponentView.class;
}


@interface LiteLLMAppKitSecureTextInputComponentView () <NSTextFieldDelegate, RCTLiteLLMAppKitSecureTextInputViewProtocol>
- (NSTextField *)activeField;
- (void)stageCurrentSecretForRequest:(NSInteger)commitRequest;
- (void)loadPlainTextSecretForGeneration:(NSUInteger)generation;
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
    _field = [[LiteLLMTabSecureTextField alloc] initWithFrame:NSZeroRect];
    _field.identifier = LiteLLMTabStopIdentifier;
    _field.delegate = self;
    _field.target = self;
    _field.action = @selector(submitSecret:);
    _field.bezelStyle = NSTextFieldRoundedBezel;
    _field.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
    ConfigureSingleLineTextField(_field);
    _plainField = [[LiteLLMTabTextField alloc] initWithFrame:NSZeroRect];
    _plainField.identifier = LiteLLMTabStopIdentifier;
    _plainField.delegate = self;
    _plainField.target = self;
    _plainField.action = @selector(submitSecret:);
    _plainField.bezelStyle = NSTextFieldRoundedBezel;
    _plainField.font = [NSFont systemFontOfSize:LiteLLMUIFontSize];
    ConfigureSingleLineTextField(_plainField);
    _host = [[LiteLLMAppKitControlHostView alloc] initWithFrame:NSZeroRect];
    // Match ordinary text fields: preserve the native bezel height and center
    // it inside the shared compact form row.
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
  const BOOL readOnlyPlainText = newViewProps.plainText && newViewProps.disabled;
  activeField.enabled = (readOnlyPlainText || !newViewProps.disabled) && !_stageInFlight;
  activeField.editable = !readOnlyPlainText;
  activeField.selectable = readOnlyPlainText || !newViewProps.disabled;
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
  const BOOL readablePlainText = newViewProps.plainText && newViewProps.autoCommit && (
      ([domain isEqualToString:@"providers_models"] && [field isEqualToString:@"api_key"] && target.length > 0) ||
      ([domain isEqualToString:@"relay_accounts"] && [field isEqualToString:@"api_key"] && target.length > 0) ||
      ([domain isEqualToString:@"codex"] && [field isEqualToString:@"api_key"] && target.length == 0) ||
      ([domain isEqualToString:@"claude"] &&
       ([field isEqualToString:@"deployment_token"] || [field isEqualToString:@"desktop_gateway_api_key"]) &&
       target.length == 0));
  if (identityChanged && readablePlainText) {
    [self loadPlainTextSecretForGeneration:_generation];
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
  const BOOL allowEmptyValue = _autoCommit && [_host.control isEqual:_plainField];
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
    const auto &viewProps = *std::static_pointer_cast<const LiteLLMAppKitSecureTextInputProps>(strongSelf->_props);
    const BOOL readOnlyPlainText = viewProps.plainText && viewProps.disabled;
    [strongSelf activeField].enabled = readOnlyPlainText || !viewProps.disabled;
    [strongSelf activeField].editable = !readOnlyPlainText;
    [strongSelf activeField].selectable = readOnlyPlainText || !viewProps.disabled;
    if (error.length > 0 || revision == nil || present == nil) {
      strongSelf->_secretDirty = preservePlainText;
      [strongSelf emitRevision:strongSelf->_lastRevision present:strongSelf->_lastPresent status:@"error" error:@"stage_failed" commitRequest:strongSelf->_lastCommitRequest];
      return;
    }
    [strongSelf emitRevision:revision.integerValue present:present.boolValue status:@"saved" error:@"" commitRequest:strongSelf->_lastCommitRequest];
  }];
}

- (void)loadPlainTextSecretForGeneration:(NSUInteger)generation
{
  NSString *domain = [_domain copy];
  NSString *field = [_secretField copy];
  NSString *target = [_target copy];
  __weak LiteLLMAppKitSecureTextInputComponentView *weakSelf = self;
  [CoreIPCBridge.shared readPlainTextSecretForDomain:domain
                                               field:field
                                              target:(target.length > 0 ? target : nil)
                                          completion:^(NSString *_Nullable value, NSString *_Nullable error) {
    LiteLLMAppKitSecureTextInputComponentView *strongSelf = weakSelf;
    if (strongSelf == nil || generation != strongSelf->_generation ||
        ![strongSelf->_host.control isEqual:strongSelf->_plainField] || strongSelf->_secretDirty) return;
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

- (BOOL)control:(__unused NSControl *)control
       textView:(__unused NSTextView *)textView
doCommandBySelector:(SEL)commandSelector
{
  return HandleLiteLLMTabCommand([self activeField], commandSelector);
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
