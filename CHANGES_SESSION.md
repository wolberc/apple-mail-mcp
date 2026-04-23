# Session Changes — Apple Mail MCP

All changes are committed to branch `full_changes` on fork `git@github.com:wolberc/apple-mail-mcp.git`.
Three files were modified: `plugin/apple_mail_mcp/core.py`, `plugin/apple_mail_mcp/tools/inbox.py`, `plugin/apple_mail_mcp/tools/search.py`.

---

## 1. Pre-scan date filtering on `list_inbox_emails`

**Problem:** `list_inbox_emails` had no date filtering at all. It fetched every message in the inbox and iterated them all before any filtering was applied. On large mailboxes this caused timeouts. The goal was to be able to chunk results by day (e.g. pass `date_from="2024-04-22"` and `date_to="2024-04-22"` to get just one day's worth of emails) without timing out.

**What was done:**
- Added `date_from: Optional[str] = None` and `date_to: Optional[str] = None` parameters (ISO `YYYY-MM-DD`) to `list_inbox_emails` (both text and JSON output variants).
- These are applied as an AppleScript `whose` clause **before** Mail fetches any messages — i.e. `every message of inboxMailbox whose date received >= fromDate and date received <= toDate` — so the OS-level filter runs rather than a post-scan Python loop.
- `include_read=False` was also moved into the `whose` clause (was previously a per-message check inside the loop).
- The `account` filter in the text output variant was also fixed — it was previously ignored entirely.

**Shared helper added to `core.py`:** `build_applescript_date(var_name, date_value, end_of_day)` — generates the AppleScript snippet to declare a date variable from an ISO string. This was previously a private function inside `search.py` (`_build_applescript_date`); it was promoted to `core.py` so both `inbox.py` and `search.py` can use it.

---

## 2. Expose message IDs in `list_inbox_emails` JSON output

**Problem:** `list_inbox_emails` with `output_format="json"` returned only subject, sender, date, is_read, account — no IDs. This made it impossible to use the listing tool as a first step and then look up or act on specific messages by ID.

**What was done:**
- Extended the pipe-delimited AppleScript output from 5 fields to 7 fields: added `message_id` (Apple Mail's internal numeric ID, `id of aMessage`) and `internet_message_id` (RFC 2822 Message-ID header, `message id of aMessage`).
- Updated `_parse_pipe_delimited_emails` to parse the two new fields. Old 5-field lines still parse correctly (backwards compatible).
- `mail_link` (`message://` deep link) is computed from `internet_message_id` in the parser, same as `search_emails` already does.

---

## 3. New `get_email` tool — fetch a single email by ID with full detail

**Problem:** There was no way to retrieve the full content of a specific email by ID. `search_emails` lists emails with previews; `list_inbox_emails` gives minimal metadata. Nothing returned to/cc headers, full body, or an attachments list in one call.

**What was done:**
- Added `get_email(message_id, internet_message_id, account, mailbox)` tool to `search.py`.
- Accepts either `message_id` (Apple Mail numeric ID — fast `whose id is X` lookup) or `internet_message_id` (RFC 2822 header — stable across Mail database rebuilds).
- `account` and `mailbox` are optional hints to narrow the search scope; without them it searches all non-system mailboxes across all accounts.
- Returns JSON with: `subject`, `from`, `to`, `cc`, `reply_to`, `date_received`, `date_sent`, `is_read`, `is_flagged`, `mailbox`, `account`, `message_id`, `internet_message_id`, `mail_link`, `body` (full text), `attachments` (list of objects).
- Each attachment object contains: `name`, `size_bytes`, `mime_type`, `resource_uri`.

**Bugs found and fixed during testing:**
- `months` in AppleScript is a reserved word (treated as `every month`). Renamed to `monthValues` in the `on month_number` handler.
- `message_id` parameter typed as `Optional[str]` but MCP clients pass integers; FastMCP/Pydantic rejected them. Changed to `Optional[Union[str, int]]` with explicit string coercion at the top of the function.
- Attachment `size` property: the correct AppleScript property on `mail attachment` is `file size`, not `size`. Also, `for-in` repeat loops on AppleScript collections cause property access to be applied to the whole collection rather than individual items — fixed by switching to index-based loops (`repeat with i from 1 to count`).
- `size of anAtt as string` parsed by AppleScript as `size of (anAtt as string)` — the explicit two-step (`set attSizeNum to (file size of anAtt)` then `attSizeNum as string`) avoids this.
- Attachment list parser in Python only captured the first attachment line (multi-attachment emails showed `attachments: []`). Fixed by adding an `in_attachments` state flag that collects lines until `BODY_START`, mirroring the existing `in_body` logic.

---

## 4. MCP Resource for attachment binary retrieval

**Problem:** MCP tools can only return text/JSON. There was no way for a client to get the actual binary content of an attachment (e.g. download a PDF).

**What was done:**
- Registered an MCP resource at URI template `mail-attachment://{message_id}/{index}` using FastMCP's `@mcp.resource()` decorator.
- The handler finds the message by numeric `message_id` (whose-clause lookup), saves the attachment at `index` (0-based, converted to 1-based for AppleScript) to a `tempfile.mkstemp()` path using AppleScript's `save` command, reads the bytes, deletes the temp file (in a `finally` block), and returns `bytes`.
- FastMCP automatically base64-encodes the bytes and sends them as `BlobResourceContents` to the client.
- Each attachment in `get_email`'s output includes `"resource_uri": "mail-attachment://<message_id>/<index>"` so clients have the URI ready without constructing it manually.
- `mime_type` for attachments: AppleScript's `mime type` property is often empty for Exchange/Outlook emails. Added a Python-side fallback using `mimetypes.guess_type(filename)`, with a final fallback of `application/octet-stream`.

---

## File summary

| File | What changed |
|------|-------------|
| `core.py` | Added `MONTH_NAMES`, `build_applescript_date()` (promoted from `search.py`) |
| `tools/inbox.py` | `list_inbox_emails`: added `date_from`/`date_to` pre-scan filtering, fixed `account` filter, moved `include_read` to whose-clause; JSON output now includes `message_id`, `internet_message_id`, `mail_link` |
| `tools/search.py` | Removed local `_build_applescript_date`; added `get_email` tool; added `get_attachment_resource` MCP resource; added `mimetypes` fallback for attachment MIME types; various AppleScript bug fixes |
