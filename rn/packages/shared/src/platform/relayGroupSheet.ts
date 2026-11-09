/**
 * 分组管理's sheet, as the browser host keeps it.
 *
 * The native hosts give the sheet its own AppKit/WinUI window, driven by Core's
 * own capability leases; a page has neither, so the same surface is a React
 * layer (`ui/RelayGroupSheet.tsx`) over the page, and this module is the state
 * the pane and that layer exchange.  It is deliberately shaped exactly like the
 * three optional leaf capabilities it serves:
 *
 *  - `showGroupManager` opens the sheet and answers when it closes,
 *  - `awaitGroupManagerApply` is the pane waiting for 保存并关闭,
 *  - `updateGroupManager` replaces the sheet's rows with a later read,
 *  - `finishGroupManagerApply` answers that save in the sheet's own strip.
 *
 * One sheet at a time, because the pane has one account selected and a second
 * sheet would be a second 保存并关闭 over the same key list.  Nothing here holds
 * a key value: the copy action goes through Core's lease and stops at the
 * clipboard, which is what the native windows do with it.
 */

import type {
  RelayGroupManagerLabels,
  RelayGroupManagerResult,
  RelayGroupManagerSnapshot,
} from "../types";

/** What the layer draws: the pane's request plus the sheet's own live state. */
export type RelayGroupSheetState = {
  title: string;
  /** The account that owns the keys; names the copy action's secret target. */
  accountId: string;
  labels: RelayGroupManagerLabels;
  /** The account's aligned layout is still being read. */
  loading: boolean;
  /** A 保存并关闭 is with the pane: its rows stay put until Core answers. */
  saving: boolean;
  /** The last outcome the pane stated, or a failure this sheet found itself. */
  status?: string;
  snapshot: RelayGroupManagerSnapshot;
};

type Sheet = {
  state: RelayGroupSheetState;
  resolveShow: (result: RelayGroupManagerResult | undefined) => void;
  /** One waiter per 保存并关闭: a refused save asks again on the same sheet. */
  applyWaiters: Array<(result: RelayGroupManagerResult | undefined) => void>;
  /** The layer's own "may I close?" — it is where the discard question lives. */
  askClose?: () => void;
};

let sheet: Sheet | undefined;
/** How this host draws a sheet: present the layer, and take it away again. */
let surface: { present: () => void; dismiss: () => void } | undefined;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function update(patch: Partial<RelayGroupSheetState>): void {
  if (!sheet) return;
  sheet.state = { ...sheet.state, ...patch };
  emit();
}

export function subscribeRelayGroupSheet(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function relayGroupSheetState(): RelayGroupSheetState | undefined {
  return sheet?.state;
}

/** The page's layer implementation, registered once by the web entry. */
export function setRelayGroupSheetSurface(next: { present: () => void; dismiss: () => void }): void {
  surface = next;
}

/**
 * Open the sheet for one account.
 *
 * A sheet already on screen is closed first: two requests for the same surface
 * are the pane changing its mind about the account, and a layer left behind by
 * the first would keep answering the second one's 保存并关闭.
 */
export function openRelayGroupSheet(options: {
  title: string;
  accountId: string;
  labels: RelayGroupManagerLabels;
  loading?: boolean;
} & RelayGroupManagerSnapshot): Promise<RelayGroupManagerResult | undefined> {
  if (sheet) closeRelayGroupSheet();
  const { title, accountId, labels, loading, ...snapshot } = options;
  return new Promise<RelayGroupManagerResult | undefined>((resolve) => {
    sheet = {
      state: { title, accountId, labels, loading: loading === true, saving: false, snapshot },
      resolveShow: resolve,
      applyWaiters: [],
    };
    emit();
    surface?.present();
  });
}

/** Replace the open sheet's content with a later read; false when it is gone. */
export function updateRelayGroupSheet(snapshot: RelayGroupManagerSnapshot): boolean {
  if (!sheet) return false;
  update({ snapshot, loading: false });
  return true;
}

/**
 * The next 保存并关闭 this sheet hands over.
 *
 * Resolves `undefined` when the sheet ends without saving, so a pane that asks
 * again after a refused write keeps waiting instead of being told the user
 * cancelled.
 */
export function takeRelayGroupApply(): Promise<RelayGroupManagerResult | undefined> {
  if (!sheet) return Promise.resolve(undefined);
  return new Promise<RelayGroupManagerResult | undefined>((resolve) => {
    sheet!.applyWaiters.push(resolve);
  });
}

/** The sheet's 保存并关闭: its edit travels to the pane, which writes it. */
export function requestRelayGroupApply(result: RelayGroupManagerResult): void {
  if (!sheet || sheet.state.saving) return;
  const waiter = sheet.applyWaiters.shift();
  update({ saving: true, status: undefined });
  waiter?.(result);
}

/** Answer that request: state the outcome, and close when the write landed. */
export function finishRelayGroupApply(options: { status: string; close: boolean }): void {
  if (!sheet) return;
  if (options.close) {
    closeRelayGroupSheet();
    return;
  }
  update({ saving: false, status: options.status });
}

/** Close the sheet, dropping the draft and answering whoever is waiting. */
export function closeRelayGroupSheet(): void {
  if (!sheet) return;
  const closing = sheet;
  sheet = undefined;
  for (const waiter of closing.applyWaiters.splice(0)) waiter(undefined);
  emit();
  surface?.dismiss();
  closing.resolveShow(undefined);
}

/**
 * The layer's "may I close?" — the sheet's own discard question.
 *
 * A save in flight is the one time nothing may close the sheet: the pane still
 * holds the result and states it in this sheet's strip, so the title bar's × and
 * Escape have to refuse exactly as the footer's Close button (disabled while
 * `saving`) does.  The save's own answer closes the sheet when it lands.
 */
export function askCloseRelayGroupSheet(): void {
  if (!sheet || sheet.state.saving) return;
  const ask = sheet.askClose;
  if (ask) {
    ask();
    return;
  }
  closeRelayGroupSheet();
}

/**
 * The sheet registers the question its Close has to ask, because only the
 * panel knows whether the draft in its fields would be lost.
 */
export function setRelayGroupSheetCloseQuestion(ask?: () => void): void {
  if (sheet) sheet.askClose = ask;
}
