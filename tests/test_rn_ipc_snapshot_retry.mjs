#!/usr/bin/env node
/**
 * A dispatch that landed is not an action that failed.
 *
 * Core can answer a snapshot with an envelope whose request id is not the one
 * this client sent -- the state behind it is still the state after the
 * dispatch.  Treating that as a read failure made the wizard report nothing
 * for a provider it had just created, and the next Finish then met the same
 * provider as a duplicate name.  The client retries the read once.
 */
import assert from "node:assert/strict";
import { createIpcClient } from "../rn/packages/shared/src/ipc.ts";

const snapshot = {
  protocol_version: 1,
  revision: 7,
  projected_revision: 7,
  core_instance_id: "original",
  service: { state: "running" },
  domains: { providers_models: { revision: 7, state: { providers: [] } } },
  drafts: {},
  disk: {},
  webdav: { enabled: false, configured: false, last_probe: "unknown", password: { present: false } },
  logs: { tabs: {}, entries: [], revision: 7 },
};

let calls = 0;
let deliverEvent;
const transport = {
  async send(request) {
    if (request.method === "subscribe") {
      return { protocol_version: 1, request_id: request.request_id, ok: true, result: { subscription_id: "sub" } };
    }
    calls += 1;
    if (calls === 1) {
      // The wrong request id: unreadable as this request's answer.
      return {
        protocol_version: 1,
        request_id: "some-other-request",
        ok: true,
        result: { snapshot },
      };
    }
    return {
      protocol_version: request.protocol_version,
      request_id: request.request_id,
      ok: true,
      result: { snapshot },
    };
  },
  subscribe(listener) {
    deliverEvent = listener;
    return () => undefined;
  },
};

const client = createIpcClient(transport);
const read = await client.snapshot();
assert.equal(read.revision, 7);
assert.equal(calls, 2, "the unreadable envelope must be retried once");
assert.equal(client.latestSnapshot()?.revision, 7);

// A second read is a fresh request and keeps working.
const again = await client.snapshot();
assert.equal(again.revision, 7);
assert.equal(calls, 3);

const seen = [];
const unsubscribe = client.subscribe(() => seen.push(client.latestSnapshot()));
deliverEvent({ protocol_version: 1, event: "patch", base_revision: 7, revision: 8, domains: [], ops: [], service: { state: "stopped" } });
assert.equal(client.latestSnapshot()?.service.state, "stopped");
assert.equal(seen[0]?.revision, 8, "a new root must see the patch before its listeners run");
deliverEvent({ protocol_version: 1, event: "patch", base_revision: 10, revision: 11, domains: [], ops: [] });
assert.equal(client.latestSnapshot()?.revision, 8, "a patch across a gap cannot advance the cache");
const restarted = { ...snapshot, core_instance_id: "replacement", revision: 1, projected_revision: 1 };
deliverEvent({ protocol_version: 1, event: "snapshot", revision: 1, snapshot: restarted });
assert.equal(client.latestSnapshot(), restarted, "the replacement Core's snapshot may start at a lower revision");
unsubscribe();

console.log("RN IPC snapshot retry and cache regressions OK");
