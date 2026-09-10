#import <RCTAppDelegate.h>
#import <Cocoa/Cocoa.h>
#import <React-RCTAppDelegate/RCTRootViewFactory.h>

@interface AppDelegate : RCTAppDelegate

@property(nonatomic, assign) BOOL liteLLMTerminationInProgress;
@property(nonatomic, assign) BOOL liteLLMTerminationReady;

- (void)startReactHostWhenNeeded;

@end
