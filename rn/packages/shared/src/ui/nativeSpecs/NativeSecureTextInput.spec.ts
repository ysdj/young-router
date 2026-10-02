// Source of truth for the NativeSecureTextInputProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitSecureTextInput";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUISecureTextInput";

/**
 * This leaf intentionally has no text prop or text-change event: the native
 * field owns the value, exchanges a one-time Core capability, and reports only
 * presence/revision/status back to React.  The Windows host keeps its editor
 * inside the native control and sends the value directly to Core with that
 * capability. `plainText` is used only by native fields explicitly approved
 * for visible editing.
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
  multiline?: WithDefault<boolean, false>;
  plainText?: WithDefault<boolean, false>;
  autoCommit?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  commitRequest?: WithDefault<Int32, 0>;
  resetRequest?: WithDefault<Int32, 0>;
  onSecretState?: DirectEventHandler<SecretStateEvent>;
}
