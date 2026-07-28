import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ value: boolean }>;

export interface NativeToggleProps extends ViewProps {
  value?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  onValueChange?: DirectEventHandler<ChangeEvent>;
}

export default codegenNativeComponent<NativeToggleProps>("LiteLLMWinUISwitch");
