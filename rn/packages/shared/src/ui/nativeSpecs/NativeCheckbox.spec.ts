// Source of truth for the NativeCheckboxProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitCheckbox";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUICheckbox";

type ChangeEvent = Readonly<{ value: boolean }>;

export interface NativeCheckboxProps extends ViewProps {
  label: string;
  value?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  labelVisible?: WithDefault<boolean, true>;
  onValueChange?: DirectEventHandler<ChangeEvent>;
}
