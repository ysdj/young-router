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

export const PLATFORMS = ["macos"];

type ChangeEvent = Readonly<{ index: Int32; value: string }>;

export interface NativePickerProps extends ViewProps {
  labels: ReadonlyArray<string>;
  selectedValue: string;
  // Options that are listed but cannot be chosen.  A relay account whose
  // session has lapsed still names a relay worth finding in the list; hiding
  // the entry made the account the user must re-sign-in disappear from the
  // one place that names it.
  disabledIndices?: ReadonlyArray<Int32>;
  disabled?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  // @macos-only
  overlayLayer?: WithDefault<boolean, false>;
  onChange?: DirectEventHandler<ChangeEvent>;
}
