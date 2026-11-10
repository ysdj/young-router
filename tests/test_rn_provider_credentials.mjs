#!/usr/bin/env node
import assert from "node:assert/strict";
import { providerCredentialKeys, relayCredentialKey } from "../rn/packages/shared/src/ui/providerCredentials.ts";

const slot = (id, name, source, managed = false) => ({ id, name, managed, source });
const relay = (accountID, resourceID, name) => ({
  stationID: "station-a",
  accountID,
  resourceID,
  name,
});

// A provider whose station no longer lists any of its keys still holds them:
// 获取模型 could not offer a single one, while the key table listed all eleven
// under the account.  Both lists are this one now.
{
  const slots = [
    slot("slot-custom", "r-kiro", { kind: "independent" }),
    ...["Gemini分组", "国模", "Gemini生图", "生图分组"].map((name, index) =>
      slot(`slot-${index}`, name, {
        kind: "relay",
        stationID: "station-a",
        accountID: "login-1",
        resourceID: `sub2api-40${index}`,
      })),
  ];
  const keys = providerCredentialKeys(slots, []);
  assert.deepEqual(keys.map((entry) => entry.name), ["r-kiro", "Gemini分组", "国模", "Gemini生图", "生图分组"]);
  // Every one of them names the slot the provider holds, so a pick can be
  // answered from the document itself.
  assert.deepEqual(keys.map((entry) => entry.slotID), ["slot-custom", "slot-0", "slot-1", "slot-2", "slot-3"]);
  assert.equal(keys.filter((entry) => entry.resource !== undefined).length, 0);
  assert.equal(keys.filter((entry) => entry.kind === "relay").length, 4);
}

// A slot and the station resource it names are one credential, not two rows:
// the picker used to list the slot and then the same group again as an
// importable source, which is what the key table could never match.
{
  const slots = [
    slot("slot-0", "国模", { kind: "relay", stationID: "station-a", accountID: "login-1", resourceID: "sub2api-1" }),
  ];
  const keys = providerCredentialKeys(slots, [relay("login-1", "sub2api-1", "国模")]);
  assert.equal(keys.length, 1);
  assert.equal(keys[0].slotID, "slot-0");
  assert.equal(keys[0].resource?.resourceID, "sub2api-1");
  assert.equal(keys[0].key, relayCredentialKey("login-1", "sub2api-1"));
}

// A station key no slot holds is a row the table already shows and 获取模型 may
// import; it is offered once, the same way.
{
  const slots = [
    slot("slot-0", "国模", { kind: "relay", stationID: "station-a", accountID: "login-1", resourceID: "sub2api-1" }),
  ];
  const keys = providerCredentialKeys(slots, [
    relay("login-1", "sub2api-1", "国模"),
    relay("login-1", "sub2api-2", "Plus分组"),
  ]);
  assert.deepEqual(keys.map((entry) => [entry.name, entry.slotID]), [["国模", "slot-0"], ["Plus分组", undefined]]);
}

// Two accounts on one station can both list a group of the same name.  They are
// two keys with two secrets, so neither may collapse into the other.
{
  const keys = providerCredentialKeys([], [relay("login-1", "sub2api-1", "国模"), relay("login-2", "sub2api-1", "国模")]);
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0].key, keys[1].key);
}

// A slot Core owns is not a credential the user may pick — the key table never
// offers it, so the picker must not either.
{
  const keys = providerCredentialKeys([
    slot("slot-managed", "workbuddy", { kind: "independent" }, true),
    slot("slot-custom", "r-kiro", { kind: "independent" }),
  ], []);
  assert.deepEqual(keys.map((entry) => entry.name), ["r-kiro"]);
}

// A slot that names no account or resource is still one row of its own: two
// such slots must not be read as one credential.
{
  const keys = providerCredentialKeys([
    slot("slot-a", "half-bound", { kind: "relay", stationID: "station-a" }),
    slot("slot-b", "half-bound-2", { kind: "relay" }),
  ], []);
  assert.deepEqual(keys.map((entry) => [entry.key, entry.name]), [["slot:slot-a", "half-bound"], ["slot:slot-b", "half-bound-2"]]);
}

// Order is the table's: the user's own keys, the station's, then the slots the
// station dropped.
{
  const keys = providerCredentialKeys([
    slot("slot-orphan", "Claude Max", { kind: "relay", stationID: "station-a", accountID: "login-1", resourceID: "gone" }),
    slot("slot-custom", "r-kiro", { kind: "independent" }),
    slot("slot-held", "国模", { kind: "relay", stationID: "station-a", accountID: "login-1", resourceID: "sub2api-1" }),
  ], [relay("login-1", "sub2api-1", "国模")]);
  assert.deepEqual(keys.map((entry) => entry.name), ["r-kiro", "国模", "Claude Max"]);
}

console.log("RN provider credential list regression tests OK");
