#!/usr/bin/env node
import assert from "node:assert/strict";
import { providerKeyChoiceLabel } from "../rn/packages/shared/src/ui/providerKeyLabels.ts";
import { providedKeyName } from "../rn/packages/shared/src/ui/providedKeyName.ts";

const words = {
  "relay.apiKeyUngrouped": "No group",
  "common.notAvailable": "N/A",
  "providers.defaultKey": "Default key",
};
const translate = (key) => words[key] ?? key;

const noGroup = { id: "", name: "ungrouped-key", apiName: "Ungrouped key", groupID: "", groupName: "" };
const translatedNameGroup = { id: "group-1", name: "No group", multiplier: null };
const groupedKey = { ...noGroup, groupID: translatedNameGroup.id };

assert.equal(providedKeyName(groupedKey, [translatedNameGroup], translate), "No group");
assert.equal(providedKeyName(noGroup, [], translate), "ungrouped-key");

// A selected orphan slot carries the account prefix even after the station
// resource disappears; a live source remains the authoritative full label.
assert.equal(providerKeyChoiceLabel({ name: "grok", kind: "relay", accountPrefix: "acct" }, translate), "acct/grok");
assert.equal(providerKeyChoiceLabel({ name: "acct/grok", kind: "relay", accountPrefix: "acct" }, translate), "acct/grok");
assert.equal(providerKeyChoiceLabel({
  name: "grok",
  kind: "relay",
  accountPrefix: "acct",
  source: { accountLabel: "acct", resourceLabel: "grok", multiplier: 2 },
}, translate), "acct/grok (2x)");

console.log("RN provider key label regressions OK (translated group names, ungrouped keys, orphan prefixes)");
