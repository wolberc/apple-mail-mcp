"""Tests for get_needs_response date/unread pre-scan filtering."""

import unittest
from unittest.mock import patch

from apple_mail_mcp.tools import smart_inbox as smart_inbox_tools


class GetNeedsResponsePrescanTests(unittest.TestCase):
    def _capture_script(self, **kwargs):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        kwargs.setdefault("account", "Work")
        with patch(
            "apple_mail_mcp.tools.smart_inbox.run_applescript", side_effect=fake_run
        ):
            smart_inbox_tools.get_needs_response(**kwargs)
        return captured["script"]

    def test_date_from_builds_prescan_whose_clause(self):
        # Date filtering must run as an AppleScript `whose` pre-filter so Mail
        # doesn't iterate the whole mailbox first on large accounts.
        script = self._capture_script(date_from="2026-03-01")
        self.assertIn("set year of fromDate to 2026", script)
        self.assertIn("set month of fromDate to March", script)
        self.assertIn("set day of fromDate to 1", script)
        self.assertIn("every message of targetMailbox whose", script)
        self.assertIn("date received >= fromDate", script)

    def test_unread_filter_moves_into_whose_clause(self):
        # The heuristic only ever looks at unread mail; Mail must skip fetching
        # read messages rather than us testing read status per message.
        script = self._capture_script()
        self.assertIn(
            "every message of targetMailbox whose read status is false", script
        )

    def test_days_back_becomes_prescan_cutoff(self):
        script = self._capture_script(days_back=14)
        self.assertIn("set cutoffDate to (current date) - (14 * days)", script)
        self.assertIn("date received >= cutoffDate", script)

    def test_date_from_overrides_days_back_cutoff(self):
        # Both bounds in one `whose` would truncate a date_from window that
        # reaches further back than days_back.
        script = self._capture_script(date_from="2026-03-01", days_back=7)
        self.assertIn("date received >= fromDate", script)
        self.assertNotIn("date received >= cutoffDate", script)

    def test_no_in_loop_cutoff_break(self):
        # The old `exit repeat` assumed newest-first ordering and truncated
        # results; the bound now lives in the whose-clause.
        script = self._capture_script(days_back=7)
        self.assertNotIn("if messageDate < cutoffDate then exit repeat", script)

    def test_days_back_zero_drops_the_date_bound(self):
        script = self._capture_script(days_back=0)
        self.assertNotIn("date received >=", script)
        self.assertIn(
            "every message of targetMailbox whose read status is false", script
        )

    def test_invalid_date_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            smart_inbox_tools.get_needs_response(account="Work", date_from="not-a-date")


if __name__ == "__main__":
    unittest.main()
