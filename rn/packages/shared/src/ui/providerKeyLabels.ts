type RelayKeyLabelSource = {
  accountLabel: string;
  resourceLabel: string;
  multiplier?: number;
};

export type ProviderKeyLabelChoice = {
  name: string;
  kind: "independent" | "relay" | "account";
  source?: RelayKeyLabelSource;
  accountPrefix?: string;
};

type Translate = (key: string, values?: Record<string, string | number>) => string;

export function apiKeyDisplayName(value: unknown, translate: Translate): string {
  const name = typeof value === "string"
    ? value
    : typeof value === "number" && Number.isFinite(value)
      ? String(value)
      : typeof value === "boolean" ? String(value) : "";
  if (!name) return translate("common.notAvailable");
  return name === "default" ? translate("providers.defaultKey") : name;
}

export function relaySourceName(source: RelayKeyLabelSource | undefined): string {
  return source ? [source.accountLabel, source.resourceLabel].filter(Boolean).join("/") : "";
}

export function providerKeyChoiceLabel(choice: ProviderKeyLabelChoice, translate: Translate): string {
  const name = apiKeyDisplayName(choice.name, translate);
  if (choice.kind !== "relay") return name;
  const prefix = choice.accountPrefix?.trim() ? `${choice.accountPrefix.trim()}/` : "";
  const ownName = prefix && !name.startsWith(prefix) ? `${prefix}${name}` : name;
  const sourceName = relaySourceName(choice.source) || ownName;
  const multiplier = choice.source && Number.isFinite(choice.source.multiplier) ? ` (${choice.source.multiplier}x)` : "";
  return `${sourceName}${multiplier}`;
}
