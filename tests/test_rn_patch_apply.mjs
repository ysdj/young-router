#!/usr/bin/env node
/**
 * A window applies a patch instead of reading the whole projection again.
 *
 * Core's incremental event is only worth its framing if the client's copy ends
 * up at the same state, so these assertions are about the copy: the row an
 * `upsert` names is there, the row a `remove` names is gone, a patch that does
 * not chain is refused, and the snapshot a window already held is never
 * mutated.  The last two blocks cover the state layer that lets one pane draw
 * one domain, and the rule that removes the third full read after an action.
 */
import assert from "node:assert/strict";
import { applyPatch, mergeSnapshot, snapshotProjectionRevision } from "../rn/packages/shared/src/applyPatch.ts";
import {
  getDomain,
  postActionRefreshNeeded,
  publishSnapshot,
  subscribeDomain,
} from "../rn/packages/shared/src/ui/snapshotStore.ts";

function model(id, overrides = {}) {
  return {
    id,
    editor_id: `editor-${id}`,
    model_name: id,
    public_model: id,
    display_name: id,
    upstream_model: `upstream-${id}`,
    litellm_model: `openai/upstream-${id}`,
    provider: "primary",
    enabled: true,
    model_enabled: true,
    order: 0,
    ...overrides,
  };
}

const provider = {
  id: "primary",
  editor_id: "provider-primary",
  name: "primary",
  enabled: true,
  auth_kind: "api_key",
  auth_status: "signed_out",
  auth_active: false,
  api_base: "https://example.test/v1",
  api_key_configured: false,
  models: [model("public-route")],
};

function snapshotAt(revision, providers) {
  return {
    protocol_version: 1,
    revision,
    service: { state: "running" },
    providers_models: { providers: [], revision: 0 },
    drafts: {},
    disk: {},
    webdav: { enabled: false, configured: false, last_probe: "unknown", password: { present: false } },
    logs: {
      requests: { tab: "requests", available: true, paused: false, line_count: 1, filter: "", limit: 32 },
      service: { tab: "service", available: true, paused: false, line_count: 0, filter: "", limit: 32 },
      actions: { tab: "actions", available: true, paused: false, line_count: 0, filter: "", limit: 32 },
      "route-trace": { tab: "route-trace", available: true, paused: false, line_count: 0, filter: "", limit: 32 },
      recovery: { tab: "recovery", available: true, paused: false, line_count: 0, filter: "", limit: 32 },
      "online-usage": { tab: "online-usage", available: true, paused: false, line_count: 0, filter: "", limit: 32 },
    },
    language: "system",
    action_summaries: {},
    domains: {
      providers_models: {
        domain: "providers_models",
        revision,
        exists: true,
        providers,
        provider_count: providers.length,
        model_contexts: {},
        model_concurrency_default: 16,
        binding_issues: [],
        service_account_recheck_seconds: 5,
        raw_editor_available: true,
      },
      runtime: { domain: "runtime", revision, settings: [] },
    },
  };
}

function patch(base, ops, extra = {}) {
  return {
    protocol_version: 1,
    event: "patch",
    base_revision: base,
    revision: base + 1,
    domains: [...new Set(ops.map((op) => op.domain))],
    ops,
    ...extra,
  };
}

const base = snapshotAt(7, [provider]);
const original = structuredClone(base);

// An upsert of a route lands in the provider it names, and the summary the
// window draws is recomputed from the projection rather than carried.
const second = model("public-route-2");
const added = applyPatch(base, patch(7, [
  { domain: "providers_models", kind: "upsert", id: "provider:primary", value: { ...provider, models: [provider.models[0]] } },
  { domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: second },
]));
assert.ok(added, "an upsert patch was refused");
assert.equal(added.revision, 8);
assert.deepEqual(
  added.domains.providers_models.providers[0].models.map((row) => row.id),
  ["public-route", "public-route-2"],
  "the added route is not in the provider it names",
);
assert.equal(added.domains.providers_models.provider_count, 1);
assert.deepEqual(
  added.providers_models.providers.map((row) => [row.id, row.model_count]),
  [["provider-primary", 2]],
  "the derived provider summary was not recomputed",
);
assert.equal(added.providers_models.providers[0].models[1].id, "editor-public-route-2");
assert.equal(added.providers_models.providers[0].models[1].upstream_model, "upstream-public-route-2");
// The domain the patch did not touch keeps its object identity, which is what
// keeps its subscribers from being notified.
assert.equal(added.domains.runtime, base.domains.runtime, "an untouched domain was copied");
assert.equal(added.logs, base.logs, "an untouched summary was rebuilt");

// A remove names the row and only that row.
const removed = applyPatch(base, patch(7, [
  { domain: "providers_models", kind: "remove", id: "model:public-route" },
]));
assert.ok(removed, "a remove patch was refused");
assert.deepEqual(removed.domains.providers_models.providers[0].models, []);
assert.equal(removed.domains.providers_models.provider_count, 1);

// A route moved between providers is one upsert under the destination and one
// remove of the same id under the source.  The client writes first and keeps
// the remove away from the provider the write just named, which is what makes
// the pair read as a move instead of deleting the row that moved.
const moved = applyPatch(base, patch(7, [
  { domain: "providers_models", kind: "upsert", id: "provider:secondary", value: { ...provider, id: "secondary", name: "secondary", editor_id: "provider-secondary", models: [] } },
  { domain: "providers_models", kind: "upsert", id: "model:public-route", value: model("public-route", { provider: "secondary" }) },
  { domain: "providers_models", kind: "remove", id: "model:public-route" },
]));
assert.ok(moved, "a move patch was refused");
assert.deepEqual(
  moved.domains.providers_models.providers.map((row) => [row.id, row.models.map((entry) => entry.id)]),
  [["primary", []], ["secondary", ["public-route"]]],
  "the moved route did not land under its new provider",
);

// Two providers commonly serve the same public model, and Core names both rows
// by that model id.  A write must land on the provider the row itself names,
// and an id-only remove that would have to choose between two holdings is
// refused rather than deleting the wrong provider's route.
const mirror = { ...provider, id: "mirror", editor_id: "provider-mirror", name: "mirror", models: [model("public-route", { provider: "mirror" })] };
const shared = snapshotAt(11, [provider, mirror]);
const edited = applyPatch(shared, patch(11, [
  { domain: "providers_models", kind: "upsert", id: "model:public-route", value: model("public-route", { provider: "mirror", upstream_model: "upstream-edited" }) },
]));
assert.ok(edited, "a duplicate-id edit was refused");
assert.equal(
  edited.domains.providers_models.providers[0].models[0].upstream_model,
  "upstream-public-route",
  "the edit landed on the provider the row did not name",
);
assert.equal(edited.domains.providers_models.providers[1].models[0].upstream_model, "upstream-edited");
assert.equal(
  applyPatch(shared, patch(11, [
    { domain: "providers_models", kind: "remove", id: "model:public-route" },
  ])),
  undefined,
  "an id-only remove with two holders deleted a provider's route by guess",
);
const bothRemoved = applyPatch(shared, patch(11, [
  { domain: "providers_models", kind: "remove", id: "model:public-route" },
  { domain: "providers_models", kind: "remove", id: "model:public-route" },
]));
assert.ok(bothRemoved, "two removes naming both holders were refused");
assert.deepEqual(bothRemoved.domains.providers_models.providers.map((row) => row.models.length), [0, 0]);

// A patch whose base is not the revision the window holds is refused: the
// caller's answer to that is one full snapshot, which always works.
assert.equal(applyPatch(base, patch(6, [
  { domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: second },
])), undefined);
assert.equal(applyPatch(base, patch(7, [
  { domain: "codex", kind: "upsert", id: "unknown", value: {} },
])), undefined);
assert.equal(applyPatch(base, patch(7, [
  { domain: "providers_models", kind: "explode", id: "model:public-route", value: {} },
])), undefined);

// Top-level fields travel beside the ops: one the patch carries replaces the
// old value, one it omits keeps the object the window already held.
const carried = applyPatch(base, patch(7, [
  { domain: "runtime", kind: "replace", value: { domain: "runtime", revision: 8, settings: [{ key: "choice", value: "en" }] } },
], { drafts: { runtime: { dirty: true, base_revision: 7 } }, service: { state: "starting" } }));
assert.ok(carried, "a top-level patch was refused");
assert.deepEqual(carried.drafts, { runtime: { dirty: true, base_revision: 7 } });
assert.deepEqual(carried.service, { state: "starting" });
assert.equal(carried.action_summaries, base.action_summaries, "an omitted top-level field was dropped");
assert.equal(carried.providers_models, base.providers_models, "an untouched derived summary was rebuilt");
// A replace op replaces that domain wholesale.
assert.deepEqual(carried.domains.runtime, { domain: "runtime", revision: 8, settings: [{ key: "choice", value: "en" }] });

// The snapshot the window held is never modified: a pane still pointing at the
// previous state must not start drawing rows the patch added.
assert.notEqual(added, base, "applyPatch returned the same object");
assert.notEqual(added.domains, base.domains, "applyPatch reused the domains map");
assert.notEqual(added.domains.providers_models, base.domains.providers_models, "applyPatch mutated the domain");
assert.deepEqual(base, original, "applyPatch mutated the snapshot it was given");

// ---------------------------------------------------------------------------
// A snapshot states the write head and the revision its projection is at.
// While another thread holds a write lane, Core answers the last committed
// projection under the current head.  A patch chains on the projection, so the
// numbers must not be read as one: a window that chained on the head refused
// the patch that carried the very state its projection lacked, and a stale
// projection that arrived after that patch walked the state back.
// ---------------------------------------------------------------------------
const staleView = {
  ...snapshotAt(9, [provider]),
  projected_revision: 7,
  drafts: { providers_models: { dirty: true, base_revision: 9, validation: { valid: true, issues: [] } } },
};
assert.equal(snapshotProjectionRevision(staleView), 7, "the projection stated its own revision");
assert.equal(
  snapshotProjectionRevision({ ...staleView, projected_revision: undefined }),
  9,
  "a Core without the field was read as head == projection",
);

const chained = applyPatch(staleView, patch(7, [
  { domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: model("public-route-2") },
]));
assert.ok(chained, "a patch was refused because the window's head had moved first");
assert.equal(chained.projected_revision, 8, "the applied patch did not move the projection");
assert.equal(chained.revision, 9, "applying the patch moved the window's head backwards");
assert.deepEqual(chained.drafts, staleView.drafts, "an older patch replaced the live drafts of a newer head");
assert.equal(
  chained.domains.providers_models.providers[0].models.length,
  2,
  "the patch did not reach the projection it chained on",
);

// A snapshot whose projection already moved is the newer state even when its
// head is the older number: the two halves are independent, and neither may
// walk back.
const newerProjectionOlderHead = { ...snapshotAt(8, [provider, { ...provider, id: "second", editor_id: "provider-second" }]), projected_revision: 8 };
const merged = mergeSnapshot(staleView, newerProjectionOlderHead);
assert.equal(merged.revision, 9, "the merged state lost the newer head");
assert.equal(merged.projected_revision, 8, "the merged state lost the newer projection");
assert.equal(merged.domains.providers_models.providers.length, 2, "the merged state kept the older projection");
assert.deepEqual(merged.drafts, staleView.drafts, "the merged state lost the live drafts of the newer head");

// The late answer to a read taken mid-write carries the old projection under a
// current head.  It must not replace a projection this window already applied.
const applied = applyPatch(snapshotAt(7, [provider]), patch(7, [
  { domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: model("public-route-2") },
]));
assert.ok(applied);
const lateStale = { ...snapshotAt(applied.revision, [provider]), projected_revision: 7 };
const kept = mergeSnapshot(applied, lateStale);
assert.equal(kept.domains, applied.domains, "a late stale projection replaced a newer one");
assert.equal(kept.projected_revision, 8, "the newer projection gave up its revision");
assert.equal(kept.revision, applied.revision, "the head moved without a newer read");
assert.equal(mergeSnapshot(applied, applied), applied, "an unchanged state was rebuilt");

// ---------------------------------------------------------------------------
// The per-domain store: one reference per domain, one notify per moved domain.
// ---------------------------------------------------------------------------
let providerNotifications = 0;
let runtimeNotifications = 0;
const unsubscribeProviders = subscribeDomain("providers_models")(() => { providerNotifications += 1; });
const unsubscribeRuntime = subscribeDomain("runtime")(() => { runtimeNotifications += 1; });

publishSnapshot(base);
assert.equal(providerNotifications, 1, "the first state did not reach a domain subscriber");
assert.equal(runtimeNotifications, 1);
publishSnapshot({ ...base, service: { state: "idle" } });
assert.deepEqual([providerNotifications, runtimeNotifications], [1, 1], "a service-only state notified a domain");
publishSnapshot(added);
assert.deepEqual([providerNotifications, runtimeNotifications], [2, 1], "the patched domain did not notify exactly once");
publishSnapshot({ ...added, domains: { ...added.domains, runtime: { domain: "runtime", revision: 9 } } });
assert.deepEqual([providerNotifications, runtimeNotifications], [2, 2], "the moved runtime domain did not notify");
assert.equal(getDomain("providers_models"), added.domains.providers_models, "the store did not keep the domain reference");
unsubscribeProviders();
unsubscribeRuntime();
publishSnapshot(base);
assert.deepEqual([providerNotifications, runtimeNotifications], [2, 2], "an unsubscribed listener was still called");

// ---------------------------------------------------------------------------
// One action, one read: a connected channel with a moved revision needs no
// post-action snapshot.  Everything else keeps it.
// ---------------------------------------------------------------------------
const channel = { received: 1, revision: 5 };
let localRevision = 5;
let skipped = 0;
let refreshed = 0;
for (let index = 0; index < 12; index += 1) {
  const revisionBefore = localRevision;
  localRevision += 1;
  const needed = postActionRefreshNeeded({ channel, revisionBefore, revisionAfter: localRevision, localRevision });
  if (needed) refreshed += 1;
  else skipped += 1;
  // The patch for the action arrives before the next one starts.
  channel.revision = localRevision;
}
assert.deepEqual([skipped, refreshed], [12, 0], "a connected, continuous action still read the snapshot");

assert.equal(
  postActionRefreshNeeded({ channel: { received: 0, revision: 5 }, revisionBefore: 5, revisionAfter: 6, localRevision: 6 }),
  true,
  "an action read the state while the channel had never delivered an event",
);
assert.equal(
  postActionRefreshNeeded({ channel, revisionBefore: 6, revisionAfter: 6, localRevision: 6 }),
  true,
  "an action that moved no revision skipped the read",
);
assert.equal(
  postActionRefreshNeeded({ channel, revisionBefore: 6, revisionAfter: 7, localRevision: 8 }),
  true,
  "an action whose answer was not the local head skipped the read",
);
assert.equal(
  postActionRefreshNeeded({ channel: { received: 1, revision: 3 }, revisionBefore: 6, revisionAfter: 7, localRevision: 7 }),
  true,
  "a window already behind skipped the read",
);

// ---------------------------------------------------------------------------
// The fields beside a domain's rows.  `model_contexts` is keyed by public name,
// so no entity op names it: a patch that adds a route carries the route's entry
// and the window merges it into the table it holds.  A patch that adds a route
// without the entry is refused -- reading the projection is how the pane gets
// the number instead of drawing 无 for a model Core just described.
// ---------------------------------------------------------------------------
const described = snapshotAt(9, [provider]);
described.domains.providers_models.model_contexts = {
  "public-route": { context_window: 200000, max_context_window: 200000, recommended_context_window: 200000 },
};
const withEntry = applyPatch(described, {
  protocol_version: 1,
  event: "patch",
  base_revision: 9,
  revision: 10,
  domains: ["providers_models"],
  domain_fields: {
    providers_models: {
      model_contexts: {
        "public-route-2": { context_window: 1000000, max_context_window: 1000000, recommended_context_window: 1000000 },
      },
    },
  },
  ops: [{ domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: model("public-route-2") }],
});
assert.ok(withEntry, "a patch that carried the context entry was refused");
assert.deepEqual(
  Object.keys(withEntry.domains.providers_models.model_contexts).sort(),
  ["public-route", "public-route-2"],
  "the entry beside the row was not merged into the table the window held",
);
assert.equal(withEntry.domains.providers_models.model_contexts["public-route-2"].context_window, 1000000);
assert.equal(withEntry.domains.providers_models.model_contexts["public-route"].context_window, 200000);

const withoutEntry = applyPatch(described, {
  protocol_version: 1,
  event: "patch",
  base_revision: 9,
  revision: 10,
  domains: ["providers_models"],
  ops: [{ domain: "providers_models", kind: "upsert", id: "model:public-route-2", value: model("public-route-2") }],
});
assert.equal(withoutEntry, undefined, "a route with no entry in the table was applied as 无");

// A domain listed only for its fields is still a state this module can hold:
// the context table moved with no row.
const fieldsOnly = applyPatch(described, {
  protocol_version: 1,
  event: "patch",
  base_revision: 9,
  revision: 10,
  domains: ["providers_models"],
  domain_fields: {
    providers_models: {
      model_contexts: {
        "public-route": { context_window: 210000, max_context_window: 210000, recommended_context_window: 210000 },
      },
    },
  },
  ops: [],
});
assert.ok(fieldsOnly, "a fields-only patch was refused");
assert.equal(fieldsOnly.domains.providers_models.model_contexts["public-route"].context_window, 210000);

console.log(`RN patch apply tests OK (skipped ${skipped}/${skipped + refreshed} post-action refreshes)`);
