import { en } from "./en";
import { zhHans } from "./zh-Hans";
import type { LanguagePreference, ResolvedLanguage, TranslationKey, Translator } from "./types";

export type { LanguagePreference, ResolvedLanguage, TranslationKey, Translator } from "./types";
export { en } from "./en";
export { zhHans } from "./zh-Hans";

export function normalizeSystemLanguage(systemLocale?: string): ResolvedLanguage {
  const locale = (systemLocale ?? "").trim().toLowerCase().replace(/_/g, "-");
  return locale === "zh" || locale.startsWith("zh-") ? "zh-Hans" : "en";
}

export function resolveLanguage(preference: LanguagePreference = "system", systemLocale?: string): ResolvedLanguage {
  if (preference === "system") return normalizeSystemLanguage(systemLocale);
  return preference === "zh-Hans" ? "zh-Hans" : "en";
}

export function createTranslator(preference: LanguagePreference = "system", systemLocale?: string): Translator {
  const messages = resolveLanguage(preference, systemLocale) === "zh-Hans" ? zhHans : en;
  return (key: string, values?: Record<string, string | number>): string => {
    const template = messages[key as TranslationKey] ?? en[key as TranslationKey] ?? key;
    return values ? template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, name: string) => String(values[name] ?? `{${name}}`)) : template;
  };
}
