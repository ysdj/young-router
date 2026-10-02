// Source of truth for the NativePersistentScrollIndicatorProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitPersistentScrollIndicator";
export const PLATFORMS = ["macos"];

export interface NativePersistentScrollIndicatorProps extends ViewProps {
  enabled?: WithDefault<boolean, true>;
}
