import { codegenNativeComponent } from "react-native";
import type { ViewProps } from "react-native";
import type {
  DirectEventHandler,
  Float,
  Int32,
  WithDefault,
} from "react-native/Libraries/Types/CodegenTypes";

type SelectionChangeEvent = Readonly<{ key: string; index: Int32 }>;

export interface NativeTableProps extends ViewProps {
  columnLabels: ReadonlyArray<string>;
  columnWidths: ReadonlyArray<Float>;
  rowKeys: ReadonlyArray<string>;
  cells: ReadonlyArray<string>;
  selectedKey: string;
  alternatingRows?: WithDefault<boolean, false>;
  onSelectionChange?: DirectEventHandler<SelectionChangeEvent>;
}

export default codegenNativeComponent<NativeTableProps>("LiteLLMAppKitTable");
