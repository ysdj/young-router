import React from "react";
import { Platform, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import AppKitCodeWebView from "../macos/NativeCodeWebViewNativeComponent";
import WinUICodeWebView from "../windows/NativeCodeWebViewNativeComponent";
import {
  codeEditorPageHtml,
  editorMenuLabels,
  readOnlyCodeEditorHtml as readOnlyPageHtml,
  type CodeEditorDiff,
  type CodeEditorLanguage,
  type CodeEditorMenuLabels,
  type EditorPlatform,
} from "./codeEditorPage";

export { editorMenuLabels };
export type { CodeEditorDiff, CodeEditorLanguage, CodeEditorMenuLabels };

const HOST_PLATFORM: EditorPlatform = Platform.OS === "windows" ? "windows" : Platform.OS === "web" ? "web" : "macos";

type InitialEditorCommand = {
  type: "replace";
  documentKey: string;
  value: string;
  baseline: string;
  language: CodeEditorLanguage;
  readOnly: boolean;
  showDiff: boolean;
};

/** The page one host embeds, ready to hand to that host's WebView. */
function codeEditorHtml(command: InitialEditorCommand, labels: CodeEditorMenuLabels | undefined): string {
  return codeEditorPageHtml({ platform: HOST_PLATFORM, command, labels });
}

/**
 * The read-only variant of the same page; the native hosts open it in their own
 * viewer window. The browser host draws that record with the shared viewer
 * instead, so it never asks for this string.
 */
export function readOnlyCodeEditorHtml(labels: CodeEditorMenuLabels): string {
  return readOnlyPageHtml(labels, HOST_PLATFORM);
}

export function CodeEditorWebView({
  documentKey,
  value,
  baseline,
  language,
  readOnly = false,
  showDiff = false,
  style,
  menuLabels,
  onChange,
  onError,
}: {
  documentKey: string;
  value: string;
  baseline: string;
  language: CodeEditorLanguage;
  readOnly?: boolean;
  showDiff?: boolean;
  style?: StyleProp<ViewStyle>;
  menuLabels?: CodeEditorMenuLabels;
  onChange?: (text: string, diff?: CodeEditorDiff) => void;
  onError?: () => void;
}): React.JSX.Element {
  if (Platform.OS === "web") {
    return <WebCodeEditor
      documentKey={documentKey}
      value={value}
      baseline={baseline}
      language={language}
      readOnly={readOnly}
      showDiff={showDiff}
      style={style}
      menuLabels={menuLabels}
      onChange={onChange}
      onError={onError}
    />;
  }
  const NativeCodeWebView = Platform.OS === "windows" ? WinUICodeWebView : AppKitCodeWebView;
  // Keep the page itself alive while React sends new documents through the
  // native props.  Rebuilding the HTML for every document-key change reloads
  // the WebView, which is visible as a flash and also discards editor state.
  // Each editor component is mounted for one document, so its first command
  // is the only command that belongs in the inline page bootstrap.
  const [initialHtml] = React.useState(() => codeEditorHtml({
    type: "replace",
    documentKey,
    value,
    baseline,
    language,
    readOnly,
    showDiff,
  }, menuLabels));
  return <View style={[styles.frame, style]}>
    <NativeCodeWebView
      html={initialHtml}
      documentKey={documentKey}
      value={value}
      baseline={baseline}
      language={language}
      readOnly={readOnly}
      showDiff={showDiff}
      onEditorChange={({ nativeEvent }) => onChange?.(nativeEvent.text, {
        added: nativeEvent.added,
        changed: nativeEvent.changed,
        deleted: nativeEvent.deleted,
      })}
      onEditorError={() => onError?.()}
      style={styles.webView}
    />
  </View>;
}

/**
 * The same editor page, framed.
 *
 * The page is the one the native hosts embed; here Core serves it as its own
 * same-origin document (`/ui/editor.html`) and this component frames it. A
 * frame is what makes it possible at all: this app's own document runs under
 * `script-src 'self'`, so the page's inline Ace bundle would be refused there,
 * and a `srcdoc` or `blob:` frame inherits that same policy. A document the
 * server answers with is a document with its own policy, and Core sends it with
 * `X-Frame-Options: SAMEORIGIN` so only this origin may frame it.
 *
 * Two messages, and no shared JavaScript: the page posts `ready`, `change`,
 * `error` and `paste` as JSON strings; this component posts the same `replace`
 * commands a native host pushes into its WebView. Both directions check the
 * sender (this frame's `contentWindow`) and the origin, so no other window can
 * drive an open document.
 */
function WebCodeEditor({
  documentKey,
  value,
  baseline,
  language,
  readOnly = false,
  showDiff = false,
  style,
  menuLabels,
  onChange,
  onError,
}: {
  documentKey: string;
  value: string;
  baseline: string;
  language: CodeEditorLanguage;
  readOnly?: boolean;
  showDiff?: boolean;
  style?: StyleProp<ViewStyle>;
  menuLabels?: CodeEditorMenuLabels;
  onChange?: (text: string, diff?: CodeEditorDiff) => void;
  onError?: () => void;
}): React.JSX.Element {
  const frameRef = React.useRef<HTMLIFrameElement | null>(null);
  const [ready, setReady] = React.useState(false);
  const onChangeRef = React.useRef(onChange);
  const onErrorRef = React.useRef(onError);
  const labelsRef = React.useRef(menuLabels);
  onChangeRef.current = onChange;
  onErrorRef.current = onError;
  labelsRef.current = menuLabels;

  const post = React.useCallback((message: unknown): void => {
    const contentWindow = frameRef.current?.contentWindow;
    if (!contentWindow) return;
    contentWindow.postMessage(JSON.stringify(message), window.location.origin);
  }, []);

  // The page asks for the system pasteboard through its own menu; a browser
  // grants that read only to a document the person is using, so this side asks
  // and hands the text back. A refused read is not an error the editor can act
  // on — the keyboard's own paste still works inside the frame.
  const pasteFromClipboard = React.useCallback((): void => {
    void navigator.clipboard?.readText?.().then((text) => {
      if (text) post({ type: "insertText", text });
    }).catch(() => undefined);
  }, [post]);

  React.useEffect(() => {
    const onMessage = (event: MessageEvent): void => {
      const frame = frameRef.current;
      if (!frame || !frame.contentWindow || event.source !== frame.contentWindow) return;
      if (event.origin !== window.location.origin) return;
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      if (payload.type === "ready") {
        // The page is running: hand it the labels this window is drawn in and
        // then the document, in that order, so its first menu is localized.
        if (labelsRef.current) post({ type: "labels", labels: labelsRef.current });
        setReady(true);
      } else if (payload.type === "change" && typeof payload.text === "string") {
        onChangeRef.current?.(payload.text, {
          added: Number(payload.added) || 0,
          changed: Number(payload.changed) || 0,
          deleted: Number(payload.deleted) || 0,
        });
      } else if (payload.type === "error") {
        onErrorRef.current?.();
      } else if (payload.type === "paste") {
        pasteFromClipboard();
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [pasteFromClipboard, post]);

  // Every prop change is one `replace`, exactly what a native host pushes
  // through its WebView props. The page compares the text before applying it,
  // so echoing what the user just typed is a no-op rather than a caret jump.
  React.useEffect(() => {
    if (!ready) return;
    post({
      type: "replace",
      documentKey,
      value,
      baseline,
      language,
      readOnly,
      showDiff,
    });
  }, [ready, documentKey, value, baseline, language, readOnly, showDiff, post]);

  React.useEffect(() => {
    if (ready && menuLabels) post({ type: "labels", labels: menuLabels });
  }, [ready, menuLabels, post]);

  return <View style={[styles.frame, style]}>
    {/* An iframe is a DOM element, not a React Native one: this branch only
        ever runs in the browser bundle, where the tree is rendered by
        react-dom. */}
    {React.createElement("iframe", {
      ref: frameRef,
      title: documentKey,
      src: "./editor.html",
      // A frame that finished loading has its message listener installed, so
      // this is the one point that cannot race the page's own `ready` — a
      // cached page can finish before this component's effect has run.
      onLoad: () => setReady(true),
      style: { border: 0, width: "100%", height: "100%", display: "block", backgroundColor: "transparent" },
    })}
  </View>;
}

const styles = StyleSheet.create({
  frame: { flex: 1, minHeight: 160, overflow: "hidden" },
  webView: { flex: 1, backgroundColor: "transparent" },
});
