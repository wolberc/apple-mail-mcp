"""Tests for get_email tool, its output parser, and the attachment resource."""

import json
import unittest
from unittest.mock import patch

from apple_mail_mcp.tools import search as search_tools


def _labelled_payload(
    *,
    message_id="12345",
    internet_id="<abc@mail.example.com>",
    subject="Hello",
    sender="Alice <alice@example.com>",
    to="bob@example.com",
    cc="",
    reply_to="",
    date_received="2026-03-07T10:00:00",
    date_sent="2026-03-07T09:59:30",
    is_read="true",
    is_flagged="false",
    mailbox="INBOX",
    account="Work",
    attach_count=0,
    attachment_lines=None,
    body="Line 1\nLine 2",
):
    attachment_lines = attachment_lines or []
    attach_block = "\n".join(attachment_lines)
    return "\n".join(
        [
            f"MSG_ID:{message_id}",
            f"INTERNET_ID:{internet_id}",
            f"SUBJECT:{subject}",
            f"FROM:{sender}",
            f"TO:{to}",
            f"CC:{cc}",
            f"REPLY_TO:{reply_to}",
            f"DATE_RECV:{date_received}",
            f"DATE_SENT:{date_sent}",
            f"IS_READ:{is_read}",
            f"IS_FLAGGED:{is_flagged}",
            f"MAILBOX:{mailbox}",
            f"ACCOUNT:{account}",
            f"ATTACH_COUNT:{attach_count}",
            f"ATTACHMENTS:{attach_block}",
            "BODY_START",
            body,
            "BODY_END",
        ]
    )


class GetEmailInputValidationTests(unittest.TestCase):
    def test_returns_error_when_neither_id_provided(self):
        response = json.loads(search_tools.get_email())
        self.assertIn("Provide message_id or internet_message_id", response["error"])

    def test_returns_error_when_message_id_not_numeric(self):
        response = json.loads(search_tools.get_email(message_id="not-a-number"))
        self.assertIn("must be numeric", response["error"])


class GetEmailScriptBuildingTests(unittest.TestCase):
    def _capture(self, **kwargs):
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return "NOT_FOUND"

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run):
            search_tools.get_email(**kwargs)
        return captured["script"]

    def test_numeric_message_id_uses_unquoted_lookup(self):
        script = self._capture(message_id=12345)
        self.assertIn("whose id is 12345", script)

    def test_internet_message_id_is_quoted_and_escaped(self):
        script = self._capture(internet_message_id='<has"quote@x>')
        self.assertIn(r'whose message id is "<has\"quote@x>"', script)

    def test_account_and_mailbox_scope_narrow_search(self):
        script = self._capture(message_id=42, account="Work", mailbox="Archive")
        self.assertIn('{account "Work"}', script)
        self.assertIn('{mailbox "Archive" of targetAccount}', script)


class ParseGetEmailOutputTests(unittest.TestCase):
    def test_full_payload_parses_with_attachments_and_mail_link(self):
        raw = _labelled_payload(
            attach_count=2,
            attachment_lines=[
                "invoice.pdf|||24576|||application/pdf",
                "photo.jpg|||102400|||image/jpeg",
            ],
            body="Dear Bob,\n\nSee attached.\n\nAlice",
        )
        result = search_tools._parse_get_email_output(raw)

        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["message_id"], "12345")
        self.assertEqual(result["internet_message_id"], "<abc@mail.example.com>")
        self.assertTrue(result["is_read"])
        self.assertFalse(result["is_flagged"])
        self.assertEqual(result["attachment_count"], 2)
        self.assertEqual(result["body"], "Dear Bob,\n\nSee attached.\n\nAlice")
        self.assertEqual(
            [att["name"] for att in result["attachments"]],
            ["invoice.pdf", "photo.jpg"],
        )
        self.assertEqual(result["attachments"][0]["size_bytes"], 24576)
        self.assertEqual(
            result["mail_link"], "message://%3Cabc@mail.example.com%3E"
        )

    def test_mime_type_fallback_from_extension(self):
        raw = _labelled_payload(
            attach_count=1,
            attachment_lines=["report.pdf|||1024|||"],
        )
        result = search_tools._parse_get_email_output(raw)
        self.assertEqual(result["attachments"][0]["mime_type"], "application/pdf")

    def test_missing_internet_id_omits_mail_link(self):
        raw = _labelled_payload(internet_id="")
        result = search_tools._parse_get_email_output(raw)
        self.assertNotIn("mail_link", result)


class GetAttachmentResourceTests(unittest.TestCase):
    def test_rejects_non_numeric_message_id(self):
        with self.assertRaisesRegex(ValueError, "must be numeric"):
            search_tools.get_attachment_resource(message_id="abc", index=0)

    def test_rejects_non_integer_index(self):
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            search_tools.get_attachment_resource(message_id="12345", index="nope")

    def test_rejects_negative_index(self):
        with self.assertRaisesRegex(ValueError, ">= 0"):
            search_tools.get_attachment_resource(message_id="12345", index=-1)

    def test_index_zero_maps_to_applescript_item_one(self):
        # AppleScript lists are 1-based; a 0-based MCP index must become 1.
        captured = {}

        def fake_run(script, timeout=120):
            captured["script"] = script
            return ""

        with patch("apple_mail_mcp.tools.search.run_applescript", side_effect=fake_run), \
             patch("builtins.open", create=True) as mock_open, \
             patch("apple_mail_mcp.tools.search.os.unlink"):
            mock_open.return_value.__enter__.return_value.read.return_value = b"bytes"
            result = search_tools.get_attachment_resource(message_id="12345", index=0)

        self.assertEqual(result, b"bytes")
        self.assertIn("save item 1 of atts in POSIX file", captured["script"])
        self.assertIn("whose id is 12345", captured["script"])


if __name__ == "__main__":
    unittest.main()
