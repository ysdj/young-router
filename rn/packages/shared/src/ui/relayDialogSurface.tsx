import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * Where a relay panel's form surfaces are drawn.
 *
 * 新建 API 密钥 is asked by a panel that lives inside the provider inspector: a
 * 290 pt column whose editor is a scroll view.  A surface drawn where its panel
 * sits is therefore clipped by that view and centred in the column instead of
 * over the surface that asked it.
 *
 * Moving a surface out of its own clip needs a portal and this build renders
 * none, so a panel hands it *up* to the route instead: it publishes a render
 * function here and `RelayDialogHost` draws the result beside the panels,
 * outside every column.  The codebase already carries this shape of module-level
 * state for the same reason — see `providerWizardGate`.
 *
 * Only placement moves.  This module holds no question and no confirmation: a
 * confirm is the host's own decision panel (`native.showConfirmation`), which is
 * presented by the host and not drawn in this page at all.
 */
type DialogRender = () => React.ReactNode;

const renders = new Map<string, DialogRender>();
const listeners = new Set<() => void>();
let nextId = 0;
let hostCount = 0;

function notify(): void {
  for (const listener of [...listeners]) listener();
}

/** Whether any route currently draws the surface. */
export function relayDialogSurfaceMounted(): boolean {
  return hostCount > 0;
}

/**
 * Publish one panel's dialogs to the route's dialog surface.
 *
 * Returns whether the surface is up, in which case the panel draws nothing itself
 * — drawing both would stack two scrims and two confirm buttons over one decision.
 * A route that mounts no host keeps the panel drawing its own dialogs, so
 * publishing can never make a question disappear.
 *
 * The render is republished after every render of the panel, because that closure
 * is what carries the panel's current state: a stale one would draw a question
 * that no longer matches the rows behind it.  The host is a sibling of the panel
 * rather than its parent, so republishing re-renders the host alone.
 */
export function useRelayDialogSurface(render: DialogRender): boolean {
  const idRef = useRef("");
  if (!idRef.current) idRef.current = `relay-dialogs-${(nextId += 1)}`;
  const id = idRef.current;
  const renderRef = useRef(render);
  renderRef.current = render;
  const [hosted, setHosted] = useState(relayDialogSurfaceMounted);
  useLayoutEffect(() => {
    renders.set(id, () => renderRef.current());
    notify();
    return () => {
      renders.delete(id);
      notify();
    };
  }, [id]);
  // The published entry is the panel's *current* render, so the host has to be
  // told after every render of the panel and not only when it mounts: a form
  // that opened later republished its closure and nothing redrew the host, so
  // the surface stayed closed until some unrelated render of the route happened
  // to draw it — a ＋ that did nothing until the next station read landed.
  // The entry itself never changes (`renderRef` is read at call time), so the
  // mount effect above owns it and this one only says "read it again".
  useLayoutEffect(() => {
    notify();
  });
  useEffect(() => subscribeRelayDialogs(() => setHosted(relayDialogSurfaceMounted())), []);
  return hosted;
}

/**
 * The route's one dialog surface: every published dialog draws here, over the
 * route that owns the panel which asked, and never inside that panel's column.
 */
export function RelayDialogHost(): React.JSX.Element | null {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    hostCount += 1;
    notify();
    return () => {
      hostCount -= 1;
      notify();
    };
  }, []);
  useEffect(() => subscribeRelayDialogs(() => setRevision((value) => value + 1)), []);
  const active = [...renders.values()];
  if (active.length === 0) return null;
  return <React.Fragment key={`relay-dialogs-${revision}`}>
    {active.map((entry, index) => <React.Fragment key={index}>{entry()}</React.Fragment>)}
  </React.Fragment>;
}

/** Subscribe to the published dialogs; returns the unsubscribe. */
export function subscribeRelayDialogs(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
