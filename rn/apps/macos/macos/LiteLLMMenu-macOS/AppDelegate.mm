#import "AppDelegate.h"

#import <React/RCTBundleURLProvider.h>
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
#import <ReactAppDependencyProvider/RCTAppDependencyProvider.h>
#endif
#import "LiteLLMMenu-Swift.h"

@interface RCTAppDelegate (LiteLLMMenuReactHostLoading)
- (void)loadReactNativeWindow:(NSDictionary *)launchOptions;
@end

@interface AppDelegate ()
@property(nonatomic, assign) BOOL reactHostStarted;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
  self.moduleName = @"LiteLLMMenu";
  self.initialProps = @{ @"isPrimaryHost": @YES, @"isWindowManagerHost": @YES };
  // Publish the native status item immediately. React and Core continue
  // loading asynchronously behind an already interactive menu-bar shell.
  AppKitNativeLeaf *nativeLeaf = AppKitNativeLeaf.shared;
  [CoreIPCBridge.shared warm];
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
  self.dependencyProvider = [RCTAppDependencyProvider new];
#endif
  // Keep the native fallback menu interactive until a native action needs
  // React. Hermes starts only on that first action.
  self.automaticallyLoadReactNativeWindow = NO;
  [super applicationDidFinishLaunching:notification];
  [nativeLeaf hideHostWindowAtLaunch:nil];
  [nativeLeaf setReactHostStarter:^{
    [self startReactHostWhenNeeded];
  }];
}

- (void)startReactHostWhenNeeded
{
  if (self.reactHostStarted) {
    return;
  }
  self.reactHostStarted = YES;
  [self loadReactNativeWindow:nil];
  RCTRootViewFactory *rootViewFactory = self.rootViewFactory;
  [AppKitNativeLeaf.shared setRouteWindowFactory:^NSWindow *(NSString *route, NSString *logTab, NSWindow *existingWindow) {
    NSMutableDictionary *props = [@{
      @"isPrimaryHost": @NO,
      @"initialRoute": route,
    } mutableCopy];
    if (logTab != nil) {
      props[@"initialLogTab"] = logTab;
    }
    NSView *rootView = (NSView *)[rootViewFactory viewWithModuleName:@"LiteLLMMenu" initialProperties:props];
    rootView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    NSViewController *controller = [NSViewController new];
    controller.view = rootView;
    if (existingWindow != nil) {
      existingWindow.contentViewController = controller;
      return existingWindow;
    }
    NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1052, 600)
                                                   styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                                                     backing:NSBackingStoreBuffered
                                                       defer:NO];
    window.contentViewController = controller;
    [window center];
    return window;
  }];
  [AppKitNativeLeaf.shared hideHostWindowAtLaunch:self.window];
  [AppKitNativeLeaf.shared setShortcuts:@{@"openMenu": @"Cmd+,", @"closeWindow": @"Esc", @"reload": @"Cmd+R"}];
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender
{
  if (self.liteLLMTerminationReady) {
    return NSTerminateNow;
  }
  if (self.liteLLMTerminationInProgress) {
    return NSTerminateLater;
  }
  self.liteLLMTerminationInProgress = YES;
  [AppKitNativeLeaf.shared prepareForTermination];
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
    [CoreIPCBridge.shared stop];
    dispatch_async(dispatch_get_main_queue(), ^{
      self.liteLLMTerminationReady = YES;
      [sender replyToApplicationShouldTerminate:YES];
    });
  });
  return NSTerminateLater;
}

- (void)application:(NSApplication *)application openURLs:(NSArray<NSURL *> *)urls
{
  NSString *routeScheme = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"LiteLLMMenuRouteScheme"];
  if (![routeScheme isKindOfClass:[NSString class]] || routeScheme.length == 0) {
    routeScheme = @"litellm-menu";
  }
  for (NSURL *url in urls) {
    if (![[url scheme] isEqualToString:routeScheme] || ![[url host] isEqualToString:@"open"]) {
      continue;
    }
    NSString *route = [[url path] stringByTrimmingCharactersInSet:[NSCharacterSet characterSetWithCharactersInString:@"/"]];
    NSSet<NSString *> *routes = [NSSet setWithArray:@[@"home", @"providers-models", @"codex-settings", @"claude-settings", @"runtime-settings", @"relay-accounts", @"webdav-settings", @"logs"]];
    if (![routes containsObject:route]) {
      continue;
    }
    NSString *logTab = nil;
    if ([route isEqualToString:@"logs"]) {
      NSSet<NSString *> *tabs = [NSSet setWithArray:@[@"requests", @"service", @"menu", @"route-trace", @"recovery", @"online-usage"]];
      NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
      NSArray<NSURLQueryItem *> *items = components.queryItems;
      NSURLQueryItem *item = items.count == 1 ? items.firstObject : nil;
      if ([item.name isEqualToString:@"tab"] && [tabs containsObject:item.value]) {
        logTab = item.value;
      }
    }
    [AppKitNativeLeaf.shared openRouteFromDeepLink:route logTab:logTab];
  }
}

- (NSURL *)sourceURLForBridge:(RCTBridge *)bridge
{
  return [self bundleURL];
}

- (NSURL *)bundleURL
{
#if DEBUG
  return [[RCTBundleURLProvider sharedSettings] jsBundleURLForBundleRoot:@"index"];
#else
  return [[NSBundle mainBundle] URLForResource:@"main" withExtension:@"jsbundle"];
#endif
}

@end
