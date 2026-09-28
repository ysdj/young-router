import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Float,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

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
  disabledRowKeys?: ReadonlyArray<string>;
  secondaryCellKeys?: ReadonlyArray<string>;
  alertRowKeys?: ReadonlyArray<string>;
  spanningRowKeys?: ReadonlyArray<string>;
  selectableSpanningRowKeys?: ReadonlyArray<string>;
  onSelectionChange?: DirectEventHandler<SelectionChangeEvent>;
  onRowDoublePress?: DirectEventHandler<RowDoublePressEvent>;
}

export default codegenNativeComponent<NativeTableProps>("LiteLLMWinUITable");
