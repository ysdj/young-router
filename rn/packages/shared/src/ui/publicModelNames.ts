/**
 * The identity of one public model name.
 *
 * A public model is the name a client sends, and the name *is* the identity:
 * `minimax-m3.1-flash` and `MiniMax-M3.1-Flash` are two models, two deployment
 * sets in LiteLLM, and two groups in the routes table.  The case is settled once,
 * where a model enters the document: an import files the name its catalog showed
 * in lower case, and a rename keeps whatever the user wrote.  So this pane draws
 * the names the document states instead of folding one onto another and showing
 * two settings surfaces for one name.
 *
 * Core states the same rule (`young_router/public_names.py`), so the pane and
 * the document agree about what one group is.
 */
export function publicModelKey(name: string): string {
  return name.trim();
}

/**
 * Whether an import has to ask which spelling it files.
 *
 * A catalog label with upper case in it is a spelling the user may or may not
 * want their clients to type, so the import asks once (`imported_public_name`
 * in Core holds the two answers).  A roster that is already lower case has
 * nothing to decide, and asking would be a question with one answer.
 */
export function importNeedsNameCase(names: readonly string[]): boolean {
  return names.some((name) => {
    const text = name.trim();
    return Boolean(text) && text !== text.toLowerCase();
  });
}

/**
 * One routes list, grouped by the public model each row serves.
 *
 * A group is one name: rows that state one name are one public model with
 * several routes, which is what the Router and the client see -- LiteLLM keeps
 * one deployment set per name.  A row that states another case is another name
 * and keeps its own group, because that is how the Router serves it.  Each group
 * keeps its routes in the order they arrived in, because that order is the one
 * the list was sorted by.
 */
export function groupRoutesByPublicModel<T extends { publicModel: string }>(
  routes: readonly T[],
): Array<{ name: string; entries: T[] }> {
  const groups = new Map<string, { name: string; entries: T[] }>();
  for (const entry of routes) {
    const name = publicModelKey(entry.publicModel);
    if (!name) continue;
    const group = groups.get(name);
    if (group) {
      group.entries.push(entry);
      continue;
    }
    groups.set(name, { name, entries: [entry] });
  }
  return Array.from(groups.values());
}
