import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export interface NativePersistentScrollIndicatorProps extends ViewProps {
  enabled?: WithDefault<boolean, true>;
}

export default codegenNativeComponent<NativePersistentScrollIndicatorProps>(
  "LiteLLMAppKitPersistentScrollIndicator",
);
