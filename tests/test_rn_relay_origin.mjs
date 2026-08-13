#!/usr/bin/env node
import assert from "node:assert/strict";
import { normalizeRelayOrigin, suggestedRelayStationName } from "../rn/packages/shared/src/ui/relayOrigin.ts";

assert.equal(normalizeRelayOrigin("aaa.com/"), "https://aaa.com");
assert.equal(normalizeRelayOrigin("https://x.bbb.com/login"), "https://x.bbb.com");

assert.equal(suggestedRelayStationName("aaa.com"), "aaa");
assert.equal(suggestedRelayStationName("x.bbb.com"), "bbb");
assert.equal(suggestedRelayStationName("https://api.example.co.uk/login"), "example");
assert.equal(suggestedRelayStationName("http://localhost:4000"), "localhost");

console.log("RN relay origin regression tests OK (normalization and station-name suggestion)");
