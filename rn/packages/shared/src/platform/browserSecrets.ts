/** The browser host's secret writer; the native bundles never call this. */
export type BrowserSecretStager = (options: { domain: string; field: string; target?: string; value: string }) => Promise<{ revision: number; present: boolean }>;
let stager: BrowserSecretStager | undefined;
let reader: ((options: { domain: string; field: string; target?: string }) => Promise<{ present: boolean; value?: string }>) | undefined;

export function setBrowserSecretStager(next: BrowserSecretStager): void {
  stager = next;
}

export function setBrowserSecretReader(next: typeof reader): void {
  reader = next;
}

export function getBrowserSecretStager(): BrowserSecretStager | undefined {
  return stager;
}

export function getBrowserSecretReader(): typeof reader {
  return reader;
}
