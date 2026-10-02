// Source of truth for the NativeTableProps native component props.
// `rn/scripts/gen-native-specs.mjs` renders the platform spec files from this one;
// edit this file instead of the generated pair.

import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Float,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

export const APPKIT_COMPONENT_NAME = "LiteLLMAppKitTable";
export const WINUI_COMPONENT_NAME = "LiteLLMWinUITable";

type SelectionChangeEvent = Readonly<{ key: string; index: Int32 }>;
type RowDoublePressEvent = Readonly<{ key: string; index: Int32 }>;

export interface NativeTableProps extends ViewProps {
  columnLabels: ReadonlyArray<string>;
  columnWidths: ReadonlyArray<Float>;
  rowKeys: ReadonlyArray<string>;
  cells: ReadonlyArray<string>;
  selectedKey: string;
  alternatingRows?: WithDefault<boolean, false>;
  compact?: WithDefault<boolean, false>;
  followBottom?: WithDefault<boolean, false>;
  borderless?: WithDefault<boolean, false>;
  sourceList?: WithDefault<boolean, false>;
  rowSymbols?: ReadonlyArray<string>;
  rowSymbolColors?: ReadonlyArray<string>;
  rowImageNames?: ReadonlyArray<string>;
  // @macos-only
  cellHorizontalPadding?: WithDefault<Float, 8>;
  // @macos-only
  firstColumnHorizontalPadding?: WithDefault<Float, 8>;
  // @macos-only
  preserveColumnWidths?: WithDefault<boolean, false>;
  // @macos-only
  scrollTrailingColumnOverflow?: WithDefault<boolean, true>;
  disabledRowKeys?: ReadonlyArray<string>;
  secondaryCellKeys?: ReadonlyArray<string>;
  alertRowKeys?: ReadonlyArray<string>;
  spanningRowKeys?: ReadonlyArray<string>;
  selectableSpanningRowKeys?: ReadonlyArray<string>;
  onSelectionChange?: DirectEventHandler<SelectionChangeEvent>;
  onRowDoublePress?: DirectEventHandler<RowDoublePressEvent>;
}
