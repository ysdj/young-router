// Source of truth for the NativePickerProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitPicker";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUIPicker";

type ChangeEvent = Readonly<{ index: Int32; value: string }>;

export interface NativePickerProps extends ViewProps {
  labels: ReadonlyArray<string>;
  selectedValue: string;
  disabled?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  onChange?: DirectEventHandler<ChangeEvent>;
}
