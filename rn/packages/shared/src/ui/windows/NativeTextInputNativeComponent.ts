import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ text: string }>;

export interface NativeTextInputProps extends ViewProps {
  value?: string;
  placeholder?: string;
  multiline?: WithDefault<boolean, false>;
  secureTextEntry?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  keyboardType?: string;
  onChangeText?: DirectEventHandler<ChangeEvent>;
  onBlur?: DirectEventHandler<Readonly<{}>>;
  onSubmitEditing?: DirectEventHandler<Readonly<{ text: string }>>;
}

export default codegenNativeComponent<NativeTextInputProps>("LiteLLMWinUITextInput");
