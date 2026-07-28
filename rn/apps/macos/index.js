// AppRegistry creates its performance logger as soon as the application
// registers.  On the bridgeless macOS host that happens before the renderer
// lazily imports InitializeCore, so install React Native's normal globals
// (including the Performance fallback) first.
require("react-native/Libraries/ReactPrivate/ReactNativePrivateInitializeCore");
require("../../packages/shared/src/platformEntry");
