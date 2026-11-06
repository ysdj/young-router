/**
 * The identity of one public model name.
 *
 * A public model is the name a client sends, and one name reaches one group
 * however a document happens to capitalise it: `minimax-m3.1-flash` and
 * `MiniMax-M3.1-Flash` are the same model, not two.  The routes table read the
 * spelling as the identity, so it drew one public model as two groups with one
 * route each — two settings surfaces for one name, and a context window that
 * each half claimed the other could not honour.
 *
 * Core states the same rule (`young_router/public_names.py`), so the pane and
 * the document agree about what one group is.
 */
export function publicModelKey(name: string): string {
  return name.trim().toLocaleLowerCase();
}

/**
 * The one spelling a group publishes, out of the cases its routes state.
 *
 * A lower-case spelling wins: it is the one a client is most likely to send,
 * and the one the other rows can always be folded onto.  With no lower-case
 * spelling among them the first one stands, so the group is named by a row the
 * document already carries rather than by one this pane invented.  Core's
 * `config.dump._preferred_public_spelling` is the same rule, and it is what the
 * document's own group table states.
 */
export function preferredPublicSpelling(spellings: readonly string[]): string {
  const names = spellings.map((name) => name.trim()).filter(Boolean);
  return names.find((name) => name === name.toLocaleLowerCase()) ?? names[0] ?? "";
}

/**
 * One routes list, grouped by the public model each row serves.
 *
 * A group is the name's identity, not its spelling: rows that state one name in
 * two cases are one public model with several routes, which is what the Router
 * and the client see — LiteLLM keeps one deployment set per name, and the pane
 * drew one name as two groups of one route each.  Each group is named by the
 * spelling it publishes (`preferredPublicSpelling`) and keeps its routes in the
 * order they arrived in, because that order is the one the list was sorted by.
 */
export function groupRoutesByPublicModel<T extends { publicModel: string }>(
  routes: readonly T[],
): Array<{ name: string; entries: T[] }> {
  const groups = new Map<string, { spellings: string[]; entries: T[] }>();
  for (const entry of routes) {
    const key = publicModelKey(entry.publicModel);
    if (!key) continue;
    const group = groups.get(key);
    if (group) {
      group.entries.push(entry);
      if (!group.spellings.includes(entry.publicModel)) group.spellings.push(entry.publicModel);
      continue;
    }
    groups.set(key, { spellings: [entry.publicModel], entries: [entry] });
  }
  return Array.from(groups.values(), (group) => ({
    name: preferredPublicSpelling(group.spellings),
    entries: group.entries,
  }));
}
