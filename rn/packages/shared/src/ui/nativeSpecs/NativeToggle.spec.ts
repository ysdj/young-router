// Source of truth for the NativeToggleProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitSwitch";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUISwitch";

type ChangeEvent = Readonly<{ value: boolean }>;

export interface NativeToggleProps extends ViewProps {
  value?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onValueChange?: DirectEventHandler<ChangeEvent>;
  // @windows-only
  /**
   * The name a screen reader announces for this switch.  The control draws as
   * a bare 24pt box with an “x” glyph, so without the label every switch in
   * the settings windows reads as the same untranslated word.
   */
  accessibilityLabel?: string;
}

