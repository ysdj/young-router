/**
 * The per-domain half of the snapshot a window already holds.
 *
 * The root component keeps one `CoreSnapshot` in `useState`, and every pane
 * used to redraw whenever any part of it moved: a live log tick re-rendered the
 * provider table, a probe result re-rendered the language picker.  This module
 * adds the layer that lets a component subscribe to the one domain it draws,
 * without changing how the root holds or accepts state.
 *
 * The comparison is one object reference per domain — `domains[domain]` — and
 * never a deep read.  A patched snapshot keeps the reference of every domain
 * the patch did not touch, so only the panes whose domain moved are notified.
 * This is the same pattern `HostChrome` and `RelayGroupSheet` already use; it
 * adds no dependency and no reducer of its own, because the state here is
 * Core's projection, not a client-side graph.
 */

import { useSyncExternalStore } from "react";
import type { ConfigDomain, CoreSnapshot } from "../types";

type Listener = () => void;

/** What a component hands to `useSyncExternalStore` for one domain. */
export type DomainSubscribe = (listener: Listener) => () => void;

/** What the event channel has told this window, and where it stands. */
export type EventChannelState = {
  /** How many events have arrived; zero means no patch is owed yet. */
  received: number;
  /** The revision of the state the root currently holds. */
  revision: number;
};

const listeners = new Map<ConfigDomain, Set<Listener>>();
const subscriptions = new Map<ConfigDomain, DomainSubscribe>();
let current: CoreSnapshot | undefined;

function listenersFor(domain: ConfigDomain): Set<Listener> {
  let set = listeners.get(domain);
  if (!set) {
    set = new Set<Listener>();
    listeners.set(domain, set);
  }
  return set;
}

/**
 * The subscribe function for one domain, cached per domain.
 *
 * `useSyncExternalStore` re-subscribes whenever the function's identity
 * changes, so returning the same function for the same domain keeps that from
 * happening on every render.
 */
export function subscribeDomain(domain: ConfigDomain): DomainSubscribe {
  const cached = subscriptions.get(domain);
  if (cached) return cached;
  const subscribe: DomainSubscribe = (listener) => {
    const set = listenersFor(domain);
    set.add(listener);
    return () => {
      set.delete(listener);
    };
  };
  subscriptions.set(domain, subscribe);
  return subscribe;
}

/** The domain's projection, by reference.  `undefined` before the first state. */
export function getDomain(domain: ConfigDomain): unknown {
  return current?.domains?.[domain];
}

/**
 * Hand the store a snapshot and notify the domains whose projection moved.
 *
 * The root calls this for every snapshot it accepts, whether read in full or
 * built by `applyPatch`.  A caller that passes the object already stored is a
 * no-op, so the effect that publishes the initial snapshot costs nothing after
 * the first time.
 */
export function publishSnapshot(next: CoreSnapshot | undefined): void {
  const previous = current;
  if (previous === next) return;
  // A snapshot older than the state this layer already holds is not a newer
  // projection: it is a render from before an event landed, and publishing it
  // would move every subscribed pane backwards.
  if (previous !== undefined && next !== undefined && next.revision < previous.revision) return;
  current = next;
  if (!next) return;
  for (const [domain, set] of listeners) {
    if (set.size === 0) continue;
    if (previous !== undefined && previous.domains?.[domain] === next.domains?.[domain]) continue;
    for (const listener of Array.from(set)) listener();
  }
}

/**
 * The domain a component draws, with the store's reference notifications.
 *
 * A component that mounts before the first snapshot sees `undefined` and is
 * notified on the first publish, so no pane needs a special first-render path.
 */
export function useDomainRecord(domain: ConfigDomain): Record<string, unknown> | undefined {
  const value = useSyncExternalStore(subscribeDomain(domain), () => getDomain(domain), () => getDomain(domain));
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/**
 * Whether a completed action still owes this window a full read.
 *
 * The event channel is the answer to a moved revision: Core publishes what the
 * action changed, this window applies it, and the pane updates.  Reading the
 * whole projection as well was the third copy of every edit, so an action whose
 * answer is the revision this window holds, that moved the revision, and that
 * arrives on a channel already delivering events needs no read at all.
 *
 * Every other case keeps the read: a channel that has never delivered an event
 * may not be connected, an action that staged nothing (a read, a probe that
 * changed no state) produces no patch, a rebased answer is not the local head,
 * and a window that was already behind before the action has a gap only a full
 * projection closes.
 */
export function postActionRefreshNeeded({ channel, revisionBefore, revisionAfter, localRevision }: {
  channel: EventChannelState;
  revisionBefore: number | undefined;
  revisionAfter: number | undefined;
  localRevision: number | undefined;
}): boolean {
  if (channel.received < 1) return true;
  if (revisionBefore === undefined || revisionAfter === undefined || localRevision === undefined) return true;
  if (revisionAfter !== localRevision) return true;
  if (revisionAfter <= revisionBefore) return true;
  if (channel.revision < revisionBefore) return true;
  return false;
}
