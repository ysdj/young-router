import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

/**
 * What one window shows when its render threw.
 *
 * A render exception used to leave `DesktopHost`'s root with no live child, and
 * the throw travelled out of the React TurboModule call as a C++ exception:
 * `ObjCTurboModule::performVoidMethodInvocation` rethrows it, `std::terminate`
 * runs, and the whole app aborts.  A pane that read a `const` before its
 * declaration was therefore not "one blank pane": it quit the app on click, and
 * every other window went with it.
 *
 * React's answer to that is a class component with `componentDidCatch`, which is
 * the only place a render error can be stopped.  The window stays up, states
 * what happened, and offers the one thing the user can do about it — the pane
 * rebuilds from a fresh mount on the next render attempt.
 *
 * The message is the error's own text, never a stack: this is a report about
 * what the window could not draw, and a stack is a source fact, not a user's.
 */
type RenderFaultState = { error?: Error };

export class RenderFaultBoundary extends React.Component<{ children: React.ReactNode }, RenderFaultState> {
  state: RenderFaultState = {};

  static getDerivedStateFromError(error: unknown): RenderFaultState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error): void {
    // Nothing here may throw: this is the handler of last resort, and a second
    // throw inside it aborts the process the boundary exists to protect.
    try {
      console.error("[young-router] a window could not be drawn:", error?.message ?? error);
    } catch {
      // A console that cannot write is not a reason to lose the window.
    }
  }

  render(): React.ReactNode {
    if (!this.state.error) return this.props.children;
    return <ScrollView style={styles.pane} contentContainerStyle={styles.content}>
      <Text style={styles.title}>这一页暂时无法显示</Text>
      <Text style={styles.detail}>{this.state.error.message}</Text>
    </ScrollView>;
  }
}

const styles = StyleSheet.create({
  pane: { flex: 1, minWidth: 0, minHeight: 0 },
  content: { padding: 16, gap: 8 },
  title: { fontSize: UI_FONT_SIZE + 2, fontWeight: "600" },
  detail: { fontSize: UI_TIP_FONT_SIZE, lineHeight: 18, opacity: 0.7 },
});
