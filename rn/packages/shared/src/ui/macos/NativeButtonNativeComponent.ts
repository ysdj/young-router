import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export interface NativeButtonProps extends ViewProps {
  title: string;
  symbol?: string;
  toolTip?: string;
  accessibilityLabel?: string;
  disabled?: WithDefault<boolean, false>;
  primary?: WithDefault<boolean, false>;
  destructive?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  link?: WithDefault<boolean, false>;
  onPress?: DirectEventHandler<Readonly<{}>>;
}

export default codegenNativeComponent<NativeButtonProps>("LiteLLMAppKitButton");
