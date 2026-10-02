// Source of truth for the NativeButtonProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type { DirectEventHandler, WithDefault } from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitButton";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUIButton";

export interface NativeButtonProps extends ViewProps {
  title: string;
  symbol?: string;
  symbolWithTitle?: WithDefault<boolean, false>;
  /**
   * Draw the symbol after the title instead of before it, so a menu button
   * reads "Title ▾" while an ordinary button keeps its leading icon.
   */
  symbolTrailing?: WithDefault<boolean, false>;
  /**
   * The control's hover hint and its accessibility name.  An icon-only button
   * has no visible words, and a busy wheel's wording rides this hint, so an
   * empty value falls back to the button's own title the way the macOS
   * control already does.
   */
  toolTip?: string;
  accessibilityLabel?: string;
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
