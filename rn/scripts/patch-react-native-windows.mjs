#!/usr/bin/env node
// Give the shared ScrollView a wheel step the size Windows itself scrolls.
//
// React Native Windows turns a wheel event into `delta * pointScaleFactor`
// tracker units and eases every event onto the composition tracker
// (`ScrollViewComponentView::OnPointerWheelChanged`).  A detent is 120 units,
// but the rest of Windows reads that as `WheelScrollLines` lines (3 by
// default) of 16 DIP; the raw mapping moved about 60% of that, and the
// per-event ease made a precision touchpad trail the finger and coast after it
// stopped.  Measured on a 200% host: ~29 DIP per detent before, 48 after.
//
// The applied change maps the delta through `SPI_GETWHEELSCROLLLINES` and
// applies it immediately.  It is a source change, so the binary that ships has
// to be the patched source build: `build-windows.ps1` still builds against the
// NuGet package (a source build costs half an hour and has no valid PRI), then
// replaces the packaged `Microsoft.ReactNative.dll` with the prepared one after
// checking both report the same file version.
//
// Idempotent: a tree that already carries the mapping is left alone.  Fails
// loudly when the upstream text it rewrites is not found, so an RNW upgrade
// cannot silently ship the old behaviour again.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// The checkout that owns the installed `node_modules` is the one to rewrite:
// an artifact build runs from the Windows working copy, not from the share, so
// `--root <checkout>` names it.  It defaults to the checkout this script lives
// in, which is what a build started from that same tree needs.
const argument = process.argv.indexOf("--root");
const repositoryRoot =
  argument >= 0 && process.argv[argument + 1]
    ? resolve(process.argv[argument + 1])
    : resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = join(
  repositoryRoot,
  "rn/node_modules/react-native-windows/Microsoft.ReactNative/Fabric/Composition/ScrollViewComponentView.cpp"
);

const INCLUDES = ["#include <windows.ui.composition.interop.h>"];
const INCLUDES_WITH_WINDOWS_H = ["#include <windows.h>", "#include <windows.ui.composition.interop.h>"];

const LINE_DELTA_ANCHOR = "constexpr float c_scrollerLineDelta = 16.0f;";
const SYSTEM_LINES = `${LINE_DELTA_ANCHOR}

// One wheel detent is a system-defined number of lines, not a pixel count.
// Windows delivers WHEEL_DELTA units per detent and a precision touchpad sends
// the matching fraction of one, while Windows' own scroll views move
// WheelScrollLines lines per detent.  Handing the raw units to the tracker
// moved a fraction of that, which reads as sluggish scrolling and is worst on
// a touchpad, where every small delta is scaled down the same way.
static int GetSystemWheelScrollLines() noexcept {
  UINT scrollLines = 3;
  if (!SystemParametersInfo(SPI_GETWHEELSCROLLLINES, 0, &scrollLines, 0) || scrollLines == 0 ||
      scrollLines == 0xFFFFFFFFu) {
    scrollLines = 3;
  }
  return static_cast<int>(scrollLines);
}`;

const HANDLER_START = "void ScrollViewComponentView::OnPointerWheelChanged(";
const HANDLER_END = "void ScrollViewComponentView::OnPointerPressed(";
const HANDLER = `void ScrollViewComponentView::OnPointerWheelChanged(
    const winrt::Microsoft::ReactNative::Composition::Input::PointerRoutedEventArgs &args) noexcept {
  auto ppp = args.GetCurrentPoint(-1).Properties();
  auto delta = static_cast<float>(ppp.MouseWheelDelta());
  // A detent moves the distance Windows' own scroll views move, and it moves
  // now: the tracker's animation is the cost of a programmatic scroll, and
  // restarting it for every wheel event is what made a touchpad feel like it
  // was dragging through treacle.
  const float wheelDelta = (delta / WHEEL_DELTA) * static_cast<float>(GetSystemWheelScrollLines()) *
      c_scrollerLineDelta * m_layoutMetrics.pointScaleFactor;
  if (ppp.IsHorizontalMouseWheel()) {
    if (wheelDelta > 0) {
      if (scrollLeft(wheelDelta, false)) {
        args.Handled(true);
      }
    } else if (wheelDelta < 0) {
      if (scrollRight(-wheelDelta, false)) {
        args.Handled(true);
      }
    }
  } else {
    if (wheelDelta > 0) {
      if (scrollUp(wheelDelta, false)) {
        args.Handled(true);
      }
    } else if (wheelDelta < 0) {
      if (scrollDown(-wheelDelta, false)) {
        args.Handled(true);
      }
    }
  }
  Super::OnPointerWheelChanged(args);
}

`;

/** The upstream text this rewrite replaces, so an unmapped tree fails loudly. */
const UNPATCHED_HANDLER = `void ScrollViewComponentView::OnPointerWheelChanged(
    const winrt::Microsoft::ReactNative::Composition::Input::PointerRoutedEventArgs &args) noexcept {
  auto ppp = args.GetCurrentPoint(-1).Properties();
  auto delta = static_cast<float>(ppp.MouseWheelDelta());
  if (ppp.IsHorizontalMouseWheel()) {`;

const withNewlines = (text, eol) => text.split("\n").join(eol);

let document;
try {
  document = readFileSync(source, "utf8");
} catch (reason) {
  console.error(`patch-react-native-windows: cannot read ${source}: ${reason.message}`);
  process.exit(1);
}

if (document.includes("GetSystemWheelScrollLines")) {
  console.log("patch-react-native-windows: the tree already carries the system wheel mapping");
  process.exit(0);
}

const eol = document.includes("\r\n") ? "\r\n" : "\n";
const expectedHandler = withNewlines(UNPATCHED_HANDLER, eol);
if (!document.includes(expectedHandler)) {
  console.error(
    "patch-react-native-windows: ScrollViewComponentView::OnPointerWheelChanged is not the shape this script rewrites.\n" +
      "The installed react-native-windows changed; re-check its wheel handling before building."
  );
  process.exit(1);
}

const includeIndex = document.indexOf(INCLUDES[0]);
if (includeIndex < 0) {
  console.error("patch-react-native-windows: the composition interop include was not found.");
  process.exit(1);
}
document = `${document.slice(0, includeIndex)}${withNewlines(INCLUDES_WITH_WINDOWS_H.join("\n"), eol)}${document.slice(includeIndex + INCLUDES[0].length)}`;

const lineDeltaIndex = document.indexOf(LINE_DELTA_ANCHOR);
if (lineDeltaIndex < 0) {
  console.error("patch-react-native-windows: c_scrollerLineDelta was not found.");
  process.exit(1);
}
document = `${document.slice(0, lineDeltaIndex)}${withNewlines(SYSTEM_LINES, eol)}${document.slice(lineDeltaIndex + LINE_DELTA_ANCHOR.length)}`;

const handlerStart = document.indexOf(HANDLER_START);
const handlerEnd = document.indexOf(HANDLER_END, handlerStart);
if (handlerStart < 0 || handlerEnd < 0) {
  console.error("patch-react-native-windows: the wheel handler's bounds were not found.");
  process.exit(1);
}
document = document.slice(0, handlerStart) + withNewlines(HANDLER, eol) + document.slice(handlerEnd);

writeFileSync(source, document);
console.log(`patch-react-native-windows: mapped the wheel delta through SPI_GETWHEELSCROLLLINES in ${source}`);
