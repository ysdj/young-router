// Source of truth for the NativeCodeWebViewProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitCodeWebView";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUICodeWebView";

type EditorChangeEvent = Readonly<{
  text: string;
  added: Int32;
  changed: Int32;
  deleted: Int32;
}>;

type EditorErrorEvent = Readonly<{ message: string }>;

export interface NativeCodeWebViewProps extends ViewProps {
  html: string;
  documentKey: string;
  value: string;
  baseline: string;
  language: string;
  readOnly?: WithDefault<boolean, false>;
  showDiff?: WithDefault<boolean, false>;
  onEditorChange?: DirectEventHandler<EditorChangeEvent>;
  onEditorError?: DirectEventHandler<EditorErrorEvent>;
}
