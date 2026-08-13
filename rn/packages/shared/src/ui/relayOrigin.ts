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
  const countryCodeSuffix = labels.length >= 3
    && labels[labels.length - 1].length === 2
    && labels[labels.length - 2].length <= 3;
  return labels[countryCodeSuffix ? labels.length - 3 : labels.length - 2] ?? labels[0] ?? "";
}
