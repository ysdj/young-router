#!/usr/bin/env node
/**
 * The page gives a proxy lifecycle call the same budget the native hosts do.
 *
 * Starting the proxy waits for three worker processes and then for the
 * controller's own health window -- minutes on a cold machine.  The page gave
 * every call twenty seconds, so 启动服务 reported "Core did not answer" while
 * Core was still starting, and the retry that followed met a start already in
 * flight.  The macOS and Windows hosts grant these calls fifteen minutes for
 * exactly this reason; this pins the page to the same number, and pins every
 * other call to the twenty-second budget it had.
 */
import assert from "node:assert/strict";
import { createIpcClient } from "../rn/packages/shared/src/ipc.ts";
import { createNativeIpcTransport } from "../rn/packages/shared/src/platform/nativeBridge.ts";
import { createBrowserIpcBridge, deadlineFor } from "../rn/packages/shared/src/platform/browserLeaf.ts";

const TWENTY_SECONDS = 20_000;
const FIFTEEN_MINUTES = 900_000;

function dispatch(type, payload = {}) {
  const domain = type.startsWith("logs.") ? "logs" : undefined;
  return JSON.stringify({
    protocol_version: 1,
    request_id: "web-test",
    method: "dispatch",
    params: { action: { type, ...(domain ? { domain } : {}), payload } },
  });
}

for (const operation of ["start", "stop", "restart", "reload"]) {
  assert.equal(deadlineFor(dispatch(`service.${operation}`)), FIFTEEN_MINUTES, `service.${operation}`);
}

// A probe reads the upstream, so the page gives it the same fifteen minutes the
// native hosts do.  A flash route's press measures the route's speed -- seconds
// of streaming output on top of the surface probes -- and the twenty-second
// edit budget aborted a measurement that was running normally.
assert.equal(deadlineFor(JSON.stringify({ method: "probe", params: { provider_id: "p", model_id: "m" } })), FIFTEEN_MINUTES);

// A usage read reaches the relay and gets the same fifteen-minute budget as
// the native hosts, including paging and time-range operations. Everything
// else keeps the twenty-second budget.
assert.equal(deadlineFor(dispatch("provider.patch")), TWENTY_SECONDS);
for (const operation of [
  "logs.refresh_online_usage",
  "logs.refresh",
  "logs.reload",
  "logs.load_more",
  "logs.previous_page",
  "logs.first_page",
  "logs.last_page",
  "logs.goto_page",
  "logs.set_page_rows",
  "logs.set_time_range",
]) {
  assert.equal(deadlineFor(dispatch(operation, { tab: "online-usage" })), FIFTEEN_MINUTES, operation);
}
assert.equal(deadlineFor(JSON.stringify({ method: "snapshot", params: {} })), TWENTY_SECONDS);
// A request this page did not build is not one it can classify.
assert.equal(deadlineFor("not json"), TWENTY_SECONDS);
assert.equal(deadlineFor(JSON.stringify({ method: "dispatch", params: {} })), TWENTY_SECONDS);

// A stale browser subscription is replaced with a fresh one and a full
// snapshot. Removing the last React listener is temporary: the shared IPC
// client keeps its transport subscription and does not send another subscribe
// request when the listener mounts again.
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
let subscriptionRequests = 0;
let eventRequests = 0;
let reloads = 0;
let expireEvents = false;
let emitPatch = false;
const liveSnapshot = {
  protocol_version: 1,
  revision: 2,
  projected_revision: 2,
  core_instance_id: "browser-retry",
  service: { state: "running" },
  domains: {},
  drafts: {},
  disk: {},
  logs: { tabs: {}, entries: [], revision: 2 },
};
const response = (status, payload) => ({
  status,
  ok: status >= 200 && status < 300,
  async text() { return JSON.stringify(payload); },
  async json() { return payload; },
});
globalThis.window = { location: { reload() { reloads += 1; } } };
const startupRequest = JSON.stringify({ protocol_version: 1, request_id: "startup", method: "snapshot", params: {} });
// A proxy replacement retires the worker carrying this page's own /ipc call:
// the answer is a 500 while it shuts down, or a 502 while no worker listens on
// the published port.  Both used to end the press and report the local core as
// unavailable.  The same envelope is sent again -- request id included, so
// Core's journal answers the first attempt instead of running it twice -- and
// the press lands once a worker is back.
let helloAttempts = 0;
let snapshotAttempts = 0;
const retriedBodies = [];
globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith("/ipc/hello")) {
    helloAttempts += 1;
    return helloAttempts === 1 ? response(503, {}) : response(200, { ok: true });
  }
  snapshotAttempts += 1;
  retriedBodies.push(init.body);
  if (snapshotAttempts === 1) return response(502, {});
  if (snapshotAttempts === 2) return response(500, {});
  return response(200, { protocol_version: 1, request_id: "startup", ok: true, result: { snapshot: liveSnapshot } });
};
const startup = createBrowserIpcBridge({ baseURL: "" });
assert.deepEqual(JSON.parse(await startup.send(startupRequest)).result.snapshot, liveSnapshot);
assert.equal(helloAttempts, 2, "a greeting cut by the restart is asked again");
assert.equal(snapshotAttempts, 3, "a 5xx from the retiring worker is retried");
assert.ok(retriedBodies.every((body) => body === startupRequest), "a retry re-sends the same envelope, request id included");
assert.equal(reloads, 0, "Core can recover from a temporary failure on the same page");

// A refusal Core stated is an answer, not a lost connection: retrying it would
// report the same refusal again and hold the strip in "working" for the whole
// budget.  The page renders the envelope's own code.
let refusalAttempts = 0;
globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith("/ipc/hello")) return response(200, { ok: true });
  refusalAttempts += 1;
  const request = JSON.parse(init.body);
  return response(400, {
    protocol_version: 1,
    request_id: request.request_id,
    ok: false,
    error: { code: "follow_rate_unavailable", message: "The account states no rate for this route" },
  });
};
const refused = createBrowserIpcBridge({ baseURL: "" });
const refusal = JSON.parse(await refused.send(JSON.stringify({ protocol_version: 1, request_id: "refused-1", method: "dispatch", params: { action: { type: "model.patch" } } })));
assert.equal(refusal.error.code, "follow_rate_unavailable");
assert.equal(refusalAttempts, 1, "a protocol refusal is not retried");

globalThis.fetch = async (url) => String(url).endsWith("/ipc/hello")
  ? response(401, {})
  : response(200, { protocol_version: 1, request_id: "startup", ok: true, result: { snapshot: liveSnapshot } });
const expired = createBrowserIpcBridge({ baseURL: "" });
await Promise.all([assert.rejects(expired.send(startupRequest)), assert.rejects(expired.send(startupRequest))]);
await assert.rejects(expired.send(startupRequest));
assert.equal(reloads, 1, "an expired session at hello leaves for sign-in only once");
reloads = 0;

globalThis.fetch = async (url, init = {}) => {
  if (String(url).endsWith("/ipc/hello")) return response(200, { ok: true });
  if (String(url).includes("/ipc/events?")) {
    eventRequests += 1;
    await new Promise((resolve) => setTimeout(resolve, 2));
    if (expireEvents) return response(401, {});
    if (eventRequests === 1) return response(410, {});
    if (emitPatch) {
      emitPatch = false;
      return response(200, { event: { protocol_version: 1, event: "resync", revision: 3 } });
    }
    return response(200, { event: null });
  }
  const request = JSON.parse(init.body);
  if (request.method === "subscribe") {
    subscriptionRequests += 1;
    return response(200, {
      protocol_version: 1,
      request_id: request.request_id,
      ok: true,
      result: { subscription_id: `browser-sub-${subscriptionRequests}` },
    });
  }
  if (request.method === "snapshot") {
    return response(200, {
      protocol_version: 1,
      request_id: request.request_id,
      ok: true,
      result: { snapshot: liveSnapshot },
    });
  }
  throw new Error(`Unexpected browser IPC method: ${request.method}`);
};

const bridge = createBrowserIpcBridge({ baseURL: "" });
const page = createIpcClient(createNativeIpcTransport(bridge));
const events = [];
const onEvent = (event) => events.push(event);
const unsubscribeFirst = page.subscribe(onEvent);
const waitFor = async (predicate) => {
  const deadline = Date.now() + 3000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("Timed out waiting for browser event recovery");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
await waitFor(() => events.some((event) => event.event === "snapshot"));
assert.equal(subscriptionRequests, 2, "410 must create a fresh Core subscription");
unsubscribeFirst();
const unsubscribeSecond = page.subscribe(onEvent);
emitPatch = true;
await waitFor(() => events.some((event) => event.event === "resync"));
assert.equal(subscriptionRequests, 2, "remounting a listener must keep the existing poll");
unsubscribeSecond();
expireEvents = true;
await waitFor(() => reloads === 1);
globalThis.fetch = originalFetch;
globalThis.window = originalWindow;

console.log("browser deadline ok");
