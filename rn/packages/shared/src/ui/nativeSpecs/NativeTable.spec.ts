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

export const PLATFORMS = ["macos"];

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
  // @macos-only
  scrollToRow?: WithDefault<Int32, -1>;
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
  // A row that is switched off is still the entry that switches it back on:
  // it dims to secondary ink and stays selectable.  `disabledRowKeys` means a
  // row with nothing to act on, which both hosts also refuse the selection —
  // putting an off provider there left the pane with no door back to its own
  // 启用 switch.
  secondaryRowKeys?: ReadonlyArray<string>;
  // A row's own second line: a source list that names its entries by title and
  // dates them underneath asks for a two-line row, and an empty entry keeps the
  // single-line rhythm for the rows that have nothing to add.
  rowDetails?: ReadonlyArray<string>;
  // One optional tooltip per cell, parallel to `cells`.  Empty keeps the
  // default: a cell's tooltip is its own text.
  // @macos-only
  cellTooltips?: ReadonlyArray<string>;
  secondaryCellKeys?: ReadonlyArray<string>;
  alertRowKeys?: ReadonlyArray<string>;
  spanningRowKeys?: ReadonlyArray<string>;
  selectableSpanningRowKeys?: ReadonlyArray<string>;
  onSelectionChange?: DirectEventHandler<SelectionChangeEvent>;
  onRowDoublePress?: DirectEventHandler<RowDoublePressEvent>;
}
