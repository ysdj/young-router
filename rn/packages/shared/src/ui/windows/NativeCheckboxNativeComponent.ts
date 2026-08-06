import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ value: boolean }>;

export interface NativeCheckboxProps extends ViewProps {
  label: string;
  value?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  labelVisible?: WithDefault<boolean, true>;
  onValueChange?: DirectEventHandler<ChangeEvent>;
}

export default codegenNativeComponent<NativeCheckboxProps>("LiteLLMWinUICheckbox");
