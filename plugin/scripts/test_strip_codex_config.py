#!/usr/bin/env python3
# Tests strip-codex-config.py — the uninstaller's ~/.codex/config.toml rewrite.
#
# The thing that must NOT happen is collateral damage: this file rewrites a config that also holds
# the user's project trust levels, their own MCP servers, and other plugins/marketplaces. Every case
# below is really an assertion about what SURVIVES.
# Run: python3 plugin/scripts/test_strip_codex_config.py
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("strip_codex_config", os.path.join(HERE, "strip-codex-config.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

KEY = "medley@medley-dev"
MARKET = "medley-dev"

# A realistic config: real trust entries, a user MCP server, a foreign plugin, probe debris, and the
# full set of medley tables — mirroring the shape of the live ~/.codex/config.toml.
FULL = """\
[projects."/Users/x/repo"]
trust_level = "trusted"

[mcp_servers.obsidian-vault]
command = "npx"
args = ["-y", "@bitbonsai/mcpvault@latest", "~/vault"]

[hooks.state]

[hooks.state."other-plugin@other-market:hooks/hooks.json:session_start:0:0"]
trusted_hash = "sha256:keepme"

[hooks.state."medley@medley-dev:hooks/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:aaa"

[hooks.state."medley@medley-dev:hooks/hooks.json:session_start:0:0"]
trusted_hash = "sha256:bbb"

[marketplaces.medley-dev]
last_updated = "2026-07-27T22:17:31Z"
source_type = "local"
source = "/Users/x/medley-dev"

[marketplaces.other-market]
source_type = "git"

[plugins."medley@medley-dev"]
enabled = true

[plugins."medley@medley-dev".mcp_servers.medley.tools.contract_set]
approval_mode = "approve"

[plugins."other@other-market"]
enabled = true
"""


class StripLogic(unittest.TestCase):
    def strip(self, text):
        return mod.strip(text, KEY, MARKET)

    def test_removes_all_three_families(self):
        new, removed = self.strip(FULL)
        # 2 hooks.state + 1 marketplace + 2 plugins tables = 5
        self.assertEqual(removed, 5)
        self.assertNotIn("medley@medley-dev", new)
        self.assertNotIn("[marketplaces.medley-dev]", new)

    def test_preserves_unrelated_tables(self):
        new, _ = self.strip(FULL)
        for keep in (
            '[projects."/Users/x/repo"]',
            "[mcp_servers.obsidian-vault]",
            '[hooks.state."other-plugin@other-market:hooks/hooks.json:session_start:0:0"]',
            "sha256:keepme",
            "[marketplaces.other-market]",
            '[plugins."other@other-market"]',
        ):
            self.assertIn(keep, new, "clobbered an unrelated entry: %s" % keep)

    def test_preserves_bare_hooks_state_parent(self):
        # `[hooks.state]` is the parent table; removing it would orphan every other plugin's hashes.
        new, _ = self.strip(FULL)
        self.assertIn("[hooks.state]\n", new)

    def test_body_lines_of_removed_tables_are_dropped(self):
        new, _ = self.strip(FULL)
        for gone in ("sha256:aaa", "sha256:bbb", "/Users/x/medley-dev", 'approval_mode = "approve"'):
            self.assertNotIn(gone, new)

    def test_similar_plugin_name_is_not_swept_up(self):
        # A different plugin whose key merely starts with ours must survive.
        text = '[plugins."medley@medley-dev-2"]\nenabled = true\n'
        new, removed = self.strip(text)
        self.assertEqual(removed, 0)
        self.assertEqual(new, text)

    def test_similar_marketplace_name_is_not_swept_up(self):
        text = "[marketplaces.medley-dev-2]\nsource_type = \"git\"\n"
        new, removed = self.strip(text)
        self.assertEqual(removed, 0)
        self.assertEqual(new, text)

    def test_noop_when_nothing_matches(self):
        text = '[projects."/tmp/x"]\ntrust_level = "trusted"\n'
        new, removed = self.strip(text)
        self.assertEqual(removed, 0)
        self.assertEqual(new, text)

    def test_trailing_table_removed_cleanly(self):
        # Ours is the LAST table — the dropping state must not swallow anything after EOF.
        text = '[projects."/tmp/x"]\ntrust_level = "trusted"\n\n[plugins."medley@medley-dev"]\nenabled = true\n'
        new, removed = self.strip(text)
        self.assertEqual(removed, 1)
        self.assertIn("trust_level", new)
        self.assertNotIn("medley", new)


class StripFile(unittest.TestCase):
    """End-to-end through main(), which is what uninstall.sh actually invokes."""

    def write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(
            lambda: [os.unlink(p) for p in (path, path + ".medley.bak") if os.path.exists(p)]
        )
        return path

    def run_main(self, path):
        """main() reads sys.argv, so drive it the way the shell does."""
        argv = sys.argv
        sys.argv = ["strip-codex-config.py", path, KEY, MARKET]
        try:
            return mod.main()
        finally:
            sys.argv = argv

    def test_writes_backup_and_rewrites(self):
        path = self.write(FULL)
        self.assertEqual(self.run_main(path), 0)
        self.assertTrue(os.path.exists(path + ".medley.bak"), "no backup written")
        with open(path + ".medley.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), FULL, "backup is not the original")
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("medley@medley-dev", fh.read())

    def test_no_backup_when_nothing_removed(self):
        path = self.write('[projects."/tmp/x"]\ntrust_level = "trusted"\n')
        self.assertEqual(self.run_main(path), 0)
        self.assertFalse(os.path.exists(path + ".medley.bak"), "backup written for a no-op")

    def test_missing_file_is_an_error_not_a_crash(self):
        self.assertEqual(self.run_main("/nonexistent/config.toml"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
