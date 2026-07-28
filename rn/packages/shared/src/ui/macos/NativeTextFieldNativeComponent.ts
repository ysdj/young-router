import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type TextEvent = Readonly<{ text: string }>;

export interface NativeTextFieldProps extends ViewProps {
  value?: string;
  placeholder?: string;
  multiline?: WithDefault<boolean, false>;
  secureTextEntry?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onChangeText?: DirectEventHandler<TextEvent>;
  onBlur?: DirectEventHandler<Readonly<{}>>;
  onSubmitEditing?: DirectEventHandler<TextEvent>;
}

export default codegenNativeComponent<NativeTextFieldProps>("LiteLLMAppKitTextField");
