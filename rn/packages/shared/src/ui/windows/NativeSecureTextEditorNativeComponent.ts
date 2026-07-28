import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
} from "react-native/Libraries/Types/CodegenTypes";

type EditorStateEvent = Readonly<{
  revision: Int32;
  status: string;
  error: string;
}>;

export interface NativeSecureTextEditorProps extends ViewProps {
  editorToken: string;
  language: string;
  onEditorState?: DirectEventHandler<EditorStateEvent>;
}

export default codegenNativeComponent<NativeSecureTextEditorProps>(
  "LiteLLMWinUISecureTextEditor",
);
