import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export interface NativeButtonProps extends ViewProps {
  title: string;
  symbol?: string;
  symbolWithTitle?: WithDefault<boolean, false>;
  disabled?: WithDefault<boolean, false>;
  /**
   * The button's own action is running. The control keeps its title, width,
   * and enabled appearance and draws a small spinner beside the title, so a
   * working button never turns into a greyed-out placeholder.
   */
  busy?: WithDefault<boolean, false>;
  primary?: WithDefault<boolean, false>;
  destructive?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  link?: WithDefault<boolean, false>;
  plainLink?: WithDefault<boolean, false>;
  onPress?: DirectEventHandler<Readonly<{}>>;
}

export default codegenNativeComponent<NativeButtonProps>("LiteLLMWinUIButton");
