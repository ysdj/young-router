from __future__ import annotations

from hook_test_utils import *


class HookWebSearchProtocolTests(HookTestCase):
    async def test_inline_tool_tag_explanation_is_not_truncated(self):
        hooks, _ = load_hook_module()
        text = "The filter only knows " + chr(96) + "<tool_call>" + chr(96) + "; this is an explanation."
        text_filter = hooks._RawToolCallTextFilter()
        self.assertEqual("".join(text_filter.consume(char) for char in text), text)
        self.assertEqual(hooks._strip_raw_tool_call_blocks(text), text)
        self.assertEqual(hooks._strip_raw_tool_call_blocks("."), ".")

    def test_fenced_dsml_example_is_not_a_tool_call(self):
        hooks, _ = load_hook_module()
        marker = "\uff5c\uff5cDSML\uff5c\uff5c"
        raw = "<" + marker + ' calls>\n<' + marker + ' invoke name="save_report">\n</' + marker + ' invoke>\n</' + marker + ' calls>'
        text = chr(96) * 3 + "xml\n" + raw + "\n" + chr(96) * 3
        self.assertEqual(hooks._raw_dsml_tool_call_items(text), [])
        self.assertEqual(hooks._strip_raw_tool_call_blocks(text), text)
