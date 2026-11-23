import { useLayoutEffect, useRef } from "react";

function adjacentSelectionAfterRemoval(previousKeys: readonly string[], keys: readonly string[], removed: string): string | undefined {
  const index = previousKeys.indexOf(removed);
  if (index < 0 || keys.length === 0) return undefined;
  const remaining = new Set(keys);
  for (let next = index + 1; next < previousKeys.length; next += 1) {
    if (remaining.has(previousKeys[next])) return previousKeys[next];
  }
  for (let next = index - 1; next >= 0; next -= 1) {
    if (remaining.has(previousKeys[next])) return previousKeys[next];
  }
  return keys[0];
}

/** Render one valid selection for both the table and its inspector. */
export function useRowSelection(
  keys: readonly string[],
  requested: string | undefined,
  setRequested: (key: string | undefined) => void,
  { first, target, list, preserve }: { first?: string; target?: string; list?: string; preserve?: boolean },
): string | undefined {
  const previous = useRef({ list, keys });
  let selected = first !== undefined && keys.includes(first) ? first : undefined;
  if (target !== undefined && keys.includes(target)) {
    selected = target;
  } else if (requested !== undefined) {
    // A nested list change (for example, choosing a different provider) gives
    // the same row ID a different owner. Never carry a colliding ID into the
    // new list; an explicit target such as a model move is handled above.
    if (previous.current.list === list) {
      if (keys.includes(requested)) selected = requested;
      else if (preserve) selected = requested;
      else selected = adjacentSelectionAfterRemoval(previous.current.keys, keys, requested) ?? selected;
    }
  }
  useLayoutEffect(() => {
    previous.current = { list, keys };
    if (selected !== requested) setRequested(selected);
  }, [keys, list, requested, selected, setRequested]);
  return selected;
}
