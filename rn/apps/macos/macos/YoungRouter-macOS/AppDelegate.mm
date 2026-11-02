#import "AppDelegate.h"

#import <React/RCTBundleURLProvider.h>
#import <React/RCTReloadCommand.h>
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
#import <ReactAppDependencyProvider/RCTAppDependencyProvider.h>
#endif
#import "YoungRouter-Swift.h"

@interface RCTAppDelegate (YoungRouterReactHostLoading)
- (void)loadReactNativeWindow:(NSDictionary *)launchOptions;
@end

@interface AppDelegate ()
@property(nonatomic, assign) BOOL reactHostStarted;
@end

@implementation AppDelegate

- (void)installNativeStatusItem
{
  [AppKitNativeLeaf.shared installStatusItem];
}

- (void)applicationWillFinishLaunching:(NSNotification *)notification
{
  NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
  [defaults setBool:NO forKey:@"NSAutomaticWindowAnimationsEnabled"];
  [defaults setBool:NO forKey:@"NSScrollAnimationEnabled"];
  self.moduleName = @"YoungRouter";
  self.initialProps = @{ @"isPrimaryHost": @YES, @"isWindowManagerHost": @YES };
  self.automaticallyLoadReactNativeWindow = NO;
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
  self.dependencyProvider = [RCTAppDependencyProvider new];
#endif
  [self clearMetroOverrideWhenBundleFails:defaults];
  [self installNativeStatusItem];
}

/// A Metro-served bundle that fails to compile reaches `RCTFatal`, which a
/// Release host answers by aborting -- with no red box to explain it.  The
/// override outlives that abort, so the next launch would fetch the same
/// broken bundle and abort again, leaving no window to clear it from.  Dropping
/// the override here, before the abort, returns the following launch to the
/// packaged bundle.  Installed only while the override is in use, so a Release
/// host that never opts into Metro keeps its stock failure path.
- (void)clearMetroOverrideWhenBundleFails:(NSUserDefaults *)defaults
{
  if (defaults && [defaults stringForKey:@"MetroServerURL"] == nil) {
    return;
  }
  [[NSNotificationCenter defaultCenter] addObserverForName:RCTJavaScriptDidFailToLoadNotification
                                                    object:nil
                                                     queue:nil
                                                usingBlock:^(NSNotification *note) {
    NSLog(@"Young Router: Metro bundle failed to load (%@); clearing MetroServerURL",
          note.userInfo[@"error"]);
    [defaults removeObjectForKey:@"MetroServerURL"];
    [defaults synchronize];
  }];
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
  [CoreIPCBridge.shared warm];
  [AppKitNativeLeaf.shared setReactHostStarter:^{
    [self startReactHostWhenNeeded];
  }];
  [super applicationDidFinishLaunching:notification];
  [AppKitNativeLeaf.shared hideHostWindowAtLaunch:self.window];
  [self startReactHostWhenNeeded];
}

- (void)startReactHostWhenNeeded
{
  if (self.reactHostStarted) {
    return;
  }
  self.reactHostStarted = YES;
  [self loadReactNativeWindow:nil];
  RCTRootViewFactory *rootViewFactory = self.rootViewFactory;
  [AppKitNativeLeaf.shared setRouteWindowFactory:^NSWindow *(NSString *route, NSString *logTab, NSString *fileId, NSWindow *existingWindow) {
    NSMutableDictionary *props = [@{
      @"isPrimaryHost": @NO,
      @"initialRoute": route,
    } mutableCopy];
    if (fileId != nil) {
      props[@"initialFileTarget"] = fileId;
    }
    if (logTab != nil) {
      props[@"initialLogTab"] = logTab;
    }
    // The settings window is a native full-height source-list window: one
    // sidebar material behind the whole window, a transparent title bar, and
    // the shared React surface painting the opaque detail column on top.
    // Mirror isSettingsShellRoute() in rn/packages/shared/src/routes.ts: every
    // route except the home surface and the provider wizard renders the shared
    // shell, so an explicit whitelist here silently gives a missed pane
    // (General) a plain titled window while the shell still reserves its
    // title-bar inset.
    NSSet<NSString *> *standaloneRoutes = [NSSet setWithArray:@[@"home", @"provider-wizard", @"file-editor"]];
    const BOOL settingsShell = ![standaloneRoutes containsObject:route];
    NSView *rootView = (NSView *)[rootViewFactory viewWithModuleName:@"YoungRouter" initialProperties:props];
    // RCTSurfaceHostingView defaults to an opaque white background on macOS.
    // The route windows paint every surface themselves, so clearing it lets a
    // window-level sidebar material show through the shared layout.
    [(id)rootView setBackgroundColor:NSColor.clearColor];
    rootView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    NSView *contentView = rootView;
    if (settingsShell) {
      NSVisualEffectView *backdrop = [[NSVisualEffectView alloc] initWithFrame:NSMakeRect(0, 0, 1052, 600)];
      backdrop.material = NSVisualEffectMaterialSidebar;
      backdrop.blendingMode = NSVisualEffectBlendingModeBehindWindow;
      backdrop.state = NSVisualEffectStateFollowsWindowActiveState;
      backdrop.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
      rootView.frame = backdrop.bounds;
      [backdrop addSubview:rootView];
      contentView = backdrop;
    }
    NSViewController *controller = [NSViewController new];
    controller.view = contentView;
    if (existingWindow != nil) {
      existingWindow.contentViewController = controller;
      return existingWindow;
    }
    NSWindowStyleMask styleMask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
        NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable;
    if (settingsShell) {
      styleMask |= NSWindowStyleMaskFullSizeContentView;
    }
    NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1052, 600)
                                                   styleMask:styleMask
                                                     backing:NSBackingStoreBuffered
                                                       defer:NO];
    if (settingsShell) {
      window.titlebarAppearsTransparent = YES;
      window.titleVisibility = NSWindowTitleHidden;
      if (@available(macOS 11.0, *)) {
        window.titlebarSeparatorStyle = NSTitlebarSeparatorStyleNone;
      }
      window.opaque = NO;
      window.backgroundColor = NSColor.clearColor;
    }
    window.contentViewController = controller;
    window.animationBehavior = NSWindowAnimationBehaviorNone;
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

- (BOOL)applicationShouldHandleReopen:(NSApplication *)application hasVisibleWindows:(BOOL)hasVisibleWindows
{
  if (hasVisibleWindows) {
    return YES;
  }
  [self startReactHostWhenNeeded];
  [AppKitNativeLeaf.shared openRouteFromDeepLink:@"providers-models" logTab:nil];
  return NO;
}

- (void)application:(NSApplication *)application openURLs:(NSArray<NSURL *> *)urls
{
  NSString *routeScheme = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"YoungRouterRouteScheme"];
  if (![routeScheme isKindOfClass:[NSString class]] || routeScheme.length == 0) {
    routeScheme = @"young-router";
  }
  for (NSURL *url in urls) {
    // `young-router://reload` is the dev loop's trigger.  `RCTHost` registers
    // itself as a reload listener in every configuration, so this re-reads the
    // bundle URL and rebuilds the instance without touching Core or the proxy --
    // which is what makes a Metro-served JS edit cost a reload instead of a
    // restart.  It is a reload and nothing else: it carries no route, reads no
    // configuration, and a Release host that never opts into Metro simply
    // reloads the packaged bundle it already runs.
    if ([[url scheme] isEqualToString:routeScheme] && [[url host] isEqualToString:@"reload"]) {
      dispatch_async(dispatch_get_main_queue(), ^{
        RCTTriggerReloadCommandListeners(@"young-router reload link");
      });
      continue;
    }
    if (![[url scheme] isEqualToString:routeScheme] || ![[url host] isEqualToString:@"open"]) {
      continue;
    }
    NSString *route = [[url path] stringByTrimmingCharactersInSet:[NSCharacterSet characterSetWithCharactersInString:@"/"]];
    NSSet<NSString *> *routes = [NSSet setWithArray:@[@"home", @"general-settings", @"providers-models", @"codex-settings", @"claude-settings", @"runtime-settings", @"data-management", @"provider-wizard", @"logs"]];
    if (![routes containsObject:route]) {
      continue;
    }
    NSString *logTab = nil;
    if ([route isEqualToString:@"logs"]) {
      NSSet<NSString *> *tabs = [NSSet setWithArray:@[@"requests", @"service", @"actions", @"route-trace", @"recovery", @"online-usage"]];
      NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
      NSArray<NSURLQueryItem *> *items = components.queryItems;
      NSURLQueryItem *item = items.count == 1 ? items.firstObject : nil;
      if ([item.name isEqualToString:@"tab"] && [tabs containsObject:item.value]) {
        logTab = item.value;
      }
    }
    // A URL can launch the app before applicationDidFinishLaunching. The
    // route factory lives in the React host, so publish the initial route
    // after starting it rather than dropping an early provider or log link.
    dispatch_async(dispatch_get_main_queue(), ^{
      [self startReactHostWhenNeeded];
      [AppKitNativeLeaf.shared openRouteFromDeepLink:route logTab:logTab];
    });
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
  // Release builds can load from Metro when this override is set, which lets a
  // JS edit be picked up by a reload instead of a deploy. Cleared or an invalid
  // URL falls back to the packaged bundle.
  NSString *metroURL = [[NSUserDefaults standardUserDefaults] stringForKey:@"MetroServerURL"];
  if (metroURL && [metroURL hasPrefix:@"http"]) {
    NSURL *url = [NSURL URLWithString:metroURL];
    if (url) {
      return url;
    }
  }
  return [[NSBundle mainBundle] URLForResource:@"main" withExtension:@"jsbundle"];
#endif
}

@end
