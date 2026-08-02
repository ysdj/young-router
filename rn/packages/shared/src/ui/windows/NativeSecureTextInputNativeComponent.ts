import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

/**
 * This leaf intentionally has no text prop or text-change event. The native
 * editor stays inside the Windows host and is sent directly to Core with a
 * one-time secret capability. plainText changes only native rendering.
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
  "LiteLLMWinUISecureTextInput",
);
