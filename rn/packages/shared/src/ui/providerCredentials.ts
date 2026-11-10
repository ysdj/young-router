/**
 * A provider's credentials, as one list.
 *
 * Two surfaces read the same keys: the 密钥 table beside the provider and the
 * key picker beside 获取模型.  They used to derive their rows apart — the table
 * from the bound station's own accounts, the picker from every station whose
 * Base URL matched the provider's address — so one pane could list two keys and
 * the picker under it offer six, four of which no row answered for.  This
 * module is the one derivation both read.
 *
 * Three things it settles, which the two derivations disagreed about:
 *
 * - A relay slot and the station resource it names are **one** credential, not
 *   two rows: the key the provider holds and the key the station still lists
 *   are the same key seen from both ends.
 * - A slot whose resource the station no longer lists is still a credential the
 *   provider holds.  The table draws it as an orphan so it can be repaired or
 *   deleted, and 获取模型 can still ask the provider's own address with it — the
 *   stored key is in the document whether or not the station still advertises
 *   it.
 * - A resource no slot holds is a credential 获取模型 may import.  It is a row
 *   the table already shows, because the table lists the account's keys rather
 *   than the provider's slots.
 *
 * The resources handed in must be the ones the table lists (the provider's own
 * station accounts, after their own visibility rules).  That is what keeps the
 * two surfaces from drifting: they are not two readings of the same facts, they
 * are one reading rendered twice.
 */

/** The slot's own account of where its key came from. */
export type CredentialSlotSource = {
  kind: "independent" | "relay";
  stationID?: string;
  accountID?: string;
  resourceID?: string;
};

/** A key slot the provider document holds. */
export type CredentialSlot = {
  id: string;
  name: string;
  /** Core's own reference, not a secret the user may pick or edit. */
  managed: boolean;
  source: CredentialSlotSource;
};

/** A key one of the provider's station accounts still lists. */
export type CredentialResource = {
  stationID: string;
  accountID: string;
  resourceID: string;
  name: string;
};

/** One credential, however many ends of the app describe it. */
export type ProviderCredentialKey = {
  /**
   * The credential's identity.  A relay credential is its account and resource
   * (the pair the station names it by); a custom key is the slot Core holds.
   * Two surfaces that print the same key carry the same string here.
   */
  key: string;
  kind: "independent" | "relay";
  /** The provider's own slot, when the document holds this credential. */
  slotID?: string;
  /** The station resource, when the station still lists it. */
  resource?: CredentialResource;
  /** What a row for this credential prints. */
  name: string;
};

/**
 * The identity the station itself uses for one of its keys.
 *
 * The account is part of it: two accounts on one station can both list a group
 * called `国模`, and those are two keys with two secrets, not one.
 */
export function relayCredentialKey(accountID: string, resourceID: string): string {
  return `${accountID}\x1f${resourceID}`;
}

/**
 * Every credential the provider holds or may import, in the order the key table
 * lists them: the user's own keys, then the station's keys, then the slots the
 * station no longer answers for.
 */
export function providerCredentialKeys(
  slots: readonly CredentialSlot[],
  resources: readonly CredentialResource[],
): ProviderCredentialKey[] {
  const resourceByKey = new Map<string, CredentialResource>();
  for (const resource of resources) {
    if (!resource.accountID || !resource.resourceID) continue;
    const key = relayCredentialKey(resource.accountID, resource.resourceID);
    if (!resourceByKey.has(key)) resourceByKey.set(key, resource);
  }
  const slotByRelayKey = new Map<string, CredentialSlot>();
  const relayKeyOf = (slot: CredentialSlot): string => {
    const accountID = slot.source.accountID ?? "";
    const resourceID = slot.source.resourceID ?? "";
    // A slot that names no account or no resource is not a relay credential the
    // station could answer for; it keeps its slot identity so that two such
    // slots stay two rows.
    return accountID && resourceID ? relayCredentialKey(accountID, resourceID) : "";
  };
  for (const slot of slots) {
    if (slot.managed || slot.source.kind !== "relay") continue;
    const key = relayKeyOf(slot);
    if (key && !slotByRelayKey.has(key)) slotByRelayKey.set(key, slot);
  }

  const keys: ProviderCredentialKey[] = [];
  // The user's own keys come first, as the table lists them.
  for (const slot of slots) {
    if (slot.managed || slot.source.kind !== "independent") continue;
    keys.push({ key: `slot:${slot.id}`, kind: "independent", slotID: slot.id, name: slot.name });
  }
  // Then the station's, in the order the station lists them: the row a slot
  // already holds is that same row, not a second one.
  for (const [key, resource] of resourceByKey) {
    const slot = slotByRelayKey.get(key);
    keys.push({
      key,
      kind: "relay",
      ...(slot ? { slotID: slot.id } : {}),
      resource,
      name: resource.name,
    });
  }
  // Finally the slots whose resource the station dropped.  The table draws
  // these under their account so they can be repaired; the picker offers them
  // because the key the provider holds is still a key it can ask with.
  for (const slot of slots) {
    if (slot.managed || slot.source.kind !== "relay") continue;
    const key = relayKeyOf(slot);
    if (!key) {
      keys.push({ key: `slot:${slot.id}`, kind: "relay", slotID: slot.id, name: slot.name });
      continue;
    }
    // The station still lists it: the row above is this slot's own row.
    if (resourceByKey.has(key)) continue;
    // Two slots on one dropped resource are one credential; the first is the
    // one the repair acts on.
    if (slotByRelayKey.get(key) !== slot) continue;
    keys.push({ key, kind: "relay", slotID: slot.id, name: slot.name });
  }
  return keys;
}
