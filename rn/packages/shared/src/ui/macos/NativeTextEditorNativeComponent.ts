import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

type ChangeTextEvent = Readonly<{ text: string }>;

export interface NativeTextEditorProps extends ViewProps {
  value: string;
  documentKey?: string;
  readOnly?: WithDefault<boolean, false>;
  wrap?: WithDefault<boolean, true>;
  onChangeText?: DirectEventHandler<ChangeTextEvent>;
}

export default codegenNativeComponent<NativeTextEditorProps>("LiteLLMAppKitTextEditor");
