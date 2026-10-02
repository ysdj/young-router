// Source of truth for the NativeTextFieldProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitTextField";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUITextInput";
export const OUTPUT_FILES = { windows: "NativeTextInputNativeComponent.ts" };

type TextEvent = Readonly<{ text: string }>;

export interface NativeTextFieldProps extends ViewProps {
  value?: string;
  placeholder?: string;
  multiline?: WithDefault<boolean, false>;
  secureTextEntry?: WithDefault<boolean, false>;
  /** Native search field chrome (NSSearchField with a magnifier and clear button). */
  search?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onChangeText?: DirectEventHandler<TextEvent>;
  onBlur?: DirectEventHandler<Readonly<{}>>;
  onSubmitEditing?: DirectEventHandler<TextEvent>;
  // @windows-only
  /**
   * The field's name for a screen reader.  A placeholder is not a name: an
   * unlabeled edit box is announced as “edit” and nothing else.
   */
  accessibilityLabel?: string;
  // @windows-only
  keyboardType?: string;
}

