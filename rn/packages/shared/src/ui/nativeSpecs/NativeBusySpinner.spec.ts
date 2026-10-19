// Source of truth for the NativeBusySpinnerProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitBusySpinner";
export const PLATFORMS = ["macos"];

export interface NativeBusySpinnerProps extends ViewProps {
  /**
   * The name a screen reader announces.  The wheel has no words of its own, so
   * without this every wait in the app reads as the same unlabeled image.
   */
  accessibilityLabel?: string;
  running?: WithDefault<boolean, true>;
}
