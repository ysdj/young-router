/**
 * The editor bundle as the browser host sees it: not there.
 *
 * `CodeEditorWebBundle.ts` is the whole Ace editor, minified, as one string —
 * half a megabyte that the two native hosts inline into the editor page they
 * hand their WebView, because a WebView is given a document and has no origin
 * to fetch a script from. This host is the opposite: it serves that page from
 * Core (`editor.html`, built by `rn/apps/web/build.mjs`) and frames it, so the
 * page never inlines anything here and the shell would be carrying half a
 * megabyte of JavaScript no code path in this bundle reads.
 *
 * `codeEditorPageHtml` therefore produces a page with no editor on this host,
 * which is exactly why nothing here calls it: `WebCodeEditor` frames the
 * served document instead, and `readOnlyCodeEditorHtml` — the native hosts'
 * viewer string — is only ever passed to `showReadOnlyText`, whose browser
 * implementation draws the record with the shared viewer and ignores the HTML.
 *
 * Metro resolves `.web.ts` only for a `web` platform, and both native bundles
 * state their own (`macos`, `windows`), so each host keeps the copy it needs.
 */
export const CODE_EDITOR_WEB_BUNDLE = "";
