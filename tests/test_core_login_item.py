"""The login item a browser page's Core writes for the app it belongs to.

The desktop hosts register launch-at-login in their own process; the page has
none, so Core writes the operating system's per-user login item instead.  What is
worth pinning here is the boundary, not the plist: a Core that is not inside an
installed app must answer "nothing registered" rather than claim a login nobody
will ever run, and the macOS document must name the app by identifier so a moved
or reinstalled bundle keeps working.
"""

from __future__ import annotations

import plistlib
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

from young_router.core import login_item


class LoginItemTests(unittest.TestCase):
    def test_a_working_tree_has_no_app_to_register(self) -> None:
        # This suite runs from a checkout, which is not inside an app bundle.
        # A False answer is what makes the pane hand its preference back.
        self.assertFalse(login_item.available())
        self.assertFalse(login_item.set_login_item(True))

    def test_the_macos_document_opens_the_app_by_identifier(self) -> None:
        document = login_item._launch_agent_document()
        self.assertEqual(login_item.LOGIN_ITEM_ID, document["Label"])
        # `open -b` asks LaunchServices for the app, so a moved bundle stays
        # registered -- and opening one that is already running activates it
        # instead of starting a second copy beside the app's own SMAppService
        # registration.
        self.assertEqual(["/usr/bin/open", "-b", login_item.APP_BUNDLE_ID], document["ProgramArguments"])
        self.assertIs(True, document["RunAtLoad"])
        # A plist that cannot be read back is not a login item.
        self.assertEqual(document["Label"], plistlib.loads(plistlib.dumps(document))["Label"])

    def test_the_label_is_the_one_the_old_installers_used(self) -> None:
        # The Homebrew cask's uninstall stanza names this launchd job, so a
        # different label would leave a second, unremovable entry behind.
        cask = (Path(__file__).resolve().parents[1] / "Casks/young-router.rb").read_text(encoding="utf-8")
        self.assertIn(f'"{login_item.LOGIN_ITEM_ID}"', cask)

    @unittest.skipUnless(sys.platform == "darwin", "the LaunchAgent shape is macOS's")
    def test_registering_writes_the_agents_plist_and_removing_deletes_it(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.object(login_item, "app_bundle", return_value=Path("/Applications/Young Router.app")), \
             mock.patch.object(login_item, "_run_launchctl") as launchctl, \
             mock.patch.object(Path, "home", return_value=directory):
            self.assertTrue(login_item.set_login_item(True))
            plist = directory / "Library/LaunchAgents/young.router.login.plist"
            self.assertTrue(plist.is_file())
            self.assertEqual(login_item.LOGIN_ITEM_ID, plistlib.loads(plist.read_bytes())["Label"])
            # The file is what makes it a login item; bootstrapping only makes
            # it live for this session, and both calls name the same label.
            self.assertTrue(any("bootstrap" in call.args[0][0] for call in launchctl.call_args_list))
            self.assertTrue(login_item.set_login_item(False))
            self.assertFalse(plist.exists())
            self.assertTrue(any("bootout" in call.args[0][0] for call in launchctl.call_args_list))

    def test_a_bundle_with_no_plist_is_not_an_app(self) -> None:
        # `app_bundle` answers by shape -- Contents/Info.plist -- rather than by
        # a path this code assumes, so an uninstalled bundle registers nothing.
        with mock.patch.object(login_item, "_core_root", return_value=Path("/tmp/somewhere/Core")):
            self.assertIsNone(login_item.app_bundle())
