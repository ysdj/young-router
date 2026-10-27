#!/usr/bin/env node
import assert from "node:assert/strict";
import {
  MODEL_ROW_KEY_SEPARATOR,
  modelGroupRowKey,
  modelIDFromRowKey,
  modelRowKey,
} from "../rn/packages/shared/src/ui/modelRowKeys.ts";

// A credential id is itself prefixed (`account:<ref>`, `key:<slot>`), so the
// model a row addresses has to survive a round trip even when both parts carry
// colons.  A colon-joined key turned a click on a model into a lookup for a
// model named after its own credential, and the pane dropped the selection.
for (const [credential, model] of [
  ["account:acc-1", "model-9f"],
  ["key:k-77", "model-9f"],
  ["key:k-77", "model-with:colon"],
  ["account:acc-1", `model${MODEL_ROW_KEY_SEPARATOR}named`],
]) {
  assert.equal(modelIDFromRowKey(modelRowKey(credential, model)), model);
}

// A group heading addresses no model, and a click below the rows reports the
// empty key: neither may be read as a model id.
assert.equal(modelIDFromRowKey(modelGroupRowKey("account:acc-1")), undefined);
assert.equal(modelIDFromRowKey(""), undefined);

// A provider with no credential groups its models under their own ids.
assert.equal(modelIDFromRowKey("model-plain"), "model-plain");

// Two credentials serving the same model are two distinct rows with one model.
assert.notEqual(modelRowKey("account:a", "model-9f"), modelRowKey("account:b", "model-9f"));
assert.equal(
  modelIDFromRowKey(modelRowKey("account:a", "model-9f")),
  modelIDFromRowKey(modelRowKey("account:b", "model-9f")),
);

console.log("RN model row key regression tests OK");
