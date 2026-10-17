/**
 * How a 倍率 is spelled everywhere it is printed.
 *
 * Two spellings of one rate reach the panes.  An account-backed service states
 * its own (`x0.00 credits`, `x0.10`), and a relay group's multiplier arrives
 * as a number.  Both are printed by this module, so a free model and a cheap
 * one can never differ in more characters than they differ in cost: a trailing
 * zero carries no information a reader acts on.
 *
 * The rule is deliberate about what it does not touch.  A leading `x` or `×`
 * and the integer part are kept exactly as stated, and a spelling this module
 * cannot read as a number is returned unchanged — the catalog's own form is the
 * only evidence there is for a rate nobody parsed, and guessing at it would put
 * a number in the pane that the upstream never billed.
 */

/** The rate a printed spelling states, or `undefined` when it states none. */
export function rateNumber(value: string): number | undefined {
  const text = value.trim().replace(/^[x×]\s*/i, "");
  if (!text) return undefined;
  const parsed = Number(text);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

/** One stated rate with the `credits` unit word dropped. */
export function rateSpelling(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return trimmed;
  const prefix = /^[x×]\s*/i.exec(trimmed)?.[0] ?? "";
  const number = rateNumber(trimmed);
  if (number === undefined) return trimmed;
  return prefix + String(number);
}

/** One rate number as the panes spell it: `×` then the number. */
export function multiplierSpelling(multiplier: number): string {
  return `×${String(multiplier)}`;
}
