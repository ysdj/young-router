/**
 * The order the model list draws its key groups in.
 *
 * The groups are headings of the key table, so they follow the key table's
 * order.  They used to be taken from the order the *routes* introduced them —
 * a `Map` filled while walking the model list and then iterated in insertion
 * order — which made a key's place depend on which of its routes happened to
 * come first in the document.  Deleting that one route then moved the key's
 * whole group to the bottom of the pane: the user deleted a model and watched
 * an unrelated key jump, with no scroll and nothing else changed.
 *
 * A key's place is the key table's, and a key the table no longer lists has no
 * place to borrow: it is a route whose slot was deleted in another window, and
 * it goes last so it is still listed, still selectable, and still repairable.
 *
 * `""` is the heading a route that names no key at all sits under (未定义密钥).
 * It is not a key and cannot be one — no slot id is empty — so it is stated
 * first, above the real keys, which is where a route with nothing to answer as
 * belongs: it is the row the pane has to explain before it lists the rows that
 * are finished.
 */
export function orderedModelGroupIDs(
  modelGroupIDs: readonly string[],
  keyOrder: readonly string[],
): string[] {
  const present: string[] = [];
  const seen = new Set<string>();
  for (const id of modelGroupIDs) {
    if (seen.has(id)) continue;
    seen.add(id);
    present.push(id);
  }
  const position = new Map(keyOrder.map((id, index) => [id, index]));
  const ungrouped = present.filter((id) => id === "");
  const known = present
    .filter((id) => id !== "" && position.has(id))
    .sort((left, right) => (position.get(left) ?? 0) - (position.get(right) ?? 0));
  const unknown = present.filter((id) => id !== "" && !position.has(id));
  return [...ungrouped, ...known, ...unknown];
}
