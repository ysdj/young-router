// Source of truth for the NativeSelectableRowProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitSelectableRow";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUISelectableRow";

export interface NativeSelectableRowProps extends ViewProps {
  title: string;
  detail?: string;
  selected?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onPress?: DirectEventHandler<Readonly<{}>>;
}
