import React from "react";
import { Pressable, ScrollView, StyleSheet, Text } from "react-native";
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

/**
 * `translate` comes from the host's own dependencies, so a window that failed
 * to draw still speaks the language the menu bar is set to.  The boundary used
 * to carry its title as a Chinese literal, which left an English-language app
 * reporting its one unrecoverable state in the wrong language.
 */
type RenderFaultProps = { children: React.ReactNode; translate?: (key: string) => string };

export class RenderFaultBoundary extends React.Component<RenderFaultProps, RenderFaultState> {
  state: RenderFaultState = {};
  /** Bumped by the retry control; it re-keys the subtree to force a fresh mount. */
  private attempt = 0;

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

  /**
   * Clear the fault and re-mount the subtree.
   *
   * The state is the boundary's own, so nothing else can clear it: a window
   * that threw once stayed on the fault screen for the life of the process
   * even after the cause was gone, while this component's own docstring
   * promised the pane would rebuild on the next render attempt.  Re-keying the
   * children is what makes that attempt a *fresh* mount rather than a retry of
   * the same component instance that holds the bad state.
   */
  private readonly retry = (): void => {
    this.attempt += 1;
    this.setState({ error: undefined });
  };

  render(): React.ReactNode {
    if (!this.state.error) {
      return <React.Fragment key={this.attempt}>{this.props.children}</React.Fragment>;
    }
    // The boundary draws even when the translator itself is what failed, so a
    // missing or throwing `translate` falls back to the key's English text
    // rather than becoming a second render fault.
    const translate = (key: string, fallback: string): string => {
      try {
        return this.props.translate?.(key) || fallback;
      } catch {
        // A translator that cannot answer is not a reason to lose the window.
        return fallback;
      }
    };
    return <ScrollView style={styles.pane} contentContainerStyle={styles.content}>
      <Text style={styles.title}>{translate("error.renderFaultTitle", "This page can't be displayed right now")}</Text>
      <Text style={styles.detail}>{this.state.error.message}</Text>
      <Pressable accessibilityRole="button" onPress={this.retry} style={styles.retry}>
        <Text style={styles.retryText}>{translate("error.renderFaultRetry", "Try again")}</Text>
      </Pressable>
    </ScrollView>;
  }
}

const styles = StyleSheet.create({
  pane: { flex: 1, minWidth: 0, minHeight: 0 },
  content: { padding: 16, gap: 8 },
  title: { fontSize: UI_FONT_SIZE + 2, fontWeight: "600" },
  detail: { fontSize: UI_TIP_FONT_SIZE, lineHeight: 18, opacity: 0.7 },
  // A quiet link under the cause, not a button: the fault screen is a report,
  // and this is the one action that belongs on it.
  retry: { alignSelf: "flex-start", paddingVertical: 4 },
  retryText: { fontSize: UI_FONT_SIZE, color: "#0a84ff" },
});
