#import <Cocoa/Cocoa.h>
#import <fcntl.h>
#import <sys/file.h>
#import <unistd.h>

// Keep this descriptor open for the entire process lifetime.  flock locks are
// released by the kernel when the process exits, including after a crash.
static int gLiteLLMMenuInstanceLock = -1;

static NSString *LiteLLMMenuInstanceLockPath(void) {
  NSFileManager *fileManager = NSFileManager.defaultManager;
  NSURL *directory = [[fileManager URLsForDirectory:NSApplicationSupportDirectory
                                          inDomains:NSUserDomainMask] firstObject];
  if (directory == nil) {
    return nil;
  }
  // Production copies share the same bundle identifier and therefore one
  // lock. A deliberately distinct preview bundle gets an independent lock,
  // so it can demonstrate a new build without taking over the user's app.
  NSString *bundleIdentifier = NSBundle.mainBundle.bundleIdentifier;
  if (bundleIdentifier.length == 0) {
    bundleIdentifier = @"menu.litellm.menu";
  }
  NSURL *applicationDirectory = [directory URLByAppendingPathComponent:bundleIdentifier
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

static BOOL LiteLLMMenuAcquireInstanceLock(void) {
  NSString *path = LiteLLMMenuInstanceLockPath();
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
  gLiteLLMMenuInstanceLock = descriptor;
  return YES;
}

static NSRunningApplication *LiteLLMMenuExistingInstance(void) {
  NSString *bundleIdentifier = NSBundle.mainBundle.bundleIdentifier;
  if (bundleIdentifier.length == 0) {
    return nil;
  }

  pid_t currentPID = NSProcessInfo.processInfo.processIdentifier;
  for (NSRunningApplication *application in
       [NSRunningApplication runningApplicationsWithBundleIdentifier:bundleIdentifier]) {
    if (!application.terminated && application.processIdentifier != currentPID) {
      return application;
    }
  }
  return nil;
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    // Launch Services enforces LSMultipleInstancesProhibited for normal app
    // launches. This keeps a directly executed or copied app bundle from
    // reaching AppKit, React Native, the status item, or the managed Core.
    NSRunningApplication *existing = LiteLLMMenuExistingInstance();
    if (existing != nil) {
      [existing activateWithOptions:NSApplicationActivateAllWindows];
      return 0;
    }
    // NSRunningApplication is enough for an already registered app, but two
    // copied/directly-executed bundles can reach this point concurrently.  A
    // per-user advisory lock closes that small startup race before AppKit or
    // the managed Core can initialize.
    if (!LiteLLMMenuAcquireInstanceLock()) {
      existing = LiteLLMMenuExistingInstance();
      if (existing != nil) {
        [existing activateWithOptions:NSApplicationActivateAllWindows];
      }
      return 0;
    }
    return NSApplicationMain(argc, argv);
  }
}
