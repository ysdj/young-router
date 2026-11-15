"""Edge cases for the image-context MCP server's JSON-RPC handling."""

from __future__ import annotations

import unittest

from young_router.proxy import image_context_mcp as mcp_module


class ImageContextMcpNotificationTests(unittest.TestCase):
    def test_a_tools_list_changed_notification_gets_no_answer(self) -> None:
        answer = mcp_module.handle(
            {"jsonrpc": "2.0", "method": "tools/list_changed"}
        )

        self.assertIsNone(answer)
