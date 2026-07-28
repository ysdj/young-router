import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export interface NativeSelectableRowProps extends ViewProps {
  title: string;
  detail?: string;
  selected?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onPress?: DirectEventHandler<Readonly<{}>>;
}

export default codegenNativeComponent<NativeSelectableRowProps>("LiteLLMAppKitSelectableRow");
