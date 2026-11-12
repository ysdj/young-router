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
import { deadlineFor } from "../rn/packages/shared/src/platform/browserLeaf.ts";

const TWENTY_SECONDS = 20_000;
const FIFTEEN_MINUTES = 900_000;

function dispatch(type, payload = {}) {
  return JSON.stringify({
    protocol_version: 1,
    request_id: "web-test",
    method: "dispatch",
    params: { action: { type, payload } },
  });
}

for (const operation of ["start", "stop", "restart", "reload"]) {
  assert.equal(deadlineFor(dispatch(`service.${operation}`)), FIFTEEN_MINUTES, `service.${operation}`);
}

// Everything else keeps the twenty-second budget: a page that waits minutes on
// a snapshot or an edit is a page with no strip to say why.
assert.equal(deadlineFor(dispatch("provider.patch")), TWENTY_SECONDS);
assert.equal(deadlineFor(dispatch("logs.refresh_online_usage")), TWENTY_SECONDS);
assert.equal(deadlineFor(JSON.stringify({ method: "snapshot", params: {} })), TWENTY_SECONDS);
// A request this page did not build is not one it can classify.
assert.equal(deadlineFor("not json"), TWENTY_SECONDS);
assert.equal(deadlineFor(JSON.stringify({ method: "dispatch", params: {} })), TWENTY_SECONDS);

console.log("browser deadline ok");
