#!/usr/bin/env node
import assert from "node:assert/strict";
import { orderedModelGroupIDs } from "../rn/packages/shared/src/ui/modelGroupOrder.ts";

// The groups follow the key table.  They used to follow the order the routes
// introduced them, so deleting a key's first route moved that key's whole
// group to the bottom of the list -- the user deleted a model and watched an
// unrelated key jump to the end of the pane.
{
  const keys = ["k1", "k2", "k3"];
  assert.deepEqual(orderedModelGroupIDs(["k2", "k1"], keys), ["k1", "k2"]);
  // The same two groups, however the routes were walked, come out the same.
  assert.deepEqual(orderedModelGroupIDs(["k1", "k2"], keys), ["k1", "k2"]);
  // A group the table no longer lists goes last: it is a route whose slot was
  // deleted elsewhere, and it has no position in the table to borrow.
  assert.deepEqual(orderedModelGroupIDs(["gone", "k3", "k1"], keys), ["k1", "k3", "gone"]);
}

// 未定义密钥 is not a key: no slot id is empty, so the heading a keyless route
// sits under is stated first and never sorted among the real keys.
{
  assert.deepEqual(orderedModelGroupIDs(["k2", "", "k1"], ["k1", "k2"]), ["", "k1", "k2"]);
  assert.deepEqual(orderedModelGroupIDs(["", "gone"], []), ["", "gone"]);
  assert.deepEqual(orderedModelGroupIDs([], ["k1"]), []);
}

// A group named twice is one heading, in the position its key has.
{
  assert.deepEqual(orderedModelGroupIDs(["k2", "k1", "k2"], ["k1", "k2"]), ["k1", "k2"]);
}

console.log("RN model group order regression tests OK");
