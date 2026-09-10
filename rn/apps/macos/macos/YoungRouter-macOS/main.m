#import <Cocoa/Cocoa.h>
#import <fcntl.h>
#import <sys/file.h>
#import <unistd.h>

// Keep this descriptor open for the entire process lifetime.  flock locks are
// released by the kernel when the process exits, including after a crash.
static int gYoungRouterInstanceLock = -1;

// This namespace is deliberately independent of the bundle identifier. A
// preview/debug copy is still the same menu-bar application and must not be
// able to create a second status item beside the installed copy.
static NSString * const kYoungRouterInstanceNamespace = @"young.router.app";

static NSString *YoungRouterInstanceLockPath(void) {
  NSFileManager *fileManager = NSFileManager.defaultManager;
  NSURL *directory = [[fileManager URLsForDirectory:NSApplicationSupportDirectory
                                          inDomains:NSUserDomainMask] firstObject];
  if (directory == nil) {
    return nil;
  }
  NSURL *applicationDirectory = [directory URLByAppendingPathComponent:kYoungRouterInstanceNamespace
                                                            isDirectory:YES];
  NSError *error = nil;
  if (![fileManager createDirectoryAtURL:applicationDirectory
             withIntermediateDirectories:YES
                              attributes:@{NSFilePosixPermissions : @0700}
                                   error:&error]) {
    return nil;
  }
  return [[applicationDirectory URLByAppendingPathComponent:@"instance.lock"] path];
}

static BOOL YoungRouterAcquireInstanceLock(void) {
  NSString *path = YoungRouterInstanceLockPath();
  if (path.length == 0) {
    return NO;
  }
  int descriptor = open(path.fileSystemRepresentation, O_CREAT | O_RDWR, 0600);
  if (descriptor < 0) {
    // Failing closed is safer than permitting another managed Core/status
    // item.  A normal app launch can be retried after the temporary folder is
    // available again.
    return NO;
  }
  if (flock(descriptor, LOCK_EX | LOCK_NB) != 0) {
    close(descriptor);
    return NO;
  }
  gYoungRouterInstanceLock = descriptor;
  return YES;
}

static BOOL YoungRouterIsManagedApplication(NSRunningApplication *application) {
  NSURL *bundleURL = application.bundleURL;
  if (bundleURL == nil) {
    return NO;
  }

  // Inspect the executable inside the bundle instead of trusting only its
  // bundle identifier. Older preview/debug bundles used distinct identifiers
  // and could otherwise bypass a production-only lookup.
  NSString *executablePath = [[bundleURL URLByAppendingPathComponent:@"Contents/MacOS/YoungRouter"] path];
  if (![[NSFileManager defaultManager] isExecutableFileAtPath:executablePath]) {
    return NO;
  }
  NSDictionary *info = [NSDictionary dictionaryWithContentsOfURL:
      [bundleURL URLByAppendingPathComponent:@"Contents/Info.plist"]];
  NSString *executable = info[@"CFBundleExecutable"];
  NSString *routeScheme = info[@"YoungRouterRouteScheme"];
  NSString *bundleIdentifier = application.bundleIdentifier.lowercaseString;
  BOOL knownIdentifier = [bundleIdentifier isEqualToString:kYoungRouterInstanceNamespace]
      || [bundleIdentifier hasPrefix:[kYoungRouterInstanceNamespace stringByAppendingString:@"."]];
  return [executable isEqualToString:@"YoungRouter"]
      && (knownIdentifier || [routeScheme.lowercaseString hasPrefix:@"young-router"]);
}

static NSRunningApplication *YoungRouterExistingInstance(void) {
  pid_t currentPID = NSProcessInfo.processInfo.processIdentifier;
  for (NSRunningApplication *application in NSWorkspace.sharedWorkspace.runningApplications) {
    if (!application.terminated
        && application.processIdentifier != currentPID
        && YoungRouterIsManagedApplication(application)) {
      return application;
    }
  }
  return nil;
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    // Check every Young Router bundle before AppKit starts. Launch Services'
    // bundle-identifier rule alone is insufficient for older preview/debug
    // copies that used a different identifier.
    NSRunningApplication *existing = YoungRouterExistingInstance();
    if (existing != nil) {
      [existing activateWithOptions:NSApplicationActivateAllWindows];
      return 0;
    }
    // NSRunningApplication is enough for an already registered app, but two
    // copied/directly-executed bundles can reach this point concurrently.  A
    // per-user advisory lock closes that small startup race before AppKit or
    // the managed Core can initialize.
    if (!YoungRouterAcquireInstanceLock()) {
      existing = YoungRouterExistingInstance();
      if (existing != nil) {
        [existing activateWithOptions:NSApplicationActivateAllWindows];
      }
      return 0;
    }
    return NSApplicationMain(argc, argv);
  }
}
