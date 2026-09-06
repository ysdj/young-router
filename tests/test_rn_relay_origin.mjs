#!/usr/bin/env node
import assert from "node:assert/strict";
import { normalizeRelayOrigin, suggestedProviderName, suggestedRelayStationName } from "../rn/packages/shared/src/ui/relayOrigin.ts";

assert.equal(normalizeRelayOrigin("aaa.com/"), "https://aaa.com");
assert.equal(normalizeRelayOrigin("https://x.bbb.com/login"), "https://x.bbb.com");

assert.equal(suggestedRelayStationName("aaa.com"), "aaa");
assert.equal(suggestedRelayStationName("x.bbb.com"), "bbb");
assert.equal(suggestedRelayStationName("https://api.example.co.uk/login"), "example");
assert.equal(suggestedRelayStationName("http://localhost:4000"), "localhost");

// Provider-name suggestion stays empty while the hostname is incomplete and
// then yields the domain label before the TLD.
assert.equal(suggestedProviderName("h"), "");
assert.equal(suggestedProviderName("https://a"), "");
assert.equal(suggestedProviderName("https://api"), "");
assert.equal(suggestedProviderName("https://a.b"), "");
assert.equal(suggestedProviderName("https://api.openai.com/v1"), "openai");
assert.equal(suggestedProviderName("api.sisct2.xyz/v1"), "sisct2");
assert.equal(suggestedProviderName("https://flux-code.cc/v1"), "flux-code");
assert.equal(suggestedProviderName("http://localhost:4000"), "");

console.log("RN relay origin regression tests OK (normalization, station-name and provider-name suggestion)");
