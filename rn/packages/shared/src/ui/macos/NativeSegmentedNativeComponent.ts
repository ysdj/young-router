import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, Int32, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

type ChangeEvent = Readonly<{ index: Int32; value: string }>;

export interface NativeSegmentedProps extends ViewProps {
  labels: ReadonlyArray<string>;
  selectedValue?: string;
  disabled?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  onChange?: DirectEventHandler<ChangeEvent>;
}

export default codegenNativeComponent<NativeSegmentedProps>("LiteLLMAppKitSegmentedControl");
