/**
 * Shared selection state for table and source-list surfaces.
 *
 * The native group editor and the React table use the same list contract:
 * a plain click selects one row, Command/Ctrl-click toggles a row, Shift-click
 * selects a range, and the keyboard can move the active row or extend that
 * range.  Keeping the state transition here means each host only translates
 * its mouse/key event into an intent; it does not grow a second selection
 * implementation.
 */

export type ListSelectionState = {
  /** Selected keys in the order supplied to `reduceListSelection`. */
  selectedKeys: readonly string[];
  /** The start of a Shift range. */
  anchorKey?: string;
  /** The row most recently acted on, whether or not it is selected. */
  activeKey?: string;
};

export type ListSelectionModifiers = {
  shiftKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
};

export type ListSelectionKeyboardEvent = {
  key?: string;
  code?: string;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  nativeEvent?: {
    key?: string;
    code?: string;
    altKey?: boolean;
    ctrlKey?: boolean;
    metaKey?: boolean;
    shiftKey?: boolean;
  };
  currentTarget?: unknown;
  preventDefault: () => void;
  stopPropagation?: () => void;
};

export const LIST_SELECTION_KEYS = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "a", "A"] as const;

export type ListSelectionAction =
  | ({ type: "press"; key: string } & ListSelectionModifiers)
  | ({ type: "move"; direction: "up" | "down" | "first" | "last" } & ListSelectionModifiers)
  | { type: "all" }
  | { type: "clear" };

export type ListSelectionOptions = {
  /** Rows that cannot be selected (headers and disabled rows use this). */
  isSelectable?: (key: string) => boolean;
};

/** Read a browser or native key event using the same names on every list. */
export function listEventKey(event: ListSelectionKeyboardEvent): string {
  const key = event.key ?? event.nativeEvent?.key ?? event.code ?? event.nativeEvent?.code ?? "";
  return key === "KeyA" ? "a" : key === "Space" ? " " : key;
}

/** Read modifier state from either RN's wrapper or its native event payload. */
export function listSelectionModifiers(event: ListSelectionKeyboardEvent): ListSelectionModifiers & { altKey?: boolean } {
  const native = event.nativeEvent;
  return {
    ctrlKey: event.ctrlKey ?? native?.ctrlKey,
    metaKey: event.metaKey ?? native?.metaKey,
    shiftKey: event.shiftKey ?? native?.shiftKey,
    altKey: event.altKey ?? native?.altKey,
  };
}

/** Translate one list key into the shared selection action. */
export function listSelectionActionForKey(event: ListSelectionKeyboardEvent, axis: "vertical" | "horizontal" = "vertical"): ListSelectionAction | undefined {
  const key = listEventKey(event);
  const modifiers = listSelectionModifiers(event);
  if (modifiers.altKey === true) return undefined;
  if ((key === "a" || key === "A") && (modifiers.ctrlKey === true || modifiers.metaKey === true) && modifiers.shiftKey !== true) {
    return { type: "all" };
  }
  const direction = key === "Home" ? "first" : key === "End" ? "last" : axis === "horizontal" && key === "ArrowLeft" ? "up" : axis === "horizontal" && key === "ArrowRight" ? "down" : key === "ArrowUp" ? "up" : key === "ArrowDown" ? "down" : undefined;
  if (direction === undefined) return undefined;
  return {
    type: "move",
    direction,
    ...(modifiers.ctrlKey === undefined ? {} : { ctrlKey: modifiers.ctrlKey }),
    ...(modifiers.metaKey === undefined ? {} : { metaKey: modifiers.metaKey }),
    ...(modifiers.shiftKey === undefined ? {} : { shiftKey: modifiers.shiftKey }),
  };
}

const DEFAULT_SELECTION: ListSelectionState = { selectedKeys: [] };

export function listSelectionEqual(left: ListSelectionState, right: ListSelectionState): boolean {
  return left.activeKey === right.activeKey && left.anchorKey === right.anchorKey
    && left.selectedKeys.length === right.selectedKeys.length
    && left.selectedKeys.every((key, index) => key === right.selectedKeys[index]);
}

/**
 * Keep a selection valid for the rows a list currently offers.
 *
 * This is the non-destructive counterpart to the `clear` action: missing,
 * disabled and header keys drop out, the order stays the list's, and the
 * selected keys that remain are kept.  Callers that normalize on every render
 * or before reporting a native event must use this; `clear` means the user
 * actually asked for no selection, so using it as validation emptied the
 * highlight on the next render.
 */
export function normalizeListSelection(
  order: readonly string[],
  current: ListSelectionState = DEFAULT_SELECTION,
  options?: ListSelectionOptions,
): ListSelectionState {
  return validState(order, current, options);
}

function uniqueKeys(order: readonly string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const key of order) {
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(key);
  }
  return result;
}

function selectableOrder(order: readonly string[], options?: ListSelectionOptions): string[] {
  const unique = uniqueKeys(order);
  return options?.isSelectable === undefined ? unique : unique.filter(options.isSelectable);
}

function orderedSubset(order: readonly string[], keys: Iterable<string>): string[] {
  const wanted = new Set(keys);
  return order.filter((key, index) => wanted.has(key) && order.indexOf(key) === index);
}

function rangeBetween(order: readonly string[], first: string, last: string): string[] {
  const firstIndex = order.indexOf(first);
  const lastIndex = order.indexOf(last);
  if (firstIndex < 0 || lastIndex < 0) return [];
  const low = Math.min(firstIndex, lastIndex);
  const high = Math.max(firstIndex, lastIndex);
  return order.slice(low, high + 1);
}

function isModified(modifiers: ListSelectionModifiers): boolean {
  return modifiers.ctrlKey === true || modifiers.metaKey === true;
}

function activeFrom(state: ListSelectionState, order: readonly string[]): string | undefined {
  if (state.activeKey !== undefined && order.includes(state.activeKey)) return state.activeKey;
  return state.selectedKeys.find((key) => order.includes(key));
}

function validState(order: readonly string[], state: ListSelectionState, options?: ListSelectionOptions): ListSelectionState {
  const selectable = selectableOrder(order, options);
  const allowed = new Set(selectable);
  const selectedKeys = orderedSubset(selectable, state.selectedKeys).filter((key) => allowed.has(key));
  const activeKey = state.activeKey !== undefined && allowed.has(state.activeKey) ? state.activeKey : selectedKeys[0];
  const anchorKey = state.anchorKey !== undefined && allowed.has(state.anchorKey) ? state.anchorKey : activeKey;
  return { selectedKeys, activeKey, anchorKey };
}

/**
 * Apply one list interaction to a selection state.
 *
 * `order` is the current visual order.  The result is always ordered by it,
 * contains no duplicates, and excludes rows rejected by `isSelectable`.
 */
export function reduceListSelection(
  order: readonly string[],
  current: ListSelectionState = DEFAULT_SELECTION,
  action: ListSelectionAction,
  options?: ListSelectionOptions,
): ListSelectionState {
  const selectable = selectableOrder(order, options);
  const selectableSet = new Set(selectable);
  const state = validState(order, current, options);

  if (action.type === "clear") {
    return { selectedKeys: [], activeKey: state.activeKey, anchorKey: state.anchorKey };
  }

  if (action.type === "all") {
    const activeKey = activeFrom(state, selectable) ?? selectable[0];
    return { selectedKeys: selectable, activeKey, anchorKey: state.anchorKey && selectableSet.has(state.anchorKey) ? state.anchorKey : activeKey };
  }

  if (action.type === "press") {
    if (!selectableSet.has(action.key)) return state;
    const modified = isModified(action);
    const activeKey = action.key;
    const anchorKey = state.anchorKey && selectableSet.has(state.anchorKey) ? state.anchorKey : (state.activeKey && selectableSet.has(state.activeKey) ? state.activeKey : action.key);
    if (action.shiftKey === true) {
      const range = rangeBetween(selectable, anchorKey, action.key);
      const selectedKeys = modified
        ? orderedSubset(selectable, [...state.selectedKeys, ...range])
        : range;
      return { selectedKeys, activeKey, anchorKey };
    }
    if (modified) {
      const selected = new Set(state.selectedKeys);
      if (selected.has(action.key)) selected.delete(action.key);
      else selected.add(action.key);
      return { selectedKeys: orderedSubset(selectable, selected), activeKey, anchorKey: action.key };
    }
    return { selectedKeys: [action.key], activeKey, anchorKey: action.key };
  }

  const currentActive = activeFrom(state, selectable);
  if (selectable.length === 0) return { selectedKeys: [], activeKey: undefined, anchorKey: undefined };
  const currentIndex = currentActive === undefined ? -1 : selectable.indexOf(currentActive);
  const index = action.direction === "first"
    ? 0
    : action.direction === "last"
      ? selectable.length - 1
      : currentIndex < 0
        ? 0
        : Math.max(0, Math.min(selectable.length - 1, currentIndex + (action.direction === "down" ? 1 : -1)));
  const activeKey = selectable[index];
  const anchorKey = state.anchorKey && selectableSet.has(state.anchorKey) ? state.anchorKey : (currentActive ?? activeKey);
  if (action.shiftKey === true) {
    const range = rangeBetween(selectable, anchorKey, activeKey);
    const selectedKeys = isModified(action)
      ? orderedSubset(selectable, [...state.selectedKeys, ...range])
      : range;
    return { selectedKeys, activeKey, anchorKey };
  }
  return { selectedKeys: [activeKey], activeKey, anchorKey: activeKey };
}

