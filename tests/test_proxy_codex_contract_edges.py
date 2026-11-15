from hook_test_utils import *


class CodexContractEdgesTests(HookTestCase):
    def test_minimal_effort_is_the_same_answer_in_both_spellings(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module

        self.assertFalse(contract._request_thinks({"reasoning": "minimal"}))
        self.assertFalse(contract._request_thinks({"reasoning": {"effort": "minimal"}}))
        # A level above it still asks for thinking, so the two branches keep
        # their shared meaning rather than agreeing on False for everything.
        self.assertTrue(contract._request_thinks({"reasoning": "low"}))
        self.assertTrue(contract._request_thinks({"reasoning": {"effort": "low"}}))

    def test_minimal_effort_reads_through_case_and_surrounding_space(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module

        self.assertFalse(contract._request_thinks({"reasoning": " Minimal "}))
        self.assertFalse(contract._request_thinks({"reasoning": {"effort": " MINIMAL "}}))
