/**
 * Row keys for the provider pane's model list.
 *
 * A login provider lists its models under the credential that serves them, so
 * a model that every credential serves appears once per group.  A row key
 * therefore carries three parts — the row kind, the credential it sits in, and
 * the model it addresses — and the parts are joined by a unit separator
 * rather than a colon: a credential id is itself `account:<ref>` or
 * `key:<slot>`, so a colon-joined key cannot be split back into the parts it
 * was made of.
 *
 * That was not a cosmetic detail.  Splitting on ":" put the credential's own
 * prefix in front of the model id, so every click on a model asked the pane
 * for a model named after its credential.  No such model exists, the repair
 * effect dropped the selection on the next render, and the row flashed while
 * the inspector stayed shut.
 */
export const MODEL_ROW_KEY_SEPARATOR = "\x1f";

/** The heading row of one credential group: a heading, never a model. */
export function modelGroupRowKey(credentialID: string): string {
  return `credential${MODEL_ROW_KEY_SEPARATOR}${credentialID}`;
}

/** One model, in the group whose credential serves it. */
export function modelRowKey(credentialID: string, modelID: string): string {
  return `${modelGroupRowKey(credentialID)}${MODEL_ROW_KEY_SEPARATOR}${modelID}`;
}

/**
 * The model a model-list row addresses.
 *
 * A group header addresses no model, and neither does the empty key a click
 * below the rows reports.  A key that names no group is an ordinary model id
 * and passes through unchanged, which is how the list addresses a model of a
 * provider that has no credential to group it under.
 */
export function modelIDFromRowKey(key: string): string | undefined {
  if (!key) return undefined;
  const parts = key.split(MODEL_ROW_KEY_SEPARATOR);
  if (parts[0] !== "credential") return key;
  return parts.length > 2 ? parts.slice(2).join(MODEL_ROW_KEY_SEPARATOR) : undefined;
}
