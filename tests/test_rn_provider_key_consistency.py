"""The key table and 获取模型's picker must list one set of credentials.

A provider pane draws both: the 密钥 table under it, and the key picker beside
获取模型.  They were derived apart — the picker matched station keys by the
provider's Base URL, the table listed the keys of the station the provider is
bound to — so one pane could show two keys and offer six, four of which no row
answered for.  Both now read one derivation; these assertions hold that wiring
in place.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI = (REPO / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
MODULE = (REPO / "rn/packages/shared/src/ui/providerCredentials.ts").read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class ProviderKeyConsistencyTests(unittest.TestCase):
    def test_the_shared_derivation_is_the_one_place_both_surfaces_read(self) -> None:
        self.assertIn("export function providerCredentialKeys(", MODULE)
        self.assertIn("export function relayCredentialKey(", MODULE)
        # Core's own slot is not a credential the user holds.
        self.assertIn("if (slot.managed || slot.source.kind !== \"independent\") continue;", MODULE)

    def test_the_picker_offers_the_providers_own_station_keys(self) -> None:
        workspace = section(UI, "function ProviderWorkspace(", "function ProviderKeysPanel(")
        self.assertIn("const selectedStationAccounts = stationAccountsFor(provider);", workspace)
        # Built from the accounts the table draws, with the table's own
        # visibility rule, so every offered entry has a row above it.
        self.assertIn("selectedStationAccounts.flatMap((account) =>", workspace)
        self.assertIn("providedKeyRows([account], translate)", workspace)
        self.assertIn("providerKeyChoices(provider, providerKeySources)", workspace)
        # The snapshot's whole station list is no longer what the picker reads.
        self.assertNotIn("providerKeyChoices(provider, relaySources", workspace)
        self.assertNotIn("relaySourcesForBaseUrl", UI)

    def test_the_picker_no_longer_hides_a_key_the_table_lists(self) -> None:
        # A slot whose station resource is gone is drawn by the table as an
        # orphan and is fetchable with the key the document holds, so the
        # picker may not drop it.
        self.assertNotIn("const fetchable = ", UI)
        workspace = section(UI, "function ProviderWorkspace(", "function ProviderKeysPanel(")
        self.assertIn("const choices = providerKeyChoices(provider, providerKeySources);", workspace)

    def test_the_key_table_reads_the_same_derivation(self) -> None:
        panel = section(UI, "function ProviderKeysPanel(", "\nfunction ")
        self.assertIn("providerCredentialKeys(keySlots, providedRows.map((row) => ({", panel)
        self.assertIn('entry.kind === "independent" && slot', panel)
        self.assertIn('entry.kind === "relay" && entry.resource === undefined && slot', panel)
        # The rows themselves are still the ones the editors act on.
        self.assertIn("const providedRows = useMemo(() => providedKeyRows(stationAccounts, translate)", panel)

    def test_the_shared_funnel_resolves_a_slot_and_its_resource_to_one_credential(self) -> None:
        choices = section(UI, "function providerKeyChoices(", "\nfunction ")
        self.assertIn("providerCredentialKeys(keyStates,", choices)
        self.assertIn("relayCredentialKey(source.accountID, source.resourceID)", choices)
        self.assertNotIn("stationOriginKey", choices)
        self.assertNotIn("matchingRelaySources", choices)

    def test_a_surface_without_the_accounts_still_scopes_by_the_bound_station(self) -> None:
        helper = section(UI, "function relaySourcesForProvider(", "\nfunction ")
        self.assertIn('if (providerKind(provider) !== "relay") return [];', helper)
        self.assertIn('const stationID = stringValue(provider.relay_station_id).trim();', helper)
        self.assertIn("relaySources.filter((source) => source.stationID === stationID)", helper)


if __name__ == "__main__":
    unittest.main()
