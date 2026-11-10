/**
 * Applying one incremental event to the projection a window already holds.
 *
 * Core publishes a `patch` that describes what an action moved: entity rows for
 * the domains that can name them, and the top-level fields no row can describe.
 * Until this file existed every window answered a patch by reading the whole
 * snapshot again, so the wire got smaller but the window still paid for a full
 * projection, a full transfer, and a whole-tree re-render.
 *
 * The contract this module keeps is narrow on purpose: it either returns the
 * state the patch describes or refuses.  A refused patch is not an error to
 * show — the caller reads the snapshot instead — because a client that guesses
 * at an entity it cannot place draws a row Core does not have.  The four
 * derived summaries below (`providers_models`, `webdav`, `logs`, `language`)
 * are recomputed from `domains`, exactly as `CoreStore` derives them, so a
 * patched window holds the same fields a fresh snapshot would have given it.
 */

import type {
  CoreSnapshot,
  IpcEventOp,
  LogSummary,
  LogTab,
  PatchEvent,
  ProviderModelSummary,
  ProvidersModelsSummary,
  ProviderSummary,
  WebDavStatus,
} from "./types";

/**
 * A snapshot states two moments, and a window needs both.
 *
 * ``revision`` is where Core's write head is; it is the number a window sends
 * back as the revision its edit was made on.  ``projected_revision`` is the
 * revision the ``domains`` beside it describe: while another thread holds a
 * write lane, Core answers with the last committed projection under the
 * current head.  The two differ only in that case, and a client that reads
 * one as the other breaks its own state:
 *
 * - A patch is a change *from* ``projected_revision``, so chaining it on the
 *   head refuses a patch that describes exactly the state the window lacks.
 * - A stale projection arriving with a current head must not replace a newer
 *   projection the window already holds.
 *
 * The IPC cache and the root component both merge through these two helpers,
 * so the rule has one home.
 */

/** The revision a window's ``domains`` are at. */
export function snapshotProjectionRevision(snapshot: CoreSnapshot | undefined): number | undefined {
  if (!snapshot) return undefined;
  const projected = snapshot.projected_revision;
  if (
    typeof projected === "number"
    && Number.isInteger(projected)
    && projected >= 0
    && projected <= snapshot.revision
  ) {
    return projected;
  }
  // A snapshot from a Core that predates the field carries its projection and
  // its head as the same number, so the fallback is exactly that Core's
  // meaning.
  return snapshot.revision;
}

/**
 * The state a window holds after seeing ``next``, given what it already holds.
 *
 * Neither half moves backwards: the domains (and the summaries derived from
 * them) come from whichever snapshot has the newer projection, while the live
 * fields — the service row, staged drafts, disk markers, action summaries —
 * come from whichever has the newer head.  The result is the same state a
 * full read at the newer of the two moments would have answered, so a late
 * answer to an earlier read cannot undo a patch this window just applied.
 *
 * ``held`` is returned unchanged when ``next`` advances neither half; that is
 * the common keep-alive case and it keeps every domain subscriber quiet.
 */
export function mergeSnapshot(held: CoreSnapshot | undefined, next: CoreSnapshot): CoreSnapshot {
  if (!held) return next;
  const heldProjection = snapshotProjectionRevision(held) ?? held.revision;
  const nextProjection = snapshotProjectionRevision(next) ?? next.revision;
  const heldHasNewerProjection = heldProjection > nextProjection;
  const heldHasNewerHead = held.revision > next.revision;
  if (!heldHasNewerProjection && !heldHasNewerHead) return next;
  if (heldHasNewerProjection && heldHasNewerHead) return held;
  const projection = heldHasNewerProjection ? held : next;
  const live = heldHasNewerHead ? held : next;
  return {
    ...projection,
    protocol_version: next.protocol_version,
    revision: live.revision,
    projected_revision: Math.max(heldProjection, nextProjection),
    service: live.service,
    drafts: live.drafts,
    disk: live.disk,
    action_summaries: live.action_summaries,
  };
}

/** The queue depth Core uses when a log tab states an unusable limit. */
const SUBSCRIPTION_QUEUE_LIMIT = 32;
const LOG_TABS: readonly LogTab[] = ["requests", "service", "actions", "route-trace", "recovery", "online-usage"];

type UnknownRecord = Record<string, unknown>;

/**
 * Apply `event` to `snapshot`, or answer `undefined` when it cannot be applied.
 *
 * `undefined` means "read the full state": the patch does not chain onto this
 * revision, names a domain this window does not have, or carries an op this
 * module cannot reduce.  The input snapshot is never modified — a domain the
 * patch did not touch keeps its own object reference, which is what lets a
 * window re-render only the panes whose domain moved.
 */
export function applyPatch(snapshot: CoreSnapshot, event: PatchEvent): CoreSnapshot | undefined {
  // A malformed event must not take a window down: every refusal here means
  // the caller reads the snapshot, which always works.
  if (!event || typeof event !== "object") return undefined;
  if (event.event !== "patch") return undefined;
  if (!isRecord(snapshot) || !isRecord(snapshot.domains)) return undefined;
  // The patch is a change from the state this window holds, not from the head
  // Core has moved to.  A snapshot answered during another thread's write
  // states both numbers; chaining on the head refused the patch that carried
  // the very state the projection lacked.
  if (event.base_revision !== snapshotProjectionRevision(snapshot)) return undefined;
  if (!Number.isInteger(event.revision) || event.revision <= event.base_revision) return undefined;
  // No op is a patch Core sends for a commit that staged a draft and moved no
  // row: the draft is a top-level field, so `drafts` beside an empty op list is
  // the whole event.  Refusing it sent every window to a full projection right
  // after the edit that made the draft -- the read the patch exists to avoid.
  if (!Array.isArray(event.domains) || !Array.isArray(event.ops)) return undefined;

  const domains = snapshot.domains;
  const touched = new Set<string>();
  for (const op of event.ops) {
    if (!isRecord(op)) return undefined;
    if (op.kind !== "upsert" && op.kind !== "remove" && op.kind !== "replace") return undefined;
    if (typeof op.domain !== "string" || !op.domain) return undefined;
    if (!Object.prototype.hasOwnProperty.call(domains, op.domain)) return undefined;
    if (!event.domains.includes(op.domain)) return undefined;
    if (op.kind === "replace") {
      if (!("value" in op) || op.value === undefined) return undefined;
    } else if (typeof op.id !== "string" || !op.id) {
      return undefined;
    }
    touched.add(op.domain);
  }
  // Core lists a domain only when it emits a row for it; a listed domain with
  // no op means this module would leave a changed projection untouched.
  for (const domain of event.domains) {
    if (typeof domain !== "string" || !touched.has(domain)) return undefined;
  }

  const nextDomains: Record<string, unknown> = { ...domains };
  for (const domain of touched) {
    const ops = event.ops.filter((op) => op.domain === domain);
    const replacements = ops.filter((op) => op.kind === "replace");
    if (replacements.length > 0) {
      // `replace` is the whole projection; any other op beside it would be
      // naming a row of a state that no longer exists.
      if (ops.length !== 1) return undefined;
      nextDomains[domain] = cloneValue(replacements[0].value);
      continue;
    }
    const updated = applyEntityOps(domain, domains[domain], ops);
    if (updated === undefined) return undefined;
    nextDomains[domain] = updated;
  }

  // Which moment the live fields in this event belong to.  A patch can chain
  // onto a projection older than the head this window already holds (a read
  // answered mid-write moved the head first).  The event's drafts, disk row,
  // service state and summaries are then older than the ones in hand, so they
  // stay; its domains are still newer than the projection, which is why this
  // patch is being applied at all.
  const eventIsHead = event.revision >= snapshot.revision;
  const next: CoreSnapshot = {
    ...snapshot,
    // A field the patch did not carry is unchanged; one it carried is the
    // authority, even when it is an empty action-summary map.  Each is copied
    // out, so the state a window holds never aliases the event object.
    ...(event.service === undefined || !eventIsHead ? {} : { service: cloneValue(event.service) }),
    ...(event.drafts === undefined || !eventIsHead ? {} : { drafts: cloneValue(event.drafts) }),
    ...(event.disk === undefined || !eventIsHead ? {} : { disk: cloneValue(event.disk) }),
    ...(event.action_summaries === undefined || !eventIsHead ? {} : { action_summaries: cloneValue(event.action_summaries) }),
    revision: Math.max(snapshot.revision, event.revision),
    projected_revision: event.revision,
    domains: nextDomains,
  };

  if (touched.has("providers_models")) next.providers_models = providersModelsSummary(nextDomains.providers_models);
  if (touched.has("webdav")) next.webdav = webdavSummary(nextDomains.webdav);
  if (touched.has("logs")) next.logs = logsSummary(snapshot.logs, nextDomains.logs);
  if (touched.has("language")) next.language = languageSummary(nextDomains.language);
  return next;
}

/**
 * Apply one domain's entity ops, cloning only what those ops move.
 *
 * A flat row list is reduced with removals first: an id that appears in both an
 * upsert and a remove is one entity that left and came back, and deleting the
 * old row before inserting the new one cannot leave two.  The providers domain
 * nests its routes inside provider rows, so it has its own ordering and pairing
 * in `applyProvidersModelsOps`.
 */
function applyEntityOps(domain: string, projection: unknown, ops: IpcEventOp[]): unknown {
  if (domain === "providers_models") return applyProvidersModelsOps(projection, ops);
  if (Array.isArray(projection)) return applyRowOps(projection, ops);
  if (!isRecord(projection)) return undefined;
  const collection = findRowCollection(projection);
  if (!collection) return undefined;
  const rows = applyRowOps(collection.rows, ops);
  if (rows === undefined) return undefined;
  return { ...projection, [collection.key]: rows };
}

function applyRowOps(rows: unknown[], ops: IpcEventOp[]): unknown[] | undefined {
  const next = rows.slice();
  const removals = ops.filter((op) => op.kind === "remove");
  const writes = ops.filter((op) => op.kind === "upsert");
  for (const op of removals) {
    const id = op.id as string;
    const index = next.findIndex((row) => entityKey(row) === id);
    // A remove for a row this window already dropped is not a failure: two
    // events may have carried the same outcome, and the end state is the same.
    if (index >= 0) next.splice(index, 1);
  }
  for (const op of writes) {
    if (!isRecord(op.value)) return undefined;
    const id = op.id as string;
    const value = cloneValue(op.value);
    const index = next.findIndex((row) => entityKey(row) === id);
    if (index >= 0) next[index] = value;
    else next.push(value);
  }
  return next;
}

/**
 * The providers domain projects its routes nested inside each provider row, so
 * its ops name `provider:<id>` and `model:<id>` instead of one flat list.
 *
 * A route id is not unique across providers: two providers commonly serve the
 * same public model, and Core names that row by its model id.  A write is
 * therefore placed on the provider its own row names, and a remove is only
 * applied when the result is unambiguous — one provider still holds that id,
 * or the patch is removing every holder.  Anything else reads the snapshot:
 * deleting the wrong provider's route is worse than one extra projection.
 */
function applyProvidersModelsOps(projection: unknown, ops: IpcEventOp[]): unknown {
  if (!isRecord(projection) || !Array.isArray(projection.providers)) return undefined;
  const providers = cloneValue(projection.providers) as unknown[];
  const removals = ops.filter((op) => op.kind === "remove");
  const writes = ops.filter((op) => op.kind === "upsert");

  // Writes go first: a route moved to another provider is an upsert under the
  // destination, and the remove that follows names only the route id.  Knowing
  // which provider this patch just wrote is what keeps that remove from taking
  // the row that arrived.  The wire order among writes stands, so a provider
  // is created before the routes it names.
  const writtenModels = new Map<string, Set<string>>();
  for (const op of writes) {
    const target = splitEntityId(op.id as string);
    if (!target || !isRecord(op.value)) return undefined;
    if (target.kind === "provider") {
      if (!upsertProvider(providers, target.key, op.value)) return undefined;
      continue;
    }
    if (target.kind === "model") {
      const owner = upsertModel(providers, target.key, op.value);
      if (owner === undefined) return undefined;
      const written = writtenModels.get(target.key) ?? new Set<string>();
      written.add(owner);
      writtenModels.set(target.key, written);
      continue;
    }
    return undefined;
  }

  // How many rows of one route id this patch says are gone.  Core emits one
  // remove per changed (provider, route) pair, so the count is what decides
  // whether the remaining copies are all of them.
  const removalsLeft = new Map<string, number>();
  for (const op of removals) {
    const target = splitEntityId(op.id as string);
    if (!target) return undefined;
    if (target.kind !== "provider" && target.kind !== "model") return undefined;
    if (target.kind === "model") removalsLeft.set(target.key, (removalsLeft.get(target.key) ?? 0) + 1);
  }
  for (const op of removals) {
    if (!removeEntity(providers, op.id as string, writtenModels, removalsLeft)) return undefined;
  }
  return { ...projection, providers, provider_count: providers.length };
}

function upsertProvider(providers: unknown[], key: string, value: UnknownRecord): boolean {
  const index = providers.findIndex((provider) => providerKey(provider) === key);
  if (index >= 0) providers[index] = cloneValue(value);
  else providers.push(cloneValue(value));
  return true;
}

/**
 * Place one route row, answering the provider's key (for the remove pairing).
 *
 * The row names its provider, so that is where it goes.  A label this window
 * cannot resolve is only acceptable while the id itself lives in exactly one
 * place; otherwise the route could be written onto the wrong row.
 */
function upsertModel(providers: unknown[], key: string, value: UnknownRecord): string | undefined {
  const owner = providerForModel(providers, value);
  if (owner) {
    const models = writeModel(Array.isArray(owner.provider.models) ? owner.provider.models : [], key, value);
    if (!models) return undefined;
    providers[owner.index] = { ...owner.provider, models };
    return providerKey(owner.provider);
  }
  const holders = providers.filter((provider) => isRecord(provider) && Array.isArray(provider.models)
    && provider.models.some((model) => entityKey(model) === key));
  if (holders.length !== 1) return undefined;
  const holder = holders[0];
  if (!isRecord(holder) || !Array.isArray(holder.models)) return undefined;
  const models = writeModel(holder.models, key, value);
  if (!models) return undefined;
  providers[providers.indexOf(holder)] = { ...holder, models };
  return providerKey(holder);
}

/**
 * Put one route row in a provider's list.
 *
 * The editor id is the row's own identity and survives a rename; the model id
 * is the public name, which two routes of one provider may share.  Prefer the
 * unique field, and replace by the shared one only when it names exactly one
 * row — a route moved into a provider that already serves that public name is
 * a second row, not that row.  Two matches without an editor-id match is a
 * state this module cannot place, so the caller reads the snapshot instead.
 */
function writeModel(models: unknown[], key: string, value: UnknownRecord): unknown[] | undefined {
  const editor = typeof value.editor_id === "string" && value.editor_id.trim() ? value.editor_id.trim() : undefined;
  let index = editor === undefined ? -1 : models.findIndex((model) => isRecord(model) && model.editor_id === editor);
  if (index < 0) {
    const matches = models.flatMap((model, position) => (entityKey(model) === key ? [position] : []));
    if (matches.length > 1) return undefined;
    if (matches.length === 1) index = matches[0];
  }
  const next = models.slice();
  const row = cloneValue(value);
  if (index >= 0) next[index] = row;
  else next.push(row);
  return next;
}

function removeEntity(providers: unknown[], id: string, writtenModels: Map<string, Set<string>>, removalsLeft: Map<string, number>): boolean {
  const target = splitEntityId(id);
  if (!target) return false;
  if (target.kind === "provider") {
    const index = providers.findIndex((provider) => providerKey(provider) === target.key);
    if (index >= 0) providers.splice(index, 1);
    return true;
  }
  if (target.kind === "model") {
    const written = writtenModels.get(target.key) ?? new Set<string>();
    const holders = providers.filter((provider) => {
      if (!isRecord(provider) || !Array.isArray(provider.models)) return false;
      const key = providerKey(provider);
      if (key !== undefined && written.has(key)) return false;
      return provider.models.some((model) => entityKey(model) === target.key);
    });
    // No holder left is the end state this remove describes, not a failure.
    if (holders.length === 0) return true;
    // Fewer removes than holders names a subset, and an id-only remove cannot
    // say which one: deleting a guess is a wrong row on screen.
    const left = removalsLeft.get(target.key) ?? 0;
    if (holders.length > left) return false;
    for (const holder of holders) {
      if (!isRecord(holder) || !Array.isArray(holder.models)) continue;
      const index = providers.indexOf(holder);
      if (index < 0) continue;
      providers[index] = {
        ...holder,
        models: holder.models.filter((model) => entityKey(model) !== target.key),
      };
    }
    removalsLeft.set(target.key, Math.max(0, left - holders.length));
    return true;
  }
  return false;
}

/** The provider a route row names, by the label its own projection carries. */
function providerForModel(providers: unknown[], model: UnknownRecord): { index: number; provider: UnknownRecord } | undefined {
  const label = typeof model.provider === "string" ? model.provider.trim() : "";
  if (label) {
    const index = providers.findIndex((provider) => providerKey(provider) === label);
    if (index >= 0 && isRecord(providers[index])) return { index, provider: providers[index] };
    return undefined;
  }
  const named = providers.filter(isRecord);
  // An unnamed provider states no label on its routes, so only an unambiguous
  // single-provider projection can place one; anything else is a full read.
  if (named.length !== 1) return undefined;
  return { index: providers.indexOf(named[0]), provider: named[0] };
}

/** `provider:primary` / `model:public-route`, split at the first colon. */
function splitEntityId(id: string): { kind: string; key: string } | undefined {
  const at = id.indexOf(":");
  if (at <= 0 || at === id.length - 1) return undefined;
  return { kind: id.slice(0, at), key: id.slice(at + 1) };
}

function providerKey(provider: unknown): string | undefined {
  return entityKey(provider);
}

/**
 * A projection that is an object rather than a list keeps its rows under one
 * array field.  A patch names rows by id, so the field has to be identifiable
 * from the field itself; two candidate arrays are ambiguous and refused.
 */
function findRowCollection(projection: UnknownRecord): { key: string; rows: unknown[] } | undefined {
  const candidates: Array<{ key: string; rows: unknown[] }> = [];
  for (const [key, value] of Object.entries(projection)) {
    if (!Array.isArray(value) || value.length === 0) continue;
    if (value.every((row) => isRecord(row) && entityKey(row) !== undefined)) candidates.push({ key, rows: value });
  }
  return candidates.length === 1 ? candidates[0] : undefined;
}

/** The first non-empty identity field of one row, as Core indexes entities. */
function entityKey(row: unknown): string | undefined {
  if (!isRecord(row)) return undefined;
  for (const field of ["id", "editor_id", "name"]) {
    const value = row[field];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

/* -------------------------------------------------------------------------- */
/* The four summaries Core derives, kept in step with a patched projection.   */
/* -------------------------------------------------------------------------- */

/**
 * `CoreStore._providers_summary`, reduced to the fields the typed client reads.
 *
 * The summary is not in the patch: it is derived from `domains.providers_models`,
 * so a window that applies a provider or route row recomputes it here instead
 * of carrying a copy on every event.
 */
function providersModelsSummary(value: unknown): ProvidersModelsSummary {
  if (!isRecord(value)) return { providers: [], revision: 0 };
  const raw = Array.isArray(value.providers) ? value.providers : [];
  const providers: ProviderSummary[] = [];
  raw.forEach((item, index) => {
    if (!isRecord(item)) return;
    const providerId = firstString(item.editor_id, item.id, item.name, `provider-${index + 1}`);
    const displayName = firstString(item.display_name, item.name, providerId);
    const models: ProviderModelSummary[] = [];
    const rawModels = Array.isArray(item.models) ? item.models : [];
    rawModels.forEach((model, modelIndex) => {
      if (!isRecord(model)) return;
      const modelId = firstString(model.editor_id, model.id, model.deployment_id, model.model_name, `model-${modelIndex + 1}`);
      const publicModel = firstString(model.public_model, model.model_name, model.name, model.display_name, modelId);
      const modelDisplayName = firstString(model.display_name, model.name, publicModel);
      const upstream = firstString(model.upstream_model, model.litellm_model, "");
      models.push({
        id: modelId,
        display_name: modelDisplayName,
        public_model: publicModel,
        upstream_model: upstream.includes("/") ? upstream.slice(upstream.indexOf("/") + 1) : upstream,
        enabled: model.model_enabled !== false && model.enabled !== false,
        order: integerValue(model.order),
      });
    });
    const count = item.model_count === undefined ? models.length : integerValue(item.model_count);
    const summary: ProviderSummary = {
      id: providerId,
      display_name: displayName,
      enabled: item.enabled !== false,
      model_count: Math.max(0, count),
      api_key: { present: Boolean(item.api_key_configured) || secretPresence(item.api_key ?? item.api_keys) },
      endpoint: firstString(item.endpoint, item.api_base, ""),
      models,
    };
    if (item.auth_kind !== null && item.auth_kind !== undefined) summary.auth_kind = item.auth_kind as ProviderSummary["auth_kind"];
    if (item.auth_status !== null && item.auth_status !== undefined) summary.auth_status = item.auth_status as ProviderSummary["auth_status"];
    if (item.auth_active !== null && item.auth_active !== undefined) summary.auth_active = item.auth_active as boolean;
    providers.push(summary);
  });
  return { providers, revision: Number.isInteger(value.revision) ? value.revision as number : 0 };
}

/** `CoreStore._webdav_summary`. */
function webdavSummary(value: unknown): WebDavStatus {
  const source = isRecord(value) ? value : {};
  const lastProbe = source.last_probe === "ok" || source.last_probe === "failed" ? source.last_probe : "unknown";
  return {
    enabled: Boolean(source.enabled !== undefined ? source.enabled : source.configured !== undefined ? source.configured : false),
    configured: Boolean(source.configured !== undefined ? source.configured : source.url),
    last_probe: lastProbe,
    password: { present: secretPresence(source.password) },
  };
}

/**
 * `CoreStore._logs_summary`.
 *
 * Core starts from its live log rows and overlays the tabs the domain states,
 * so a window starts from the summary it already holds.  A tab the projection
 * does not mention keeps its previous row.
 */
function logsSummary(base: Record<LogTab, LogSummary>, value: unknown): Record<LogTab, LogSummary> {
  if (!isRecord(value)) return base;
  const tabs = isRecord(value.tabs) ? value.tabs : value;
  const next: Record<LogTab, LogSummary> = { ...base };
  const names = new Set<LogTab>([...LOG_TABS, ...(Object.keys(base) as LogTab[])]);
  for (const tab of names) {
    const raw = tabs[tab];
    if (!isRecord(raw)) continue;
    const stated = raw.limit;
    const limit = typeof stated === "number" && Number.isInteger(stated) && stated >= 1 ? stated : SUBSCRIPTION_QUEUE_LIMIT;
    const lineCount = raw.line_count;
    next[tab] = {
      tab,
      available: Boolean(raw.available),
      paused: Boolean(raw.paused),
      line_count: typeof lineCount === "number" && Number.isInteger(lineCount) && lineCount >= 0 ? lineCount : 0,
      filter: String(raw.filter ?? "").slice(0, 256),
      limit,
    };
  }
  return next;
}

/** `CoreStore._language_summary`. */
function languageSummary(value: unknown): CoreSnapshot["language"] {
  if (isRecord(value)) {
    const choice = value.choice !== undefined ? value.choice : value.language;
    if (choice === "system" || choice === "en" || choice === "zh-Hans") return choice;
  }
  return "system";
}

/** `service._secret_presence`: whether a stated value is anything but absent. */
function secretPresence(value: unknown): boolean {
  if (value === undefined || value === null || value === "" || value === false || value === 0) return false;
  if (value === "configured") return true;
  if (Array.isArray(value)) return value.some(secretPresence);
  if (isRecord(value)) return Object.values(value).some(secretPresence);
  return true;
}

/** The first value that states a usable string. */
function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

/** Python's `int(value)` in the places Core applies it to projected numbers. */
function integerValue(value: unknown): number {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) return Number.parseInt(value.trim(), 10);
  return 0;
}

function cloneValue<T>(value: T): T {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  if (isRecord(value)) {
    const copy: UnknownRecord = {};
    for (const [key, item] of Object.entries(value)) copy[key] = cloneValue(item);
    return copy as T;
  }
  return value;
}

function isRecord(value: unknown): value is UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
