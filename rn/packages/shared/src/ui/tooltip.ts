const TOOLTIP_SCREEN_INSET = 48;
const TOOLTIP_MAX_WIDTH = 560;
const TOOLTIP_MAX_HEIGHT = 420;
const TOOLTIP_CELL_WIDTH = 7.5;
const TOOLTIP_LINE_HEIGHT = 16;

type ScreenBounds = {
  width: number;
  height: number;
};

function tooltipCellWidth(character: string): number {
  return /^[\u0000-\u024f]$/u.test(character) ? 1 : 2;
}

function wrapTooltipLine(line: string, columnLimit: number): string[] {
  if (line.length === 0) return [""];
  const wrapped: string[] = [];
  let current = "";
  let width = 0;
  for (const character of line) {
    const characterWidth = tooltipCellWidth(character);
    if (current.length > 0 && width + characterWidth > columnLimit) {
      wrapped.push(current);
      current = "";
      width = 0;
    }
    current += character;
    width += characterWidth;
  }
  if (current.length > 0) wrapped.push(current);
  return wrapped;
}

function appendEllipsis(line: string, columnLimit: number): string {
  const characters = Array.from(line);
  const suffix = ".".repeat(Math.min(3, columnLimit));
  let width = characters.reduce((total, character) => total + tooltipCellWidth(character), 0);
  while (characters.length > 0 && width + suffix.length > columnLimit) {
    width -= tooltipCellWidth(characters.pop() ?? "");
  }
  return `${characters.join("")}${suffix}`;
}

export function screenBoundedTooltipText(text: string, screen: ScreenBounds): string {
  const availableWidth = Math.min(TOOLTIP_MAX_WIDTH, Math.max(TOOLTIP_CELL_WIDTH, screen.width - TOOLTIP_SCREEN_INSET));
  const availableHeight = Math.min(TOOLTIP_MAX_HEIGHT, Math.max(TOOLTIP_LINE_HEIGHT, screen.height - TOOLTIP_SCREEN_INSET));
  const columnLimit = Math.max(1, Math.floor(availableWidth / TOOLTIP_CELL_WIDTH));
  const lineLimit = Math.max(1, Math.floor(availableHeight / TOOLTIP_LINE_HEIGHT));
  const wrapped = text.split("\n").flatMap((line) => wrapTooltipLine(line, columnLimit));
  if (wrapped.length <= lineLimit) return wrapped.join("\n");
  const visible = wrapped.slice(0, lineLimit);
  visible[lineLimit - 1] = appendEllipsis(visible[lineLimit - 1] ?? "", columnLimit);
  return visible.join("\n");
}
