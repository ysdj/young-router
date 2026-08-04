import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

/**
 * This leaf intentionally has no text prop or text-change event.  The
 * The native field owns the value, exchanges a one-time Core capability, and
 * reports only presence/revision/status back to React. `plainText` alters
 * native rendering only for the provider API-key editor.
 */
type SecretStateEvent = Readonly<{
  revision: Int32;
  present: boolean;
  status: string;
  error: string;
  commitRequest: Int32;
}>;

export interface NativeSecureTextInputProps extends ViewProps {
  domain: string;
  field: string;
  target: string;
  label: string;
  placeholder?: string;
  plainText?: WithDefault<boolean, false>;
  autoCommit?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  commitRequest?: WithDefault<Int32, 0>;
  resetRequest?: WithDefault<Int32, 0>;
  onSecretState?: DirectEventHandler<SecretStateEvent>;
}

export default codegenNativeComponent<NativeSecureTextInputProps>(
  "LiteLLMAppKitSecureTextInput",
);
