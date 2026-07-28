import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Float,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

type PaneWidthChangeEvent = Readonly<{ width: Float }>;

export interface NativeSplitViewProps extends ViewProps {
  paneWidth: Float;
  minPaneWidth: Float;
  maxPaneWidth: Float;
  paneOpen?: WithDefault<boolean, true>;
  disabled?: WithDefault<boolean, false>;
  onPaneWidthChange?: DirectEventHandler<PaneWidthChangeEvent>;
}

export default codegenNativeComponent<NativeSplitViewProps>("LiteLLMWinUISplitView");
