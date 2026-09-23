import React from "react";
import { Platform, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import AppKitCodeWebView from "../macos/NativeCodeWebViewNativeComponent";
import WinUICodeWebView from "../windows/NativeCodeWebViewNativeComponent";
import { CODE_EDITOR_WEB_BUNDLE } from "./CodeEditorWebBundle";

export type CodeEditorLanguage = "json" | "toml" | "yaml" | "text";

/**
 * Localized labels for the editor's in-page context menu. The WebView cannot
 * read the app's translations, so the shared UI passes them in the page
 * bootstrap; a missing label falls back to English inside the page.
 */
export type CodeEditorMenuLabels = {
  cut: string;
  copy: string;
  paste: string;
  selectAll: string;
  undo: string;
  redo: string;
};

export function editorMenuLabels(translate: (key: string) => string): CodeEditorMenuLabels {
  return {
    cut: translate("common.cut"),
    copy: translate("common.copy"),
    paste: translate("common.paste"),
    selectAll: translate("common.selectAll"),
    undo: translate("common.undo"),
    redo: translate("common.redo"),
  };
}

export type CodeEditorDiff = {
  added: number;
  changed: number;
  deleted: number;
};

type InitialEditorCommand = {
  type: "replace";
  documentKey: string;
  value: string;
  baseline: string;
  language: CodeEditorLanguage;
  readOnly: boolean;
  showDiff: boolean;
};

function inlineMenuLabels(labels: CodeEditorMenuLabels | undefined): string {
  if (!labels) return "";
  const serialized = JSON.stringify(labels)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026");
  return `<script>window.LiteLLMCodeEditorMenuLabels = ${serialized};</script>`;
}

function inlineCommand(command: InitialEditorCommand | undefined): string {
  if (!command) return "";
  const serialized = JSON.stringify(command)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026");
  return `<script>window.LiteLLMCodeEditorInitialCommand = ${serialized};</script>`;
}

function codeEditorHtml(command?: InitialEditorCommand, labels?: CodeEditorMenuLabels): string {
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
    <style>
      :root {
        color-scheme: light dark;
        --editor-bg: #ffffff;
        --editor-fg: #202124;
        --editor-gutter: #f8fafc;
        --editor-gutter-fg: #6b7280;
        --editor-border: #e5e7eb;
        --editor-active: #f4f8ff;
        --editor-active-gutter: #eef4ff;
        --editor-selection: #a9c7ff;
        --editor-caret: #2563eb;
        --editor-match: #dbeafe;
        --editor-match-border: #93c5fd;
        --editor-scrollbar-track: #e2e8f0;
        --editor-scrollbar-thumb: #94a3b8;
        --editor-scrollbar-thumb-hover: #64748b;
        --editor-menu-bg: #ffffff;
        --diff-sidebar-bg: #f8fafc;
        --diff-sidebar-header: #f1f5f9;
        --diff-sidebar-card: #ffffff;
        --diff-sidebar-fg: #475569;
        --diff-sidebar-border: #d8dee8;
        --diff-sidebar-added: #16803c;
        --diff-sidebar-added-bg: #f0fdf4;
        --diff-sidebar-changed: #a16207;
        --diff-sidebar-changed-bg: #fffbeb;
        --diff-sidebar-deleted: #c2413a;
        --diff-sidebar-deleted-bg: #fef2f2;
      }
      @media (prefers-color-scheme: dark) {
        :root {
          --editor-bg: #1e1e1e;
          --editor-fg: #e5e7eb;
          --editor-gutter: #252526;
          --editor-gutter-fg: #9ca3af;
          --editor-border: #3f3f46;
          --editor-active: #253047;
          --editor-active-gutter: #303a4f;
          --editor-selection: #3b6ea8;
          --editor-caret: #93c5fd;
          --editor-match: #334155;
          --editor-match-border: #64748b;
          --editor-scrollbar-track: #34343a;
          --editor-scrollbar-thumb: #737b88;
          --editor-scrollbar-thumb-hover: #9aa3b2;
          --editor-menu-bg: #2d2d30;
          --diff-sidebar-bg: #252526;
          --diff-sidebar-header: #2d2d30;
          --diff-sidebar-card: #1e1e1e;
          --diff-sidebar-fg: #cbd5e1;
          --diff-sidebar-border: #3f3f46;
          --diff-sidebar-added: #86efac;
          --diff-sidebar-added-bg: #173522;
          --diff-sidebar-changed: #fde68a;
          --diff-sidebar-changed-bg: #3d3215;
          --diff-sidebar-deleted: #fca5a5;
          --diff-sidebar-deleted-bg: #431f24;
        }
      }
      html, body, #code-editor-layout {
        width: 100%;
        height: 100%;
        margin: 0;
        overflow: hidden;
        background: var(--editor-bg);
      }
      #code-editor-layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        box-sizing: border-box;
        border: 1px solid var(--editor-border);
      }
      body.diff-sidebar-enabled #code-editor-layout {
        grid-template-columns: minmax(0, 1fr) clamp(132px, 28vw, 184px);
      }
      #editor {
        position: relative;
        width: 100%;
        height: 100%;
        min-width: 0;
        overflow: hidden;
      }
      #editor #ace-editor {
        position: absolute;
        inset: 0;
        overflow: hidden;
      }
      #editor .ace_scroller {
        scrollbar-width: none;
      }
      #editor .ace_scroller::-webkit-scrollbar {
        width: 0;
        height: 0;
      }
      #editor .ace_scrollbar,
      #editor .ace_sb-v,
      #editor .ace_sb-h { display: none !important; }
      #editor .ace_editor {
        position: absolute;
        inset: 0;
        font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        color: var(--editor-fg);
        background: var(--editor-bg);
      }
      #editor .ace_gutter {
        color: var(--editor-gutter-fg);
        background: var(--editor-gutter);
        border-right: 1px solid var(--editor-border);
      }
      #editor .ace_active-line { background: var(--editor-active); }
      #editor .ace_gutter-active-line { background: var(--editor-active-gutter); color: var(--editor-fg); }
      #editor .ace_marker-layer .ace_selection { background: var(--editor-selection); }
      #editor .ace_cursor { color: var(--editor-caret); }
      #editor .ace_fold-widget { color: var(--editor-gutter-fg); }
      #editor-scrollbar {
        position: absolute;
        z-index: 4;
        top: 3px;
        right: 2px;
        bottom: 3px;
        width: 11px;
        border-radius: 6px;
        background: var(--editor-scrollbar-track);
        cursor: pointer;
      }
      #editor-scrollbar[hidden] { display: none; }
      #editor-menu {
        position: absolute;
        z-index: 8;
        min-width: 136px;
        padding: 4px;
        border: 1px solid var(--editor-border);
        border-radius: 6px;
        background: var(--editor-menu-bg);
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18);
        font: 12px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      }
      #editor-menu[hidden] { display: none; }
      .editor-menu-item {
        display: block;
        width: 100%;
        box-sizing: border-box;
        padding: 4px 10px;
        border: 0;
        border-radius: 4px;
        background: transparent;
        color: var(--editor-fg);
        font: inherit;
        text-align: left;
        cursor: default;
      }
      .editor-menu-item:hover:not(:disabled) { background: var(--editor-match); }
      .editor-menu-item:disabled { color: var(--editor-gutter-fg); }
      .editor-menu-separator {
        height: 1px;
        margin: 4px 6px;
        background: var(--editor-border);
      }
      #editor-scrollbar-thumb {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        box-sizing: border-box;
        border: 2px solid var(--editor-scrollbar-track);
        border-radius: 6px;
        background: var(--editor-scrollbar-thumb);
        cursor: ns-resize;
      }
      #editor-scrollbar:hover #editor-scrollbar-thumb {
        background: var(--editor-scrollbar-thumb-hover);
      }
      #diff-sidebar {
        display: none;
        min-width: 0;
        overflow: hidden;
        border-left: 1px solid var(--diff-sidebar-border);
        color: var(--diff-sidebar-fg);
        background: var(--diff-sidebar-bg);
        font: 11px/14px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        user-select: none;
        -webkit-user-select: none;
      }
      body.diff-sidebar-enabled #diff-sidebar {
        display: flex;
        flex-direction: column;
      }
      #diff-sidebar-header {
        min-height: 25px;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 6px;
        padding: 0 7px;
        border-bottom: 1px solid var(--diff-sidebar-border);
        background: var(--diff-sidebar-header);
        font-weight: 600;
      }
      #diff-sidebar-total {
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        font-weight: 500;
      }
      #diff-sidebar-list {
        flex: 1;
        min-height: 0;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 6px;
      }
      #diff-sidebar-list:empty::after {
        content: "—";
        margin: auto;
        color: var(--editor-gutter-fg);
      }
      .diff-sidebar-item {
        flex-shrink: 0;
        overflow: hidden;
        border: 1px solid var(--diff-sidebar-border);
        border-left-width: 3px;
        border-radius: 4px;
        background: var(--diff-sidebar-card);
      }
      .diff-sidebar-item-added { border-left-color: var(--diff-sidebar-added); }
      .diff-sidebar-item-changed { border-left-color: var(--diff-sidebar-changed); }
      .diff-sidebar-item-deleted { border-left-color: var(--diff-sidebar-deleted); }
      .diff-sidebar-item-heading {
        min-height: 22px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 5px;
        padding: 0 5px;
        border-bottom: 1px solid var(--diff-sidebar-border);
      }
      .diff-sidebar-item-count { font-weight: 700; }
      .diff-sidebar-item-added .diff-sidebar-item-count { color: var(--diff-sidebar-added); }
      .diff-sidebar-item-changed .diff-sidebar-item-count { color: var(--diff-sidebar-changed); }
      .diff-sidebar-item-deleted .diff-sidebar-item-count { color: var(--diff-sidebar-deleted); }
      .diff-sidebar-item-range {
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        color: var(--editor-gutter-fg);
      }
      .diff-sidebar-code {
        overflow: hidden;
        padding: 4px 5px;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .diff-sidebar-code + .diff-sidebar-code { border-top: 1px solid var(--diff-sidebar-border); }
      .diff-sidebar-code-before {
        color: var(--diff-sidebar-deleted);
        background: var(--diff-sidebar-deleted-bg);
      }
      .diff-sidebar-code-after {
        color: var(--diff-sidebar-added);
        background: var(--diff-sidebar-added-bg);
      }
      .diff-sidebar-item-changed .diff-sidebar-item-heading {
        background: var(--diff-sidebar-changed-bg);
      }
      .diff-sidebar-code-line { min-width: 0; }
      .diff-sidebar-code-more, .diff-sidebar-more {
        color: var(--editor-gutter-fg);
        text-align: center;
      }
      body { color: var(--editor-fg); }
    </style>
  </head>
  <body>
    <div id="code-editor-layout">
      <div id="editor" tabindex="0" role="textbox" aria-multiline="true">
        <div id="ace-editor"></div>
        <div id="editor-scrollbar" aria-hidden="true" hidden><div id="editor-scrollbar-thumb"></div></div>
        <div id="editor-menu" role="menu" hidden></div>
      </div>
      <aside id="diff-sidebar" aria-hidden="true">
        <div id="diff-sidebar-header"><span>Δ</span><span id="diff-sidebar-total"></span></div>
        <div id="diff-sidebar-list"></div>
      </aside>
    </div>
    ${inlineCommand(command)}
    ${inlineMenuLabels(labels)}
    <script>${CODE_EDITOR_WEB_BUNDLE.replace(/<\/script/gi, "<\\/script")}</script>
  </body>
</html>`;
}

// Building the shell embeds the whole editor bundle, so the localized
// read-only variant is cached per label set instead of per open.
let readOnlyHtmlCache: { key: string; html: string } | undefined;

export function readOnlyCodeEditorHtml(labels: CodeEditorMenuLabels): string {
  const key = JSON.stringify(labels);
  if (!readOnlyHtmlCache || readOnlyHtmlCache.key !== key) {
    readOnlyHtmlCache = { key, html: codeEditorHtml(undefined, labels) };
  }
  return readOnlyHtmlCache.html;
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

const styles = StyleSheet.create({
  frame: { flex: 1, minHeight: 160, overflow: "hidden" },
  webView: { flex: 1, backgroundColor: "transparent" },
});
