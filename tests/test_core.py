"""Tests for core AppleScript execution helpers."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from apple_mail_mcp import core


class RunAppleScriptTests(unittest.TestCase):
    def test_kills_process_on_timeout(self):
        # Apple Mail's Apple-Events bridge is single-threaded. If osascript is
        # left running after a Python-level timeout, it holds the event queue
        # and every subsequent MCP call hangs. Pin the kill+drain behavior.
        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="osascript", timeout=1),
            (b"", b""),
        ]
        with patch("apple_mail_mcp.core.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(Exception, "timed out"):
                core.run_applescript("return 1", timeout=1)

        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
