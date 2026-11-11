export function normalizeRelayOrigin(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  let normalized = /^[a-z][a-z\d+.-]*:\/\//iu.test(trimmed)
    ? trimmed
    : `https://${trimmed.replace(/^\/+/, "")}`;
  normalized = normalized.replace(/\/+$/u, "");
  while (/\/(?:login|signin|sign-in|dashboard|v1)$/iu.test(normalized)) {
    normalized = normalized.replace(/\/(?:login|signin|sign-in|dashboard|v1)$/iu, "");
  }
  return normalized;
}

// A country-code domain can carry a second-level suffix label
// ("example.co.uk"), but only a known registry prefix does: a short middle
// label that is not one of them is the registrable domain itself, so
// "aaa.bb.cc" names "bb" and not the subdomain in front of it.
const TWO_PART_SUFFIX_LABELS = new Set([
  "ac", "ad", "co", "com", "ed", "edu", "firm", "gen", "go", "gov", "govt", "gr",
  "ind", "lg", "ltd", "me", "mil", "ne", "net", "nhs", "or", "org", "plc", "sch", "web",
]);

export function suggestedRelayStationName(value: string): string {
  const normalized = normalizeRelayOrigin(value);
  if (!normalized) return "";
  let hostname: string;
  try {
    hostname = new URL(normalized).hostname.toLowerCase().replace(/\.$/u, "");
  } catch {
    hostname = normalized.replace(/^[a-z][a-z\d+.-]*:\/\//iu, "").split("/", 1)[0].replace(/:\d+$/u, "").toLowerCase();
  }
  const unwrapped = hostname.replace(/^\[|\]$/gu, "");
  if (!unwrapped || unwrapped === "localhost" || unwrapped.includes(":") || /^\d{1,3}(?:\.\d{1,3}){3}$/u.test(unwrapped)) {
    return unwrapped;
  }
  const labels = unwrapped.split(".").filter(Boolean);
  if (labels.length < 2) return labels[0] ?? "";
  const twoPartSuffix = labels.length >= 3
    && labels[labels.length - 1].length === 2
    && TWO_PART_SUFFIX_LABELS.has(labels[labels.length - 2]);
  return labels[twoPartSuffix ? labels.length - 3 : labels.length - 2] ?? labels[0] ?? "";
}

export function suggestedProviderName(value: string): string {
  // Suggest a provider name from a base URL only once the hostname looks
  // complete: while the user is still typing the first label ("h", "api")
  // there is no meaningful name yet, and pre-filling those fragments would
  // freeze the name field at a single stray character.
  const normalized = normalizeRelayOrigin(value);
  if (!normalized) return "";
  let hostname = "";
  try {
    hostname = new URL(normalized).hostname.toLowerCase().replace(/\.$/u, "");
  } catch {
    return "";
  }
  if (!hostname.includes(".")) return "";
  // An IP address is not a name: leave the field to the user instead of
  // suggesting "127.0.0.1".
  if (hostname.replace(/^\[|\]$/gu, "").includes(":") || /^\d{1,3}(?:\.\d{1,3}){3}$/u.test(hostname)) return "";
  const suggestion = suggestedRelayStationName(normalized);
  return suggestion.length > 1 ? suggestion : "";
}

/**
 * The one origin key a relay browser session proved.
 *
 * Two spellings of the same deployment share a station: the host is
 * lower-cased, default ports are dropped, and a trailing slash disappears.
 * The path stays part of the identity, because a deployment base path is a
 * different site from the origin beside it.
 */
export function stationOriginKey(value: string): string {
  const normalized = normalizeRelayOrigin(value);
  if (!normalized) return "";
  try {
    const parsed = new URL(normalized);
    const port = parsed.port && !((parsed.protocol === "https:" && parsed.port === "443") || (parsed.protocol === "http:" && parsed.port === "80")) ? `:${parsed.port}` : "";
    const path = parsed.pathname.replace(/\/+$/u, "");
    return `${parsed.protocol.toLowerCase()}//${parsed.hostname.toLowerCase()}${port}${path}`;
  } catch {
    return normalized.toLowerCase();
  }
}

/** The account one sign-in actually landed on, or the pending shell itself. */
export type RelayLoginIdentity = {
  id: string;
  origin: string;
  username?: string;
  loginStatus?: string;
};

/**
 * A sign-in asks with a pending id and lands on whatever row the station's
 * answer identifies.
 *
 * Core never writes a second account for one site and address, so the id the
 * browser flow started with is often not the id that exists afterwards.  Both
 * the wizard and the provider pane must read the account the station actually
 * answered for; matching the pending id alone reports a successful sign-in as
 * a failed resource read.
 */
export function relayLoginLandedAccount<T extends RelayLoginIdentity>(
  accounts: readonly T[],
  pendingID: string,
  origin: string,
  username = "",
): T | undefined {
  const target = stationOriginKey(origin);
  const matched = accounts.find((account) => account.id !== pendingID
    && stationOriginKey(account.origin) === target
    && (username ? account.username === username : account.loginStatus === "signed_in"));
  return matched ?? accounts.find((account) => account.id === pendingID);
}
