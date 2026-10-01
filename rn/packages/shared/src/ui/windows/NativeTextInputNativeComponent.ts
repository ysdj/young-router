import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ text: string }>;

export interface NativeTextInputProps extends ViewProps {
  value?: string;
  placeholder?: string;
  multiline?: WithDefault<boolean, false>;
  secureTextEntry?: WithDefault<boolean, false>;
  search?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  /**
   * The field's name for a screen reader.  A placeholder is not a name: an
   * unlabeled edit box is announced as “edit” and nothing else.
   */
  accessibilityLabel?: string;
  keyboardType?: string;
  onChangeText?: DirectEventHandler<ChangeEvent>;
  onBlur?: DirectEventHandler<Readonly<{}>>;
  onSubmitEditing?: DirectEventHandler<Readonly<{ text: string }>>;
}

export default codegenNativeComponent<NativeTextInputProps>("LiteLLMWinUITextInput");
