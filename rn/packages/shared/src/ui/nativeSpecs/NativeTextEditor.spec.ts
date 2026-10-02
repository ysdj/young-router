// Source of truth for the NativeTextEditorProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitTextEditor";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUITextEditor";

type ChangeTextEvent = Readonly<{ text: string }>;

export interface NativeTextEditorProps extends ViewProps {
  value: string;
  documentKey?: string;
  readOnly?: WithDefault<boolean, false>;
  wrap?: WithDefault<boolean, true>;
  onChangeText?: DirectEventHandler<ChangeTextEvent>;
}
