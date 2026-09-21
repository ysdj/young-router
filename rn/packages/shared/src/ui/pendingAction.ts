import { useCallback, useState } from "react";

/**
 * Tracks which of a surface's own actions is still running, so the button that
 * started it can show its inline spinner (`NativeButton`'s `busy` prop) while
 * its siblings stay disabled.
 *
 * A button that reports progress must stay pressable, so every action handed to
 * `run` still needs its own re-entry guard when a second press would repeat
 * work — the pending id is exactly what that guard reads.
 */
export function usePendingAction<Id extends string>(): [Id | undefined, (id: Id, action: () => Promise<unknown>) => Promise<unknown>] {
  const [pending, setPending] = useState<Id>();
  const run = useCallback((id: Id, action: () => Promise<unknown>): Promise<unknown> => {
    setPending(id);
    return action().finally(() => {
      setPending((current) => (current === id ? undefined : current));
    });
  }, []);
  return [pending, run];
}
