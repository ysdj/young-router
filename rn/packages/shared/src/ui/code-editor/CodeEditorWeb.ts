/// <reference lib="dom" />

// Ace is bundled as a browser-side IIFE. These side-effect imports register
// the editor and the two language modes on window.ace; no worker or loader is
// used, which keeps the strict inline WebView CSP intact.
// @ts-ignore ace-builds has no declaration for source-noconflict entry points.
import "ace-builds/src-noconflict/ace";
// @ts-ignore see the note above.
import "ace-builds/src-noconflict/mode-json";
// @ts-ignore see the note above.
import "ace-builds/src-noconflict/mode-toml";
// @ts-ignore see the note above.
import "ace-builds/src-noconflict/mode-yaml";
// @ts-ignore see the note above.
import "ace-builds/src-noconflict/ext-searchbox";

type EditorLanguage = "json" | "toml" | "yaml" | "text";
/** Localized labels for the editor's own context menu. */
type EditorMenuLabels = { cut: string; copy: string; paste: string; selectAll: string; undo: string; redo: string };
type EditorDiff = { added: number; changed: number; deleted: number };
type EditorDiffPreview = { lines: string[]; truncated: boolean };
type EditorDiffEntry = EditorDiff & {
  kind: "added" | "changed" | "deleted";
  range: string;
  before: EditorDiffPreview;
  after: EditorDiffPreview;
};
type ComputedEditorDiff = EditorDiff & { entries: EditorDiffEntry[] };
type ReplaceDocumentCommand = {
  type: "replace";
  documentKey: string;
  value: string;
  baseline: string;
  language: EditorLanguage;
  readOnly: boolean;
  showDiff: boolean;
};
type SetBaselineCommand = { type: "setBaseline"; baseline: string };
type InsertTextCommand = { type: "insertText"; text: string };
type HostCommand = ReplaceDocumentCommand | SetBaselineCommand | InsertTextCommand | { type: "focus" };

type AceSession = {
  getValue: () => string;
  getScreenLength: () => number;
  setValue: (value: string) => void;
  setMode: (mode: string) => void;
  setUseWorker: (enabled: boolean) => void;
  on: (event: string, listener: () => void) => void;
};
type AceRenderer = {
  scroller?: HTMLElement;
  layerConfig?: { lineHeight?: number; maxHeight?: number };
  getScrollTop: () => number;
  scrollToY: (top: number) => void;
  on: (event: string, listener: () => void) => void;
};
type AceSelection = { clearSelection?: () => void };
type AceEditor = {
  session: AceSession;
  renderer: AceRenderer;
  selection?: AceSelection;
  setValue: (value: string, cursorPosition?: number) => void;
  getValue: () => string;
  getCopyText: () => string;
  setTheme: (theme: string) => void;
  setReadOnly: (readOnly: boolean) => void;
  setOption: (name: string, value: unknown) => void;
  execCommand: (command: string, args?: unknown) => void;
  focus: () => void;
  resize: (force?: boolean) => void;
  on: (event: string, listener: () => void) => void;
};
type AceApi = { edit: (element: HTMLElement, options?: Record<string, unknown>) => AceEditor };

declare global {
  interface Window {
    LiteLLMCodeEditorInitialCommand?: unknown;
    LiteLLMCodeEditorMenuLabels?: unknown;
    LiteLLMCodeEditor?: { receive: (message: unknown) => void };
    ace?: AceApi;
    webkit?: { messageHandlers?: { litellmCodeEditor?: { postMessage: (message: string) => void } } };
    chrome?: { webview?: { postMessage: (message: string) => void } };
  }
}

const editorFrame = document.getElementById("editor");
const editorHost = document.getElementById("ace-editor");
const editorScrollbar = document.getElementById("editor-scrollbar");
const editorScrollbarThumb = document.getElementById("editor-scrollbar-thumb");
const diffSidebar = document.getElementById("diff-sidebar");
const diffSidebarTotal = document.getElementById("diff-sidebar-total");
const diffSidebarList = document.getElementById("diff-sidebar-list");
const editorMenu = document.getElementById("editor-menu");
if (!editorFrame || !editorHost || !editorScrollbar || !editorScrollbarThumb || !diffSidebar || !diffSidebarTotal || !diffSidebarList || !editorMenu) {
  throw new Error("Code editor shell is missing.");
}
const menu: HTMLElement = editorMenu;
const frame: HTMLElement = editorFrame;
const host: HTMLElement = editorHost;
const scrollTrack: HTMLElement = editorScrollbar;
const scrollThumb: HTMLElement = editorScrollbarThumb;
const sidebarTotal: HTMLElement = diffSidebarTotal;
const sidebarList: HTMLElement = diffSidebarList;
frame.tabIndex = 0;
frame.setAttribute("role", "textbox");
frame.setAttribute("aria-multiline", "true");
frame.addEventListener("pointerdown", () => aceEditor?.focus(), true);
frame.addEventListener("mousedown", () => aceEditor?.focus(), true);

let aceEditor: AceEditor | undefined;
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

/**
 * Copy text to the system pasteboard through the legacy editing command.
 *
 * The async clipboard API is unavailable to a page the host loads from a
 * string, and its rejection used to swallow Cmd-C/Cmd-X entirely. A
 * programmatic selection inside the caller's user gesture still reaches the
 * pasteboard, so this is the deterministic path; false means the caller must
 * leave the event to the browser's own copy/cut handling.
 */
function writeClipboardText(text: string): boolean {
  if (!text) return false;
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "true");
  scratch.setAttribute("aria-hidden", "true");
  scratch.style.position = "fixed";
  scratch.style.top = "-10000px";
  scratch.style.left = "0";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let copied = false;
  try {
    scratch.select();
    scratch.setSelectionRange(0, text.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  scratch.remove();
  previous?.focus();
  return copied;
}

/** Copy the current selection, reporting whether the pasteboard took it. */
function copySelection(editor: AceEditor): boolean {
  const text = editor.getCopyText();
  return text ? writeClipboardText(text) : false;
}

/** Ask the host to inject the system pasteboard into the editor. */
function requestHostPaste(): void {
  post({ type: "paste" });
}

function handleEditorShortcut(event: KeyboardEvent): void {
  const editor = aceEditor;
  if (!editor || event.defaultPrevented || (!event.metaKey && !event.ctrlKey) || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === "a") {
    event.preventDefault();
    event.stopPropagation();
    editor.focus();
    editor.execCommand("selectall");
    return;
  }
  if (key === "z" || key === "y") {
    event.preventDefault();
    event.stopPropagation();
    editor.focus();
    editor.execCommand(key === "z" && event.shiftKey ? "redo" : key === "y" ? "redo" : "undo");
    return;
  }
  if (key === "c" || key === "x") {
    // Copy first, then cut: the selection must never disappear without
    // reaching the pasteboard. When the copy is refused, the event is left
    // untouched so the browser's own copy/cut event still runs.
    if (!copySelection(editor)) return;
    event.preventDefault();
    event.stopPropagation();
    editor.focus();
    if (key === "x") editor.execCommand("cut");
  }
  // Paste keeps the browser's own paste event: the WebView delivers the
  // system pasteboard there, and the context menu asks the host for it.
}

document.addEventListener("keydown", handleEditorShortcut, true);

const FALLBACK_MENU_LABELS: EditorMenuLabels = {
  cut: "Cut",
  copy: "Copy",
  paste: "Paste",
  selectAll: "Select All",
  undo: "Undo",
  redo: "Redo",
};

/** Labels come from the shared UI; English is only the last-resort fallback. */
function readMenuLabels(): EditorMenuLabels {
  const provided = window.LiteLLMCodeEditorMenuLabels;
  const labels: EditorMenuLabels = { ...FALLBACK_MENU_LABELS };
  if (!provided || typeof provided !== "object") return labels;
  const source = provided as Record<string, unknown>;
  for (const key of Object.keys(FALLBACK_MENU_LABELS) as Array<keyof EditorMenuLabels>) {
    const value = source[key];
    if (typeof value === "string" && value) labels[key] = value.slice(0, 64);
  }
  return labels;
}

const menuLabels = readMenuLabels();
type MenuAction = "undo" | "redo" | "cut" | "copy" | "paste" | "selectAll";
const MENU_ENTRIES: ReadonlyArray<MenuAction | "separator"> = ["undo", "redo", "separator", "cut", "copy", "paste", "separator", "selectAll"];

let documentReadOnly = false;
let menuOpen = false;

function menuActionDisabled(action: MenuAction): boolean {
  const editor = aceEditor;
  if (!editor) return true;
  if (action === "paste") return documentReadOnly;
  if (action === "cut" || action === "copy") return documentReadOnly || !editor.getCopyText();
  return false;
}

function runMenuAction(action: MenuAction): void {
  const editor = aceEditor;
  if (!editor) return;
  hideEditorMenu();
  editor.focus();
  if (action === "undo") editor.execCommand("undo");
  else if (action === "redo") editor.execCommand("redo");
  else if (action === "selectAll") editor.execCommand("selectall");
  else if (action === "copy") copySelection(editor);
  else if (action === "cut") {
    // Only remove the selection once the pasteboard holds it.
    if (copySelection(editor)) editor.execCommand("cut");
  } else if (action === "paste") requestHostPaste();
}

function renderEditorMenu(): void {
  menu.replaceChildren();
  for (const entry of MENU_ENTRIES) {
    if (entry === "separator") {
      const separator = document.createElement("div");
      separator.className = "editor-menu-separator";
      menu.appendChild(separator);
      continue;
    }
    const item = document.createElement("button");
    item.type = "button";
    item.className = "editor-menu-item";
    item.dataset.action = entry;
    item.textContent = menuLabels[entry];
    item.disabled = menuActionDisabled(entry);
    item.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      runMenuAction(entry);
    });
    menu.appendChild(item);
  }
}

function showEditorMenu(clientX: number, clientY: number): void {
  if (!aceEditor) return;
  renderEditorMenu();
  menu.hidden = false;
  menuOpen = true;
  const frameBounds = frame.getBoundingClientRect();
  const menuBounds = menu.getBoundingClientRect();
  const left = Math.max(2, Math.min(clientX - frameBounds.left, frameBounds.width - menuBounds.width - 2));
  const top = Math.max(2, Math.min(clientY - frameBounds.top, frameBounds.height - menuBounds.height - 2));
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

function hideEditorMenu(): void {
  if (!menuOpen) return;
  menuOpen = false;
  menu.hidden = true;
}

// The in-page menu replaces WebKit's own context menu, which would otherwise
// act on Ace's hidden staging textarea instead of the visible document.
frame.addEventListener("contextmenu", (event) => {
  if (!aceEditor) return;
  event.preventDefault();
  event.stopPropagation();
  showEditorMenu(event.clientX, event.clientY);
});
document.addEventListener("pointerdown", (event) => {
  if (menuOpen && event.target instanceof Node && !menu.contains(event.target)) hideEditorMenu();
}, true);
document.addEventListener("keydown", (event) => {
  if (menuOpen && event.key === "Escape") hideEditorMenu();
}, true);
window.addEventListener("blur", hideEditorMenu);
window.addEventListener("resize", hideEditorMenu);
frame.addEventListener("wheel", hideEditorMenu, { passive: true } as AddEventListenerOptions);

const DIFF_PREVIEW_LINE_LIMIT = 4;
const DIFF_SIDEBAR_ENTRY_LIMIT = 24;
// Forward the newest editor value at most once per frame-sized interval. This
// keeps bursts coalesced without waiting for typing to go idle, so dependent
// React/Core state stays current during continuous input.
const CHANGE_SYNC_INTERVAL_MS = 16;
const DIFF_DEBOUNCE_MS = 120;
const DIFF_LCS_CELL_LIMIT = 1_000_000;

function aceViewport(): HTMLElement {
  return aceEditor?.renderer.scroller ?? frame;
}

function aceContentHeight(viewport: HTMLElement): number {
  const editor = aceEditor;
  if (!editor) return viewport.scrollHeight;
  const config = editor.renderer.layerConfig;
  const lineHeight = config?.lineHeight ?? 16;
  // Ace keeps its content layer one line taller than the scroller
  // (``minHeight = scrollerHeight + lineHeight``), so the scroller's own
  // scrollHeight always claims a one-line overflow and must never be the
  // overflow signal: a document that fits would still show a scroller. The
  // document's own pixel height decides instead.
  const lineHeightTotal = editor.session.getScreenLength() * lineHeight;
  return Math.max(config?.maxHeight ?? 0, lineHeightTotal);
}

function renderEditorScrollbar(): void {
  if (!aceEditor) return;
  const viewport = aceViewport();
  const contentHeight = aceContentHeight(viewport);
  const maximumScrollTop = Math.max(0, contentHeight - viewport.clientHeight);
  if (maximumScrollTop <= 0) {
    scrollTrack.hidden = true;
    return;
  }
  scrollTrack.hidden = false;
  const trackHeight = scrollTrack.clientHeight;
  const thumbHeight = Math.max(24, Math.min(trackHeight, Math.round(trackHeight * viewport.clientHeight / contentHeight)));
  const maximumThumbTop = Math.max(0, trackHeight - thumbHeight);
  const thumbTop = maximumThumbTop === 0 ? 0 : Math.round(maximumThumbTop * aceEditor.renderer.getScrollTop() / maximumScrollTop);
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
  if (!aceEditor) return;
  const viewport = aceViewport();
  const trackBounds = scrollTrack.getBoundingClientRect();
  const thumbHeight = scrollThumb.getBoundingClientRect().height;
  const maximumThumbTop = Math.max(0, trackBounds.height - thumbHeight);
  const maximumScrollTop = Math.max(0, aceContentHeight(viewport) - viewport.clientHeight);
  if (maximumThumbTop <= 0 || maximumScrollTop <= 0) return;
  const thumbTop = Math.max(0, Math.min(maximumThumbTop, clientY - trackBounds.top - grabOffset));
  aceEditor.renderer.scrollToY(maximumScrollTop * thumbTop / maximumThumbTop);
}

function installEditorScrollbar(): void {
  if (!aceEditor || scrollbarObserver) return;
  const viewport = aceViewport();
  viewport.addEventListener("scroll", queueEditorScrollbar, { passive: true });
  aceEditor.renderer.on("afterRender", queueEditorScrollbar);
  scrollbarObserver = new ResizeObserver(queueEditorScrollbar);
  scrollbarObserver.observe(frame);
  scrollbarObserver.observe(viewport);
  window.addEventListener("resize", queueEditorScrollbar);
  queueEditorScrollbar();
}

scrollTrack.addEventListener("pointerdown", (event) => {
  if (!aceEditor || event.button !== 0 || scrollTrack.hidden) return;
  event.preventDefault();
  aceEditor.focus();
  const thumbBounds = scrollThumb.getBoundingClientRect();
  const grabOffset = event.target === scrollThumb ? event.clientY - thumbBounds.top : thumbBounds.height / 2;
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

function normalizedText(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}
function normalizedLines(text: string): string[] {
  return normalizedText(text).split("\n");
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

type DiffHunk = { beforeStart: number; beforeEnd: number; afterStart: number; afterEnd: number };
function diffHunks(before: string[], after: string[]): DiffHunk[] {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix && before[before.length - suffix - 1] === after[after.length - suffix - 1]) suffix += 1;
  const beforeMiddle = before.slice(prefix, before.length - suffix);
  const afterMiddle = after.slice(prefix, after.length - suffix);
  if (beforeMiddle.length === 0 && afterMiddle.length === 0) return [];
  type DiffOp = "equal" | "delete" | "insert";
  const operations: DiffOp[] = [];
  if (beforeMiddle.length * afterMiddle.length > DIFF_LCS_CELL_LIMIT) {
    operations.push(...Array.from({ length: beforeMiddle.length }, () => "delete" as const));
    operations.push(...Array.from({ length: afterMiddle.length }, () => "insert" as const));
  } else {
    const width = afterMiddle.length + 1;
    const table = new Uint32Array((beforeMiddle.length + 1) * width);
    for (let row = beforeMiddle.length - 1; row >= 0; row -= 1) {
      for (let column = afterMiddle.length - 1; column >= 0; column -= 1) {
        table[row * width + column] = beforeMiddle[row] === afterMiddle[column]
          ? table[(row + 1) * width + column + 1] + 1
          : Math.max(table[(row + 1) * width + column], table[row * width + column + 1]);
      }
    }
    let row = 0;
    let column = 0;
    while (row < beforeMiddle.length || column < afterMiddle.length) {
      if (row < beforeMiddle.length && column < afterMiddle.length && beforeMiddle[row] === afterMiddle[column]) {
        operations.push("equal");
        row += 1;
        column += 1;
      } else if (column < afterMiddle.length && (row >= beforeMiddle.length || table[row * width + column + 1] >= table[(row + 1) * width + column])) {
        operations.push("insert");
        column += 1;
      } else {
        operations.push("delete");
        row += 1;
      }
    }
  }
  const hunks: DiffHunk[] = [];
  let beforeIndex = prefix;
  let afterIndex = prefix;
  let current: DiffHunk | undefined;
  const flush = () => {
    if (current) hunks.push(current);
    current = undefined;
  };
  for (const operation of operations) {
    if (operation === "equal") {
      flush();
      beforeIndex += 1;
      afterIndex += 1;
    } else {
      current ??= { beforeStart: beforeIndex, beforeEnd: beforeIndex, afterStart: afterIndex, afterEnd: afterIndex };
      if (operation === "delete") {
        beforeIndex += 1;
        current.beforeEnd = beforeIndex;
      } else {
        afterIndex += 1;
        current.afterEnd = afterIndex;
      }
    }
  }
  flush();
  return hunks;
}

function lineRange(lines: string[], start: number, end: number): string {
  const first = Math.max(1, Math.min(lines.length || 1, start + 1));
  const last = Math.max(first, Math.min(lines.length || first, end));
  return first === last ? `L${first}` : `L${first}–L${last}`;
}
function previewLines(lines: string[], start: number, end: number): EditorDiffPreview {
  if (end <= start) return { lines: [], truncated: false };
  const previewEnd = Math.min(end, start + DIFF_PREVIEW_LINE_LIMIT);
  return { lines: lines.slice(start, previewEnd), truncated: previewEnd < end };
}
function diffLabel(diff: EditorDiff): string {
  return [diff.added > 0 ? `+${diff.added}` : undefined, diff.changed > 0 ? `~${diff.changed}` : undefined, diff.deleted > 0 ? `−${diff.deleted}` : undefined]
    .filter((part): part is string => Boolean(part)).join(" ");
}
function computeDiff(): ComputedEditorDiff {
  if (!aceEditor || !showingDiff) return { added: 0, changed: 0, deleted: 0, entries: [] };
  const current = normalizedLines(aceEditor.getValue());
  const original = normalizedLines(baselineText);
  let added = 0;
  let changed = 0;
  let deleted = 0;
  const entries: EditorDiffEntry[] = [];
  for (const hunk of diffHunks(original, current)) {
    const beforeCount = hunk.beforeEnd - hunk.beforeStart;
    const afterCount = hunk.afterEnd - hunk.afterStart;
    const changedLines = Math.min(beforeCount, afterCount);
    const addedLines = Math.max(0, afterCount - beforeCount);
    const deletedLines = Math.max(0, beforeCount - afterCount);
    changed += changedLines;
    added += addedLines;
    deleted += deletedLines;
    entries.push({
      kind: beforeCount === 0 ? "added" : afterCount === 0 ? "deleted" : "changed",
      range: afterCount > 0 ? lineRange(current, hunk.afterStart, hunk.afterEnd) : lineRange(original, hunk.beforeStart, hunk.beforeEnd),
      added: addedLines,
      changed: changedLines,
      deleted: deletedLines,
      before: previewLines(original, hunk.beforeStart, hunk.beforeEnd),
      after: previewLines(current, hunk.afterStart, hunk.afterEnd),
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
  if (!aceEditor || applyingHostUpdate) return;
  queueDiffSidebar();
  if (changeTimer !== undefined) return;
  changeTimer = window.setTimeout(() => {
    changeTimer = undefined;
    if (!aceEditor) return;
    const summary = computeDiff();
    latestDiff = summary;
    post({ type: "change", text: aceEditor.getValue(), added: summary.added, changed: summary.changed, deleted: summary.deleted });
  }, CHANGE_SYNC_INTERVAL_MS);
}

function modeForLanguage(language: EditorLanguage): string {
  if (language === "json") return "ace/mode/json";
  if (language === "toml") return "ace/mode/toml";
  if (language === "yaml") return "ace/mode/yaml";
  return "ace/mode/text";
}
function configureEditor(command: ReplaceDocumentCommand): void {
  if (!aceEditor) return;
  // Workers require a URL and violate the self-contained WebView contract;
  // syntax highlighting remains local and JSON/TOML do not need validation.
  aceEditor.session.setUseWorker(false);
  aceEditor.session.setMode(modeForLanguage(command.language));
  aceEditor.setReadOnly(command.readOnly);
  documentReadOnly = command.readOnly;
  hideEditorMenu();
  aceEditor.setOption("wrap", "free");
  // Copy and Cut act on the current line when nothing is selected, which is
  // what every code editor does and what makes Cmd-X feel alive: without this
  // an unselected Cmd-X has nothing to cut and silently does nothing.
  aceEditor.setOption("copyWithEmptySelection", true);
  aceEditor.setOption("showPrintMargin", false);
  aceEditor.setOption("highlightActiveLine", true);
  aceEditor.setOption("highlightSelectedWord", true);
  aceEditor.setOption("showFoldWidgets", true);
  aceEditor.setOption("displayIndentGuides", true);
}
function createEditor(command: ReplaceDocumentCommand): void {
  const aceApi = window.ace;
  if (!aceApi) throw new Error("Ace failed to load.");
  aceEditor = aceApi.edit(host);
  // `ace.edit` reads text from the element, not an options.value property.
  // Set the initial document before subscribing to changes so bootstrap does
  // not echo the host's replace command back as a user edit.
  aceEditor.setValue(normalizedText(command.value), -1);
  aceEditor.setTheme("ace/theme/textmate");
  configureEditor(command);
  aceEditor.session.on("change", reportChange);
  aceEditor.renderer.on("afterRender", queueEditorScrollbar);
  // Ace's default command set includes undo/redo, Tab indentation, folding,
  // and multi-select. Keep the built-in command/keybinding behavior intact;
  // only the search box extension is loaded explicitly above.
  aceEditor.resize(true);
  installEditorScrollbar();
}
function replaceDocument(command: ReplaceDocumentCommand): void {
  cancelChange();
  cancelDiffSidebar();
  showingDiff = command.showDiff;
  document.body.classList.toggle("diff-sidebar-enabled", showingDiff);
  baselineText = normalizedText(command.baseline);
  latestDiff = { added: 0, changed: 0, deleted: 0 };
  sidebarTotal.textContent = "";
  sidebarList.replaceChildren();
  const nextText = normalizedText(command.value);
  if (!aceEditor) {
    try {
      createEditor(command);
    } catch {
      post({ type: "error" });
      return;
    }
  } else {
    const documentChanged = activeDocumentKey !== command.documentKey;
    const replaceText = aceEditor.getValue() !== nextText;
    applyingHostUpdate = replaceText;
    try {
      configureEditor(command);
      if (replaceText) aceEditor.setValue(nextText, documentChanged ? -1 : 1);
      if (documentChanged) {
        aceEditor.renderer.scrollToY(0);
        aceEditor.selection?.clearSelection?.();
      }
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
  baselineText = normalizedText(baseline);
  latestDiff = { added: 0, changed: 0, deleted: 0 };
  queueDiffSidebar();
}
function receive(input: unknown): void {
  const command = typeof input === "string" ? (() => {
    try { return JSON.parse(input) as unknown; } catch { return undefined; }
  })() : input;
  if (!command || typeof command !== "object") return;
  const message = command as Partial<HostCommand>;
  if (message.type === "replace" && typeof message.documentKey === "string" && typeof message.value === "string"
    && typeof message.baseline === "string"
    && typeof message.readOnly === "boolean" && typeof message.showDiff === "boolean"
    && (message.language === "json" || message.language === "toml" || message.language === "yaml" || message.language === "text")) {
    replaceDocument(message as ReplaceDocumentCommand);
  } else if (message.type === "setBaseline" && typeof message.baseline === "string") {
    setBaseline(message.baseline);
  } else if (message.type === "insertText" && typeof message.text === "string") {
    // The host reads the system pasteboard for the context menu's Paste item
    // and injects it here; the editor document itself stays capped at 2 MiB.
    insertHostText(message.text);
  } else if (message.type === "focus") {
    aceEditor?.focus();
  }
}

function insertHostText(text: string): void {
  const editor = aceEditor;
  if (!editor || documentReadOnly || !text) return;
  editor.focus();
  editor.execCommand("paste", text.slice(0, 1024 * 1024));
}

window.LiteLLMCodeEditor = { receive };
const initialCommand = window.LiteLLMCodeEditorInitialCommand;
delete window.LiteLLMCodeEditorInitialCommand;
if (initialCommand !== undefined) receive(initialCommand);
post({ type: "ready", documentKey: activeDocumentKey });
