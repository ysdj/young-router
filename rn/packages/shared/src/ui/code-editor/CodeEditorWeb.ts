/// <reference lib="dom" />

import { Compartment, EditorState, Text } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { defaultHighlightStyle, indentOnInput, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { presentableDiff } from "@codemirror/merge";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { json } from "@codemirror/lang-json";

type EditorLanguage = "json" | "toml" | "text";

type EditorDiff = {
  added: number;
  changed: number;
  deleted: number;
};

type EditorDiffPreview = {
  lines: string[];
  truncated: boolean;
};

type EditorDiffEntry = EditorDiff & {
  kind: "added" | "changed" | "deleted";
  range: string;
  before: EditorDiffPreview;
  after: EditorDiffPreview;
};

type ComputedEditorDiff = EditorDiff & {
  entries: EditorDiffEntry[];
};

type ReplaceDocumentCommand = {
  type: "replace";
  documentKey: string;
  value: string;
  baseline: string;
  language: EditorLanguage;
  readOnly: boolean;
  showDiff: boolean;
};

type SetBaselineCommand = {
  type: "setBaseline";
  baseline: string;
};

type HostCommand = ReplaceDocumentCommand | SetBaselineCommand | { type: "focus" };

declare global {
  interface Window {
    LiteLLMCodeEditorInitialCommand?: unknown;
    LiteLLMCodeEditor?: {
      receive: (message: unknown) => void;
    };
    webkit?: {
      messageHandlers?: {
        litellmCodeEditor?: {
          postMessage: (message: string) => void;
        };
      };
    };
    chrome?: {
      webview?: {
        postMessage: (message: string) => void;
      };
    };
  }
}

const editorHost = document.getElementById("editor");
const editorScrollbar = document.getElementById("editor-scrollbar");
const editorScrollbarThumb = document.getElementById("editor-scrollbar-thumb");
const diffSidebar = document.getElementById("diff-sidebar");
const diffSidebarTotal = document.getElementById("diff-sidebar-total");
const diffSidebarList = document.getElementById("diff-sidebar-list");
if (!editorHost || !editorScrollbar || !editorScrollbarThumb || !diffSidebar || !diffSidebarTotal || !diffSidebarList) {
  throw new Error("Code editor shell is missing.");
}
const host: HTMLElement = editorHost;
const scrollTrack: HTMLElement = editorScrollbar;
const scrollThumb: HTMLElement = editorScrollbarThumb;
const sidebarTotal: HTMLElement = diffSidebarTotal;
const sidebarList: HTMLElement = diffSidebarList;
// The native host can paint the document before WebKit has installed
// CodeMirror's contenteditable node.  Keep the outer host keyboard-focusable
// and forward the first pointer press to the editor so an early click never
// gets lost during that hand-off.
host.tabIndex = 0;
host.setAttribute("role", "textbox");
host.setAttribute("aria-multiline", "true");
host.addEventListener("pointerdown", () => editor?.focus(), true);
host.addEventListener("mousedown", () => editor?.focus(), true);

let editor: EditorView | undefined;
let activeDocumentKey = "";
let showingDiff = false;
let baselineText = "";
let changeTimer: number | undefined;
let diffTimer: number | undefined;
let scrollbarFrame: number | undefined;
let scrollbarObserver: ResizeObserver | undefined;
let scrollbarDrag: { pointerId: number; grabOffset: number } | undefined;
let latestDiff: EditorDiff = { added: 0, changed: 0, deleted: 0 };
let applyingHostUpdate = false;

const languageConfiguration = new Compartment();
const editabilityConfiguration = new Compartment();

const DIFF_CONFIG = { scanLimit: 2_000, timeout: 16 } as const;
const DIFF_PREVIEW_LINE_LIMIT = 4;
const DIFF_SIDEBAR_ENTRY_LIMIT = 24;
const CHANGE_DEBOUNCE_MS = 40;
const DIFF_DEBOUNCE_MS = 120;

// Keep the embedded editor deliberately small. `basicSetup` also pulls in
// autocomplete, lint, folding, search panels, and other extensions that these
// compact configuration panes do not use. Parsing those modules in every
// system WebView was the largest part of the editor's cold-start cost.
const editorSetup = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightSpecialChars(),
  history(),
  drawSelection(),
  EditorState.allowMultipleSelections.of(true),
  indentOnInput(),
  syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
  highlightActiveLine(),
  EditorView.lineWrapping,
  keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap]),
];

const editorTheme = EditorView.theme({
  "&": {
    height: "100%",
    color: "var(--editor-fg)",
    backgroundColor: "var(--editor-bg)",
    fontSize: "12px",
  },
  ".cm-scroller": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    lineHeight: "1.55",
    overflowX: "auto",
    overflowY: "auto",
  },
  ".cm-content": {
    minHeight: "100%",
    padding: "8px 14px 20px 0",
    caretColor: "var(--editor-caret)",
    userSelect: "text",
    WebkitUserSelect: "text",
  },
  ".cm-line": {
    userSelect: "text",
    WebkitUserSelect: "text",
  },
  ".cm-gutters": {
    minWidth: "44px",
    color: "var(--editor-gutter-fg)",
    backgroundColor: "var(--editor-gutter)",
    borderRight: "1px solid var(--editor-border)",
  },
  ".cm-lineNumbers .cm-gutterElement": {
    minWidth: "30px",
    padding: "0 8px 0 4px",
  },
  ".cm-activeLine": {
    backgroundColor: "var(--editor-active)",
  },
  ".cm-activeLineGutter": {
    color: "var(--editor-fg)",
    backgroundColor: "var(--editor-active-gutter)",
  },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--editor-selection)",
  },
  ".cm-cursor, .cm-dropCursor": {
    borderLeftColor: "var(--editor-caret)",
  },
  ".cm-matchingBracket": {
    color: "var(--editor-fg)",
    backgroundColor: "var(--editor-match)",
    outline: "1px solid var(--editor-match-border)",
  },
});

function renderEditorScrollbar(): void {
  if (!editor) return;
  const viewport = editor.scrollDOM;
  const maximumScrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  if (maximumScrollTop <= 0) {
    scrollTrack.hidden = true;
    return;
  }
  scrollTrack.hidden = false;
  const trackHeight = scrollTrack.clientHeight;
  const thumbHeight = Math.max(24, Math.min(trackHeight, Math.round(trackHeight * viewport.clientHeight / viewport.scrollHeight)));
  const maximumThumbTop = Math.max(0, trackHeight - thumbHeight);
  const thumbTop = maximumThumbTop === 0 ? 0 : Math.round(maximumThumbTop * viewport.scrollTop / maximumScrollTop);
  scrollThumb.style.height = `${thumbHeight}px`;
  scrollThumb.style.transform = `translateY(${thumbTop}px)`;
}

function queueEditorScrollbar(): void {
  if (scrollbarFrame !== undefined) return;
  scrollbarFrame = window.requestAnimationFrame(() => {
    scrollbarFrame = undefined;
    renderEditorScrollbar();
  });
}

function scrollEditorFromPointer(clientY: number, grabOffset: number): void {
  if (!editor) return;
  const viewport = editor.scrollDOM;
  const trackBounds = scrollTrack.getBoundingClientRect();
  const thumbHeight = scrollThumb.getBoundingClientRect().height;
  const maximumThumbTop = Math.max(0, trackBounds.height - thumbHeight);
  const maximumScrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  if (maximumThumbTop <= 0 || maximumScrollTop <= 0) return;
  const thumbTop = Math.max(0, Math.min(maximumThumbTop, clientY - trackBounds.top - grabOffset));
  viewport.scrollTop = maximumScrollTop * thumbTop / maximumThumbTop;
}

function installEditorScrollbar(): void {
  if (!editor || scrollbarObserver) return;
  editor.scrollDOM.addEventListener("scroll", queueEditorScrollbar, { passive: true });
  scrollbarObserver = new ResizeObserver(queueEditorScrollbar);
  scrollbarObserver.observe(editor.scrollDOM);
  scrollbarObserver.observe(editor.contentDOM);
  window.addEventListener("resize", queueEditorScrollbar);
  queueEditorScrollbar();
}

scrollTrack.addEventListener("pointerdown", (event) => {
  if (!editor || event.button !== 0 || scrollTrack.hidden) return;
  event.preventDefault();
  editor.focus();
  const thumbBounds = scrollThumb.getBoundingClientRect();
  const grabOffset = event.target === scrollThumb
    ? event.clientY - thumbBounds.top
    : thumbBounds.height / 2;
  scrollbarDrag = { pointerId: event.pointerId, grabOffset };
  scrollTrack.setPointerCapture(event.pointerId);
  scrollEditorFromPointer(event.clientY, grabOffset);
});

scrollTrack.addEventListener("pointermove", (event) => {
  if (!scrollbarDrag || scrollbarDrag.pointerId !== event.pointerId) return;
  event.preventDefault();
  scrollEditorFromPointer(event.clientY, scrollbarDrag.grabOffset);
});

function finishScrollbarDrag(event: PointerEvent): void {
  if (!scrollbarDrag || scrollbarDrag.pointerId !== event.pointerId) return;
  scrollbarDrag = undefined;
  if (scrollTrack.hasPointerCapture(event.pointerId)) scrollTrack.releasePointerCapture(event.pointerId);
}

scrollTrack.addEventListener("pointerup", finishScrollbarDrag);
scrollTrack.addEventListener("pointercancel", finishScrollbarDrag);

function normalizedLines(text: string): string[] {
  return text.replace(/\r\n?/g, "\n").split("\n");
}

function textDocument(text: string): Text {
  return Text.of(normalizedLines(text));
}

function languageExtension(language: EditorLanguage) {
  if (language === "json") return json();
  if (language === "toml") return StreamLanguage.define(toml);
  return [];
}

function post(message: unknown): void {
  const serialized = JSON.stringify(message);
  const webKitHandler = window.webkit?.messageHandlers?.litellmCodeEditor;
  if (webKitHandler) {
    webKitHandler.postMessage(serialized);
    return;
  }
  window.chrome?.webview?.postMessage(serialized);
}

function linesInRange(document: Text, from: number, to: number): number {
  if (to <= from || document.length === 0) return 0;
  let safeFrom = Math.max(0, Math.min(from, document.length));
  if (safeFrom < to && document.sliceString(safeFrom, safeFrom + 1) === "\n") safeFrom += 1;
  const safeTo = Math.max(safeFrom, Math.min(to - 1, document.length));
  return document.lineAt(safeTo).number - document.lineAt(safeFrom).number + 1;
}

function lineRange(document: Text, from: number, to: number): string {
  let safeFrom = Math.max(0, Math.min(from, document.length));
  if (safeFrom < to && document.sliceString(safeFrom, safeFrom + 1) === "\n") safeFrom += 1;
  const safeTo = to <= from
    ? safeFrom
    : Math.max(safeFrom, Math.min(to - 1, document.length));
  const first = document.lineAt(safeFrom).number;
  const last = document.lineAt(safeTo).number;
  return first === last ? `L${first}` : `L${first}–${last}`;
}

function previewLines(document: Text, from: number, to: number): EditorDiffPreview {
  if (to <= from || document.length === 0) return { lines: [], truncated: false };
  let safeFrom = Math.max(0, Math.min(from, document.length));
  if (safeFrom < to && document.sliceString(safeFrom, safeFrom + 1) === "\n") safeFrom += 1;
  const safeTo = Math.max(safeFrom, Math.min(to - 1, document.length));
  const first = document.lineAt(safeFrom).number;
  const last = document.lineAt(safeTo).number;
  const previewLast = Math.min(last, first + DIFF_PREVIEW_LINE_LIMIT - 1);
  const lines: string[] = [];
  for (let line = first; line <= previewLast; line += 1) lines.push(document.line(line).text);
  return { lines, truncated: previewLast < last };
}

function diffLabel(diff: EditorDiff): string {
  return [
    diff.added > 0 ? `+${diff.added}` : undefined,
    diff.changed > 0 ? `~${diff.changed}` : undefined,
    diff.deleted > 0 ? `−${diff.deleted}` : undefined,
  ].filter((part): part is string => Boolean(part)).join(" ");
}

function computeDiff(): ComputedEditorDiff {
  if (!editor || !showingDiff) return { added: 0, changed: 0, deleted: 0, entries: [] };
  const current = editor.state.doc;
  const original = textDocument(baselineText);
  const changes = presentableDiff(original.toString(), current.toString(), DIFF_CONFIG);
  let added = 0;
  let changed = 0;
  let deleted = 0;
  const entries: EditorDiffEntry[] = [];
  for (const change of changes) {
    const before = linesInRange(original, change.fromA, change.toA);
    const after = linesInRange(current, change.fromB, change.toB);
    const changedLines = Math.min(before, after);
    const addedLines = Math.max(0, after - before);
    const deletedLines = Math.max(0, before - after);
    changed += changedLines;
    added += addedLines;
    deleted += deletedLines;
    entries.push({
      kind: before === 0 ? "added" : after === 0 ? "deleted" : "changed",
      range: after > 0
        ? lineRange(current, change.fromB, change.toB)
        : lineRange(original, change.fromA, change.toA),
      added: addedLines,
      changed: changedLines,
      deleted: deletedLines,
      before: previewLines(original, change.fromA, change.toA),
      after: previewLines(current, change.fromB, change.toB),
    });
  }
  return { added, changed, deleted, entries };
}

function appendDiffPreview(parent: HTMLElement, prefix: string, preview: EditorDiffPreview, className: string): void {
  if (preview.lines.length === 0) return;
  const block = document.createElement("div");
  block.className = `diff-sidebar-code ${className}`;
  for (const text of preview.lines) {
    const line = document.createElement("div");
    line.className = "diff-sidebar-code-line";
    line.textContent = `${prefix} ${text || "↵"}`;
    block.appendChild(line);
  }
  if (preview.truncated) {
    const more = document.createElement("div");
    more.className = "diff-sidebar-code-more";
    more.textContent = "…";
    block.appendChild(more);
  }
  parent.appendChild(block);
}

function renderDiffSidebar(): void {
  const summary = computeDiff();
  latestDiff = summary;
  sidebarTotal.textContent = diffLabel(summary);
  sidebarList.replaceChildren();
  if (!showingDiff) return;
  for (const entry of summary.entries.slice(0, DIFF_SIDEBAR_ENTRY_LIMIT)) {
    const item = document.createElement("section");
    item.className = `diff-sidebar-item diff-sidebar-item-${entry.kind}`;
    item.title = `${diffLabel(entry)} ${entry.range}`.trim();
    const heading = document.createElement("div");
    heading.className = "diff-sidebar-item-heading";
    const count = document.createElement("span");
    count.className = "diff-sidebar-item-count";
    count.textContent = diffLabel(entry);
    const range = document.createElement("span");
    range.className = "diff-sidebar-item-range";
    range.textContent = entry.range;
    heading.append(count, range);
    item.appendChild(heading);
    appendDiffPreview(item, "−", entry.before, "diff-sidebar-code-before");
    appendDiffPreview(item, "+", entry.after, "diff-sidebar-code-after");
    sidebarList.appendChild(item);
  }
  if (summary.entries.length > DIFF_SIDEBAR_ENTRY_LIMIT) {
    const more = document.createElement("div");
    more.className = "diff-sidebar-more";
    more.textContent = "…";
    sidebarList.appendChild(more);
  }
}

function queueDiffSidebar(): void {
  if (!showingDiff) return;
  if (diffTimer !== undefined) window.clearTimeout(diffTimer);
  diffTimer = window.setTimeout(() => {
    diffTimer = undefined;
    renderDiffSidebar();
  }, DIFF_DEBOUNCE_MS);
}

function cancelDiffSidebar(): void {
  if (diffTimer !== undefined) {
    window.clearTimeout(diffTimer);
    diffTimer = undefined;
  }
}

function cancelChange(): void {
  if (changeTimer !== undefined) {
    window.clearTimeout(changeTimer);
    changeTimer = undefined;
  }
}

function reportChange(): void {
  if (!editor || applyingHostUpdate) return;
  queueDiffSidebar();
  cancelChange();
  changeTimer = window.setTimeout(() => {
    changeTimer = undefined;
    if (!editor) return;
    post({
      type: "change",
      text: editor.state.doc.toString(),
      added: latestDiff.added,
      changed: latestDiff.changed,
      deleted: latestDiff.deleted,
    });
  }, CHANGE_DEBOUNCE_MS);
}

function replaceDocument(command: ReplaceDocumentCommand): void {
  cancelChange();
  cancelDiffSidebar();
  showingDiff = command.showDiff;
  document.body.classList.toggle("diff-sidebar-enabled", showingDiff);
  baselineText = textDocument(command.baseline).toString();
  latestDiff = { added: 0, changed: 0, deleted: 0 };
  sidebarTotal.textContent = "";
  sidebarList.replaceChildren();
  const nextDocument = textDocument(command.value);
  if (!editor) {
    editor = new EditorView({
      state: EditorState.create({
        doc: nextDocument,
        extensions: [
          editorSetup,
          languageConfiguration.of(languageExtension(command.language)),
          editabilityConfiguration.of([
            EditorState.readOnly.of(command.readOnly),
            EditorView.editable.of(!command.readOnly),
          ]),
          editorTheme,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) reportChange();
            if (update.docChanged || update.geometryChanged) queueEditorScrollbar();
          }),
        ],
      }),
      parent: host,
    });
    installEditorScrollbar();
  } else {
    const nextText = nextDocument.toString();
    const documentChanged = activeDocumentKey !== command.documentKey;
    const replaceText = editor.state.doc.toString() !== nextText;
    applyingHostUpdate = replaceText;
    try {
      editor.dispatch({
        changes: replaceText ? { from: 0, to: editor.state.doc.length, insert: nextText } : undefined,
        selection: documentChanged ? { anchor: 0 } : undefined,
        effects: [
          languageConfiguration.reconfigure(languageExtension(command.language)),
          editabilityConfiguration.reconfigure([
            EditorState.readOnly.of(command.readOnly),
            EditorView.editable.of(!command.readOnly),
          ]),
        ],
      });
      if (documentChanged) editor.scrollDOM.scrollTo({ left: 0, top: 0 });
    } finally {
      applyingHostUpdate = false;
    }
  }
  activeDocumentKey = command.documentKey;
  queueEditorScrollbar();
  queueDiffSidebar();
}

function setBaseline(baseline: string): void {
  if (!showingDiff) return;
  baselineText = textDocument(baseline).toString();
  latestDiff = { added: 0, changed: 0, deleted: 0 };
  queueDiffSidebar();
}

function receive(input: unknown): void {
  const command = typeof input === "string"
    ? (() => {
      try {
        return JSON.parse(input) as unknown;
      } catch {
        return undefined;
      }
    })()
    : input;
  if (!command || typeof command !== "object") return;
  const message = command as Partial<HostCommand>;
  if (message.type === "replace"
    && typeof message.documentKey === "string"
    && typeof message.value === "string"
    && typeof message.baseline === "string"
    && (message.language === "json" || message.language === "toml" || message.language === "text")
    && typeof message.readOnly === "boolean"
    && typeof message.showDiff === "boolean") {
    replaceDocument(message as ReplaceDocumentCommand);
  } else if (message.type === "setBaseline" && typeof message.baseline === "string") {
    setBaseline(message.baseline);
  } else if (message.type === "focus") {
    editor?.focus();
  }
}

window.LiteLLMCodeEditor = { receive };
const initialCommand = window.LiteLLMCodeEditorInitialCommand;
delete window.LiteLLMCodeEditorInitialCommand;
if (initialCommand !== undefined) receive(initialCommand);
post({ type: "ready", documentKey: activeDocumentKey });
