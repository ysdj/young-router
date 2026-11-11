#!/usr/bin/env node
import assert from "node:assert/strict";
import { normalizeRelayOrigin, relayLoginLandedAccount, stationOriginKey, suggestedProviderName, suggestedRelayStationName } from "../rn/packages/shared/src/ui/relayOrigin.ts";

assert.equal(normalizeRelayOrigin("aaa.com/"), "https://aaa.com");
assert.equal(normalizeRelayOrigin("https://x.bbb.com/login"), "https://x.bbb.com");

assert.equal(suggestedRelayStationName("aaa.com"), "aaa");
assert.equal(suggestedRelayStationName("x.bbb.com"), "bbb");
assert.equal(suggestedRelayStationName("aaa.bb.cc"), "bb");
assert.equal(suggestedRelayStationName("https://api.example.co.uk/login"), "example");
assert.equal(suggestedRelayStationName("http://localhost:4000"), "localhost");

// Provider-name suggestion stays empty while the hostname is incomplete and
// then yields the domain label before the TLD.
assert.equal(suggestedProviderName("h"), "");
assert.equal(suggestedProviderName("https://a"), "");
assert.equal(suggestedProviderName("https://api"), "");
assert.equal(suggestedProviderName("https://a.b"), "");
assert.equal(suggestedProviderName("https://api.openai.com/v1"), "openai");
assert.equal(suggestedProviderName("api.gamma.example/v1"), "gamma");
assert.equal(suggestedProviderName("https://atlas.example/v1"), "atlas");
assert.equal(suggestedProviderName("aaa.bb.cc"), "bb");
assert.equal(suggestedProviderName("https://aaa.cc/v1"), "aaa");
assert.equal(suggestedProviderName("https://api.example.co.uk/login"), "example");
assert.equal(suggestedProviderName("http://localhost:4000"), "");
assert.equal(suggestedProviderName("http://127.0.0.1:4000"), "");

// A station is one site whatever the spelling of its base URL: the host is
// case-insensitive, a default port is not part of the identity, and the
// dashboard/API leaf is trimmed.  A deployment base path stays.
assert.equal(stationOriginKey("HTTPS://WWW.Example.test:443/v1/"), "https://www.example.test");
assert.equal(stationOriginKey("https://example.test:8443/relay/"), "https://example.test:8443/relay");

// A sign-in asks with a pending id; the answer names the row the station
// actually proved.  Reading only the pending id turns a successful re-login
// into a failed resource read.
const landed = [
  { id: "pending-login", origin: "https://relay.example.test", username: "", loginStatus: "signed_out" },
  { id: "kept", origin: "https://relay.example.test", username: "person@example.test", loginStatus: "signed_in" },
];
assert.equal(relayLoginLandedAccount(landed, "pending-login", "https://relay.example.test/v1", "person@example.test")?.id, "kept");
assert.equal(relayLoginLandedAccount(landed, "pending-login", "https://relay.example.test", "")?.id, "kept");
assert.equal(relayLoginLandedAccount(landed, "pending-login", "https://relay.example.test", "other@example.test")?.id, "pending-login");
assert.equal(relayLoginLandedAccount(landed, "pending-login", "https://other.example.test", "person@example.test")?.id, "pending-login");

console.log("RN relay origin regression tests OK (normalization, station identity, sign-in landing, station-name and provider-name suggestion)");
