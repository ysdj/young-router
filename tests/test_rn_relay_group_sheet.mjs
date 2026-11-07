#!/usr/bin/env node
/**
 * 分组管理's page-side state machine.
 *
 * The native hosts implement this sheet four times over in Swift and C++; the
 * browser host implements it once, in `platform/relayGroupSheet.ts`, and its
 * contract is exactly the four optional leaf calls the pane already knows how
 * to use.  What is asserted here is that contract, because the failure it
 * replaces was silent: the page answered `undefined` for all four, the pane
 * disabled 分组, and a button that reads 分组 and does nothing looked like a
 * dead control rather than a stated limitation.
 */
import assert from "node:assert/strict";
import {
  askCloseRelayGroupSheet,
  closeRelayGroupSheet,
  finishRelayGroupApply,
  openRelayGroupSheet,
  relayGroupSheetState,
  requestRelayGroupApply,
  setRelayGroupSheetCloseQuestion,
  setRelayGroupSheetSurface,
  takeRelayGroupApply,
  updateRelayGroupSheet,
} from "../rn/packages/shared/src/platform/relayGroupSheet.ts";

const labels = {
  listLabel: "密钥", addLabel: "添加", removeLabel: "删除", nameLabel: "密钥名",
  groupLabel: "分组", multiplierLabel: "倍率", valueLabel: "密钥值", copyLabel: "复制",
  failedLabel: "操作失败", modelsLabel: "模型列表", emptyLabel: "无", enabledLabel: "启用",
  newKeyName: "新密钥", autoGroupingLabel: "自动分组", autoGroupingHelp: "帮助",
  ungroupedLabel: "未分组", closeLabel: "关闭", applyLabel: "保存并关闭",
  discardTitle: "放弃未保存的更改？", discardBody: "关闭会丢弃更改。", discardConfirm: "放弃更改",
  loadingLabel: "正在读取中转站…",
};
const snapshot = {
  accountLabel: "a@example.com",
  groups: [{ id: "g1", label: "g1 ×1", name: "g1", rate: "×1" }],
  keys: [{ id: "k1", name: "g1", groupID: "g1", groupLabel: "g1", multiplier: "×1", hint: "", models: [], enabled: true }],
  autoGrouping: false,
};

const shown = [];
const dismissed = [];
setRelayGroupSheetSurface({ present: () => shown.push(1), dismiss: () => dismissed.push(1) });

// Opening a sheet shows exactly one layer and answers only when it ends.
const opening = openRelayGroupSheet({ title: "分组", accountId: "acct", labels, loading: true, ...snapshot });
assert.deepEqual(shown, [1]);
assert.equal(relayGroupSheetState().loading, true);
assert.equal(relayGroupSheetState().saving, false);

// A later read of the same account replaces the rows and ends the wait.
assert.equal(updateRelayGroupSheet(snapshot), true);
assert.equal(relayGroupSheetState().loading, false);
assert.equal(updateRelayGroupSheet(snapshot), true);
closeRelayGroupSheet();
assert.deepEqual(dismissed, [1]);
assert.equal(await opening, undefined, "a sheet closed without saving answers undefined");
assert.equal(updateRelayGroupSheet(snapshot), false, "a read that lands after Close changes nothing");

// 保存并关闭 hands the draft to the pane and stays up until the write answers.
const second = openRelayGroupSheet({ title: "分组", accountId: "acct", labels, ...snapshot });
const awaiting = takeRelayGroupApply();
requestRelayGroupApply({ autoGrouping: false, creates: [], updates: [], deletes: ["k1"] });
assert.equal(relayGroupSheetState().saving, true);
assert.deepEqual(await awaiting, { autoGrouping: false, creates: [], updates: [], deletes: ["k1"] });
finishRelayGroupApply({ status: "更改未生效", close: false });
assert.equal(relayGroupSheetState().saving, false);
assert.equal(relayGroupSheetState().status, "更改未生效");
// A refused save keeps the sheet and its 保存并关闭: the pane asks again.
const retry = takeRelayGroupApply();
requestRelayGroupApply({ autoGrouping: false, creates: [], updates: [], deletes: ["k1"] });
assert.deepEqual(await retry, { autoGrouping: false, creates: [], updates: [], deletes: ["k1"] });
finishRelayGroupApply({ status: "已保存", close: true });
assert.equal(relayGroupSheetState(), undefined);
assert.deepEqual(dismissed, [1, 1]);
assert.equal(await second, undefined, "the sheet's own close answers the opening request");

// Close goes through the sheet's own question, because only its fields know
// whether the draft would be lost.
openRelayGroupSheet({ title: "分组", accountId: "acct", labels, ...snapshot });
let asked = 0;
setRelayGroupSheetCloseQuestion(() => { asked += 1; });
askCloseRelayGroupSheet();
assert.equal(asked, 1, "Close asks instead of closing over a draft");
assert.notEqual(relayGroupSheetState(), undefined);
setRelayGroupSheetCloseQuestion(undefined);
askCloseRelayGroupSheet();
assert.equal(relayGroupSheetState(), undefined);

// Asking for one sheet again replaces the one on screen rather than stacking a
// second 保存并关闭 over the same key list.
openRelayGroupSheet({ title: "分组", accountId: "acct", labels, ...snapshot });
const first = relayGroupSheetState();
openRelayGroupSheet({ title: "分组", accountId: "other", labels, ...snapshot });
assert.equal(relayGroupSheetState().accountId, "other");
assert.notEqual(relayGroupSheetState(), first);
closeRelayGroupSheet();

console.log("RN relay group sheet regression tests OK (open, update, apply, discard, reopen)");
