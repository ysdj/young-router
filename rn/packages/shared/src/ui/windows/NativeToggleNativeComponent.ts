import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ value: boolean }>;

export interface NativeToggleProps extends ViewProps {
  value?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  /**
   * The name a screen reader announces for this switch.  The control draws as
   * a bare 24pt box with an “x” glyph, so without the label every switch in
   * the settings windows reads as the same untranslated word.
   */
  accessibilityLabel?: string;
  onValueChange?: DirectEventHandler<ChangeEvent>;
}

export default codegenNativeComponent<NativeToggleProps>("LiteLLMWinUISwitch");
