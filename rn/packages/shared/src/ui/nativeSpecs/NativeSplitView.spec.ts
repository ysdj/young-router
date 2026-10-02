// Source of truth for the NativeSplitViewProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Float,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitSplitView";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUISplitView";

type PaneWidthChangeEvent = Readonly<{ width: Float }>;

export interface NativeSplitViewProps extends ViewProps {
  paneWidth: Float;
  minPaneWidth: Float;
  maxPaneWidth: Float;
  paneOpen?: WithDefault<boolean, true>;
  disabled?: WithDefault<boolean, false>;
  onPaneWidthChange?: DirectEventHandler<PaneWidthChangeEvent>;
}
