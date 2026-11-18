"""One datum, one source.

A derived value has exactly one producer in this repository and every surface
calls it.  A second copy of the *rule* is a defect even while the two copies
agree: the next fix lands in one of them and the surfaces start naming the same
thing differently.  wawapii served six keys in no group; the key table was
taught to name such a key by its own name, while the 获取模型 picker went on
deriving the name from the row's raw ``group`` field and printed
``riveryang6/未分组`` directly above ``riveryang6/Gemini-Ultra``.

The checks here are mechanical on purpose.  Each entry names the value, the file
allowed to produce it, and a pattern that must not appear anywhere else — so a
second copy fails on the day it is written rather than the day its wording
drifts.  When a new derived value gets a producer, add its entry; when a new
surface needs the value, call the producer and leave this table alone.

Legitimate repetition is not checked, because it is not the same thing: a wire
contract mirrored in Swift and C++ (the four-key login answer), one row per
language in the i18n tables, ``config.dump`` rebuilding ``extra`` from the
``provider_type``/``relay_station_id`` pair.  Those copy *data across a
boundary*; what is banned is copying *the rule that decides the data*.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "rn/packages/shared/src/ui"
I18N_DIR = ROOT / "rn/packages/shared/src/i18n"
PROVIDERS_DOMAIN = ROOT / "young_router/core/domains/providers_models.py"


def _ui_sources() -> list[tuple[Path, str]]:
    paths = [*UI_DIR.rglob("*.tsx"), *UI_DIR.rglob("*.ts")]
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(paths)]


def _matches(pattern: str, text: str) -> list[tuple[int, str]]:
    """Every line of ``text`` matching ``pattern``, with its 1-based number."""

    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if re.search(pattern, line):
            found.append((number, line.strip()))
    return found


@dataclass(frozen=True)
class Producer:
    """One derived value, the only place that computes it, and its signature."""

    value: str
    owner: Path
    # What the owner does.  Everything matching this outside the owner is a
    # second copy of the rule.
    pattern: str


class OneDerivedValueOneProducerTests(unittest.TestCase):
    """Derived display values are computed once and called everywhere."""

    # Each entry is a rule, not a string: it fails on the second copy of the
    # derivation no matter what the copy happens to spell.
    PRODUCERS = (
        Producer(
            "a provided key's display name (its group, or its own name when it has none)",
            UI_DIR / "providedKeyName.ts",
            r"resource\.name \|\| resource\.apiName",
        ),
        Producer(
            "the word a key with no group falls back to",
            UI_DIR / "providedKeyName.ts",
            r'resource\.(?:name|apiName).*translate\("relay\.apiKeyUngrouped"\)',
        ),
    )

    def test_no_ui_surface_recomputes_a_derived_name(self) -> None:
        sources = _ui_sources()
        self.assertTrue(sources, "no UI sources found")
        for producer in self.PRODUCERS:
            offenders: list[str] = []
            for path, text in sources:
                hits = _matches(producer.pattern, text)
                if path == producer.owner:
                    continue
                offenders.extend(f"{path.relative_to(ROOT)}:{number}: {line}" for number, line in hits)
            self.assertEqual(
                [],
                offenders,
                f"{producer.value} is produced only in {producer.owner.name}; "
                "these lines derive it somewhere else",
            )

    def test_a_producer_is_defined_once(self) -> None:
        """``providedKeyName`` has one definition and the surface list calls it.

        A helper copied into the second component is the same defect as an
        inline rule: the copy answers `name` for a while, then stops.
        """

        definitions: list[str] = []
        calls: list[str] = []
        for path, text in _ui_sources():
            # The file, not the line: an unrelated edit above them must not
            # turn this test red, and a second copy is what it is for.
            definitions.extend(path.name for _ in _matches(r"export function providedKeyName\(", text))
            calls.extend(path.name for _ in _matches(r"providedKeyName\(row\.resource", text))
        self.assertEqual(["providedKeyName.ts"], definitions)
        self.assertEqual(["YoungRouterApp.tsx"], calls)

    def test_the_ungrouped_fallback_stays_with_the_key_and_group_producers(self) -> None:
        """The key helper and group controls own the ungrouped label.

        The group controls also use this translation for an empty group option.
        That is distinct from deriving the display name of an ungrouped key.
        """

        for path, text in _ui_sources():
            if path.name in {"providedKeyName.ts", "RelayAccountManager.tsx"}:
                continue
            for number, line in _matches(r"relay\.apiKeyUngrouped", text):
                self.fail(f"{path.relative_to(ROOT)}:{number} names the ungrouped bucket again: {line}")

    def test_a_key_label_is_composed_in_exactly_one_place(self) -> None:
        """The 账号/名字 label has one producer: `providedKeyLabel`.

        Every row that names a relay key composes those two halves -- the key
        table, a row's live rename draft, and a slot the station no longer
        lists.  While each composed them itself a surface could carry the
        account and another could drop it, and a dropped slot read `grok`
        beside `xmfwhu/grok` as though the provider held two keys.
        """

        compositions: list[tuple[Path, int, str]] = []
        for path, text in _ui_sources():
            for number, line in _matches(r"\$\{accountKeyPrefix\(", text):
                if "/${" in line:
                    compositions.append((path, number, line))
        self.assertEqual(
            ["RelayAccountManager.tsx"],
            [path.name for path, _, _ in compositions],
            f"unexpected label compositions: {[(p.name, n) for p, n, _ in compositions]}",
        )
        # The consumers ask the producer, including the two that used to spell
        # it out (a draft, and a slot whose station key is gone).
        app = (ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        self.assertIn("providedKeyLabel(row.account, draft, translate)", app)
        self.assertIn("providedKeyLabel(account, slot.name, translate)", app)

    def test_the_model_group_order_has_one_producer(self) -> None:
        """Where a key group sits is decided once, in ``modelGroupOrder.ts``.

        The list used to take its groups in the order the *routes* introduced
        them — a `Map` filled while walking the models and then iterated in
        insertion order — so deleting a key's first route moved that key's whole
        group to the bottom of the pane.  A second copy of that ordering (a
        sort in a component, or another map read the same way) brings the jump
        back the moment the two disagree.
        """

        definitions: list[str] = []
        calls: list[str] = []
        for path, text in _ui_sources():
            definitions.extend(path.name for _ in _matches(r"export function orderedModelGroupIDs\(", text))
            calls.extend(path.name for _ in _matches(r"orderedModelGroupIDs\(", text))
        self.assertEqual(["modelGroupOrder.ts"], definitions)
        # One definition site plus the one caller; a third line is a second
        # surface deciding the order for itself.
        self.assertEqual(["YoungRouterApp.tsx", "modelGroupOrder.ts"], calls)
        # No component sorts the groups itself: the only `.sort(` on a group
        # map in the pane would be that second rule.
        for path, text in _ui_sources():
            for number, line in _matches(r"for \(const \[keyID, group\] of grouped", text):
                self.fail(f"{path.relative_to(ROOT)}:{number} iterates groups in route order: {line}")

    def test_every_resource_label_is_a_pass_through_of_its_producer(self) -> None:
        """``resourceLabel`` is filled from a producer, never assembled locally.

        The picker's entry is built from a provided-key row and used to put that
        row's raw ``group`` field there while the table beside it had already
        been fixed — the same key under two names in one window.
        """

        allowed = (
            "resourceLabel: stringValue(resource.api_name, stringValue(resource.name, resourceID)),",
            "resourceLabel: providedKeyName(row.resource, account.groups, translate),",
            "resourceLabel: string;",
        )
        offenders: list[str] = []
        seen = 0
        for path, text in _ui_sources():
            for number, line in _matches(r"^\s*resourceLabel:", text):
                seen += 1
                if line not in allowed:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line}")
        self.assertEqual([], offenders, "resourceLabel is assembled outside its producer")
        # Both the snapshot adapter and the shared label helper declare the
        # source type; the other two sites produce values.
        self.assertEqual(4, seen, "a resourceLabel site appeared or disappeared; update this table")


class OneFactOneSetterTests(unittest.TestCase):
    """Two fields that state one fact move together through one setter."""

    def test_the_provider_source_pair_is_written_only_by_its_setter(self) -> None:
        """``provider_type`` + ``relay_station_id`` have a single writer.

        The pair is one fact (which station this row came from) and the reader
        refuses a half: ``provider_type: relay`` with no station raised
        "Provider source is invalid" and the Apply after a delete refused.  A
        cleanup path restated ``provider_type`` after calling the setter, which
        is how the two copies start to disagree.
        """

        source = PROVIDERS_DOMAIN.read_text(encoding="utf-8")
        # The setter's own span: from its `def` to the next method of the class.
        start = source.index("    def _set_provider_source(")
        end = source.index("\n    def ", start + 10)
        setter_lines = source[:start].count("\n") + 1, source[:end].count("\n") + 1
        for field in ("provider_type", "relay_station_id"):
            writes = _matches(rf'provider\["{field}"\]\s*=', source)
            self.assertEqual(1, len(writes), f"{field} is written in {len(writes)} places: {writes}")
            number = writes[0][0]
            self.assertTrue(
                setter_lines[0] <= number <= setter_lines[1],
                f"{field} is written at line {number}, outside _set_provider_source "
                f"({setter_lines[0]}-{setter_lines[1]}): {writes[0][1]}",
            )

    def test_a_relay_login_result_cannot_state_the_balance(self) -> None:
        """Money has one writer: the refresh's profile read.

        A sign-in probe answers with its own ``quota``; writing that as the
        balance moved the number by tens of dollars seconds after login, because
        it is a different reading of a different number.
        """

        accounts = (ROOT / "young_router/core/domains/relay_accounts.py").read_text(encoding="utf-8")
        start = accounts.index("    def accept_login_result(")
        signature_end = accounts.index("    ) -> ", start)
        signature = accounts[start:signature_end]
        self.assertNotIn("quota:", signature, "accept_login_result takes the station's probe number again")
        end = accounts.index("\n    def ", start + 10)
        body = accounts[signature_end:end]
        # The docstring explains why the number is dropped; the write is what
        # would make it a second balance source.
        for pattern in (r'\bbalance\b\s*=', r'\["balance"\]\s*='):
            self.assertEqual([], _matches(pattern, body), "accept_login_result writes the balance again")


if __name__ == "__main__":
    unittest.main()
