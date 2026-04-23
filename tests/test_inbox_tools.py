"""Tests for list_inbox_emails filtering (date, read-status, account)."""

import unittest
from unittest.mock import patch

from apple_mail_mcp.tools import inbox as inbox_tools


class ListInboxEmailsFilterTests(unittest.TestCase):
    def _capture_script(self, **kwargs):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.inbox.run_applescript", side_effect=fake_run):
            inbox_tools.list_inbox_emails(**kwargs)
        return captured["script"]

    def test_date_from_and_to_build_prescan_whose_clause(self):
        # Date filtering must run as an AppleScript `whose` pre-filter so Mail
        # doesn't iterate the whole mailbox first on large accounts.
        script = self._capture_script(date_from="2026-03-01", date_to="2026-03-07")
        self.assertIn("set year of fromDate to 2026", script)
        self.assertIn("set month of fromDate to March", script)
        self.assertIn("set year of toDate to 2026", script)
        self.assertIn("date received >= fromDate", script)
        self.assertIn("date received <= toDate", script)
        self.assertIn("every message of inboxMailbox whose", script)

    def test_include_read_false_moves_into_whose_clause(self):
        # Previously evaluated per-message in Python-ish AppleScript; must now
        # be part of the `whose` pre-filter so Mail can skip fetching reads.
        script = self._capture_script(include_read=False)
        self.assertIn("whose read status is false", script)

    def test_text_variant_honors_account_filter(self):
        # Regression: the text output previously ignored `account` entirely
        # and iterated every account.
        script = self._capture_script(account="Work")
        self.assertIn('if accountName is "Work" then', script)

    def test_invalid_date_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            inbox_tools.list_inbox_emails(date_from="not-a-date")


if __name__ == "__main__":
    unittest.main()
