#import "AppDelegate.h"

#import <React/RCTBundleURLProvider.h>
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
#import <ReactAppDependencyProvider/RCTAppDependencyProvider.h>
#endif
#import "LiteLLMMenu-Swift.h"

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
  self.moduleName = @"LiteLLMMenu";
  self.initialProps = @{};
#if __has_include(<ReactAppDependencyProvider/RCTAppDependencyProvider.h>)
  self.dependencyProvider = [RCTAppDependencyProvider new];
#endif
  [super applicationDidFinishLaunching:notification];
  [AppKitNativeLeaf.shared hideHostWindowAtLaunch:self.window];
  [AppKitNativeLeaf.shared setShortcuts:@{@"openMenu": @"Cmd+,", @"closeWindow": @"Esc", @"reload": @"Cmd+R"}];
}

- (void)applicationWillTerminate:(NSNotification *)notification
{
  [CoreIPCBridge.shared stop];
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
    NSSet<NSString *> *routes = [NSSet setWithArray:@[@"home", @"providers-models", @"codex-settings", @"claude-settings", @"runtime-settings", @"configuration-package", @"webdav-settings", @"logs"]];
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
