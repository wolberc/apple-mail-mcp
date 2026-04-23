"""Search tools: finding and filtering emails."""

import json
import mimetypes
import os
import re
import tempfile
from typing import Optional, List, Dict, Any, Union
from urllib.parse import quote

from apple_mail_mcp.server import mcp
from apple_mail_mcp.core import (
    build_applescript_date,
    contains_any_condition,
    inject_preferences,
    escape_applescript,
    normalize_search_terms,
    run_applescript,
    LOWERCASE_HANDLER,
)


def _parse_search_records(output: str) -> List[Dict[str, Any]]:
    """Parse structured search output into dict records."""
    if not output:
        return []

    records = []
    for line in output.splitlines():
        parts = line.split("|||", 8)
        if len(parts) < 8:
            continue

        internet_message_id = parts[1].strip()
        record = {
            "message_id": parts[0].strip(),
            "internet_message_id": internet_message_id,
            "subject": parts[2].strip(),
            "sender": parts[3].strip(),
            "mailbox": parts[4].strip(),
            "account": parts[5].strip(),
            "is_read": parts[6].strip().lower() == "true",
            "received_date": parts[7].strip(),
        }
        if internet_message_id:
            # Apple Mail requires: message:// scheme, angle brackets (percent-encoded),
            # and raw @ in the Message-ID. Normalize ID in case angle brackets are
            # present or missing (AppleScript returns both forms).
            msg_id = internet_message_id.strip("<>")
            record["mail_link"] = f"message://%3C{quote(msg_id, safe='@')}%3E"
        if len(parts) > 8 and parts[8].strip():
            record["content_preview"] = parts[8].strip()
        records.append(record)

    return records


def _sort_search_records(
    records: List[Dict[str, Any]], sort: str
) -> List[Dict[str, Any]]:
    """Sort records by received date."""
    reverse = sort == "date_desc"
    return sorted(
        records, key=lambda item: item.get("received_date", ""), reverse=reverse
    )


def _format_search_records_text(
    records: List[Dict[str, Any]],
    subject_only: bool = False,
) -> str:
    """Format search records as human-readable text."""
    lines = []

    if subject_only:
        lines.append("SUBJECT SEARCH RESULTS")
        lines.append("")
        for item in records:
            lines.append(f"- {item['subject']}")
    else:
        lines.append("SEARCH RESULTS")
        lines.append("")
        for item in records:
            indicator = "\u2713" if item["is_read"] else "\u2709"
            lines.append(f"{indicator} {item['subject']}")
            lines.append(f"   From: {item['sender']}")
            lines.append(f"   Date: {item['received_date']}")
            lines.append(f"   Mailbox: {item['mailbox']}")
            if item.get("content_preview"):
                lines.append(f"   Content: {item['content_preview']}")
            lines.append("")

    lines.append("========================================")
    lines.append(f"FOUND: {len(records)} matching email(s)")
    lines.append("========================================")
    return "\n".join(lines)


def _build_search_response(
    records: List[Dict[str, Any]],
    offset: int,
    limit: int,
    sort: str,
    output_format: str,
    subject_only: bool = False,
) -> str:
    """Return either JSON or text for search results."""
    sorted_records = _sort_search_records(records, sort)
    has_more = len(sorted_records) > limit
    items = sorted_records[:limit]
    next_offset = offset + len(items) if has_more else None

    if output_format == "json":
        return json.dumps(
            {
                "items": items,
                "offset": offset,
                "limit": limit,
                "returned": len(items),
                "has_more": has_more,
                "next_offset": next_offset,
                "sort": sort,
            }
        )

    return _format_search_records_text(items, subject_only=subject_only)


def _search_mail_records(
    account: Optional[str] = None,
    mailbox: str = "INBOX",
    subject_terms: Optional[List[str]] = None,
    sender: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    read_status: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_content: bool = False,
    content_length: int = 300,
    offset: int = 0,
    limit: int = 100,
    sort: str = "date_desc",
    body_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return structured search records from Apple Mail.

    When account is None, iterates all accounts.
    When body_text is provided, uses per-message iteration with case-insensitive
    content matching (slower than subject/sender-only searches).
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit <= 0:
        return []
    if sort not in {"date_desc", "date_asc"}:
        raise ValueError("Invalid sort. Use: date_desc, date_asc")
    if read_status not in {"all", "read", "unread"}:
        raise ValueError("Invalid read_status. Use: all, read, unread")

    escaped_sender = escape_applescript(sender) if sender else None

    # When body_text is provided, we must iterate per-message (can't use whose clause)
    use_body_search = body_text is not None

    # Build whose-clause filter conditions (only used when NOT doing body search)
    filter_conditions = []
    if not use_body_search:
        if subject_terms:
            filter_conditions.append(contains_any_condition("subject", subject_terms))
        if sender:
            filter_conditions.append(f'sender contains "{escaped_sender}"')
        if has_attachments is not None:
            if has_attachments:
                filter_conditions.append("(count of mail attachments) > 0")
            else:
                filter_conditions.append("(count of mail attachments) = 0")
        if read_status == "read":
            filter_conditions.append("read status is true")
        elif read_status == "unread":
            filter_conditions.append("read status is false")
        if date_from:
            filter_conditions.append("date received >= fromDate")
        if date_to:
            filter_conditions.append("date received <= toDate")

    if filter_conditions:
        matching_messages_script = f"set matchingMessages to every message of currentMailbox whose {' and '.join(filter_conditions)}"
    else:
        matching_messages_script = (
            "set matchingMessages to every message of currentMailbox"
        )

    if mailbox == "All":
        mailbox_script = """
                set searchMailboxes to every mailbox of targetAccount
        """
        skip_script = """
                        set skipFolders to {"Trash", "Junk", "Junk Email", "Deleted Items", "Sent", "Sent Items", "Sent Messages", "Drafts", "Spam", "Deleted Messages"}
                        repeat with skipFolder in skipFolders
                            if mailboxName is skipFolder then
                                set shouldSkip to true
                                exit repeat
                            end if
                        end repeat
        """
    else:
        escaped_mailbox = escape_applescript(mailbox)
        mailbox_script = f'''
                try
                    set searchMailbox to mailbox "{escaped_mailbox}" of targetAccount
                on error
                    if "{escaped_mailbox}" is "INBOX" then
                        set searchMailbox to mailbox "Inbox" of targetAccount
                    else
                        error "Mailbox not found: {escaped_mailbox}"
                    end if
                end try
                set searchMailboxes to {{searchMailbox}}
        '''
        skip_script = ""

    date_setup = build_applescript_date("fromDate", date_from)
    date_setup += build_applescript_date("toDate", date_to, end_of_day=True)

    # Build account iteration
    if account:
        escaped_account = escape_applescript(account)
        account_setup = f'''
                set searchAccounts to {{account "{escaped_account}"}}
        '''
    else:
        account_setup = """
                set searchAccounts to every account
        """

    # Build body search per-message filter block
    if use_body_search:
        escaped_body = escape_applescript(body_text.lower()) if body_text else ""
        # Build per-message conditions for subject, sender, read_status, dates, attachments
        per_msg_conditions = []
        if subject_terms:
            # Case-insensitive subject check
            subject_checks = " or ".join(
                f'lowerSubject contains "{escape_applescript(t.lower())}"'
                for t in subject_terms
            )
            per_msg_conditions.append(f"({subject_checks})")
        if sender:
            per_msg_conditions.append(f'lowerSender contains "{escape_applescript(sender.lower())}"')
        if read_status == "read":
            per_msg_conditions.append("messageRead is true")
        elif read_status == "unread":
            per_msg_conditions.append("messageRead is false")
        if date_from:
            per_msg_conditions.append("messageDate >= fromDate")
        if date_to:
            per_msg_conditions.append("messageDate <= toDate")
        if has_attachments is True:
            per_msg_conditions.append("(count of mail attachments of aMessage) > 0")
        elif has_attachments is False:
            per_msg_conditions.append("(count of mail attachments of aMessage) = 0")

        # Body text condition is always present in body search mode
        per_msg_conditions.append(f'lowerContent contains "{escaped_body}"')

        combined_condition = " and ".join(per_msg_conditions)

        body_search_loop = f'''
                            set matchingMessages to {{}}
                            set allMessages to every message of currentMailbox
                            repeat with aMessage in allMessages
                                if collectLimit <= 0 then exit repeat
                                try
                                    set messageSubject to subject of aMessage
                                    set messageSender to sender of aMessage
                                    set messageRead to read status of aMessage
                                    set messageDate to date received of aMessage
                                    set lowerSubject to my lowercase(messageSubject)
                                    set lowerSender to my lowercase(messageSender)
                                    set msgContent to ""
                                    try
                                        set msgContent to content of aMessage
                                    end try
                                    set lowerContent to my lowercase(msgContent)
                                    if {combined_condition} then
                                        set end of matchingMessages to aMessage
                                    end if
                                end try
                            end repeat
        '''
    else:
        body_search_loop = ""

    # Choose the message collection strategy
    if use_body_search:
        message_collection = body_search_loop
    else:
        message_collection = f"                            {matching_messages_script}"

    lowercase_handler = LOWERCASE_HANDLER if use_body_search else ""

    script = f'''
    {lowercase_handler}

    on sanitize_field(value)
        try
            set valueText to value as string
        on error
            set valueText to ""
        end try

        set AppleScript's text item delimiters to {{return, linefeed, tab}}
        set valueParts to text items of valueText
        set AppleScript's text item delimiters to " "
        set valueText to valueParts as string
        set AppleScript's text item delimiters to "|||"
        set valueParts to text items of valueText
        set AppleScript's text item delimiters to " | "
        set valueText to valueParts as string
        set AppleScript's text item delimiters to ""
        return valueText
    end sanitize_field

    on pad2(numberValue)
        if numberValue < 10 then
            return "0" & (numberValue as string)
        end if
        return numberValue as string
    end pad2

    on month_number(monthValue)
        set monthValues to {{January, February, March, April, May, June, July, August, September, October, November, December}}
        repeat with monthIndex from 1 to 12
            if item monthIndex of monthValues is monthValue then
                return monthIndex
            end if
        end repeat
        return 0
    end month_number

    on iso_datetime(dateValue)
        set yearValue to year of dateValue as integer
        set monthValue to my month_number(month of dateValue)
        set dayValue to day of dateValue as integer
        set hourValue to hours of dateValue
        set minuteValue to minutes of dateValue
        set secondValue to seconds of dateValue
        return (yearValue as string) & "-" & my pad2(monthValue) & "-" & my pad2(dayValue) & "T" & my pad2(hourValue) & ":" & my pad2(minuteValue) & ":" & my pad2(secondValue)
    end iso_datetime

    tell application "Mail"
        with timeout of 150 seconds
            try
                set recordLines to {{}}
                set offsetRemaining to {offset}
                set collectLimit to {limit + 1}
                {date_setup}
                {account_setup}

                repeat with targetAccount in searchAccounts
                    if collectLimit <= 0 then exit repeat
                    set accountName to my sanitize_field(name of targetAccount)
                    {mailbox_script}

                    repeat with currentMailbox in searchMailboxes
                        if collectLimit <= 0 then exit repeat

                        try
                            set mailboxName to my sanitize_field(name of currentMailbox)
                            set shouldSkip to false
                            {skip_script}

                            if not shouldSkip then
                                {message_collection}
                                set matchingCount to count of matchingMessages

                                if offsetRemaining >= matchingCount then
                                    set offsetRemaining to offsetRemaining - matchingCount
                                else
                                    set startIndex to offsetRemaining + 1
                                    set availableCount to matchingCount - offsetRemaining
                                    if availableCount > collectLimit then
                                        set endIndex to startIndex + collectLimit - 1
                                    else
                                        set endIndex to startIndex + availableCount - 1
                                    end if

                                    if endIndex >= startIndex then
                                        set targetMessages to items startIndex thru endIndex of matchingMessages

                                        repeat with aMessage in targetMessages
                                            try
                                                set messageId to my sanitize_field(id of aMessage)
                                                set internetMessageId to ""
                                                try
                                                    set internetMessageId to my sanitize_field(message id of aMessage)
                                                end try
                                                set messageSubject to my sanitize_field(subject of aMessage)
                                                set messageSender to my sanitize_field(sender of aMessage)
                                                set messageRead to read status of aMessage
                                                set messageDate to date received of aMessage
                                                set receivedAt to my iso_datetime(messageDate)
                                                set contentPreview to ""

                                                if {str(include_content).lower()} then
                                                    try
                                                        set msgContent to content of aMessage
                                                        set AppleScript's text item delimiters to {{return, linefeed, tab}}
                                                        set contentParts to text items of msgContent
                                                        set AppleScript's text item delimiters to " "
                                                        set cleanText to contentParts as string
                                                        set AppleScript's text item delimiters to ""
                                                        if {content_length} > 0 and length of cleanText > {content_length} then
                                                            set contentPreview to my sanitize_field(text 1 thru {content_length} of cleanText & "...")
                                                        else
                                                            set contentPreview to my sanitize_field(cleanText)
                                                        end if
                                                    on error
                                                        set contentPreview to ""
                                                    end try
                                                end if

                                                set readValue to "false"
                                                if messageRead then
                                                    set readValue to "true"
                                                end if

                                                set recordLine to messageId & "|||" & internetMessageId & "|||" & messageSubject & "|||" & messageSender & "|||" & mailboxName & "|||" & accountName & "|||" & readValue & "|||" & receivedAt & "|||" & contentPreview
                                                set end of recordLines to recordLine
                                                set collectLimit to collectLimit - 1
                                                if collectLimit <= 0 then exit repeat
                                            end try
                                        end repeat
                                    end if

                                    set offsetRemaining to 0
                                end if
                            end if
                        on error
                            -- Skip mailboxes that cannot be searched
                        end try
                    end repeat
                end repeat

                if (count of recordLines) is 0 then
                    return ""
                end if

                set AppleScript's text item delimiters to linefeed
                set outputText to recordLines as string
                set AppleScript's text item delimiters to ""
                return outputText
            on error errMsg
                return "ERROR|||" & errMsg
            end try
        end timeout
    end tell
    '''

    result = run_applescript(script, timeout=180)
    if result.startswith("ERROR|||"):
        raise ValueError(result.split("|||", 1)[1])

    return _parse_search_records(result)


@mcp.tool()
@inject_preferences
def search_emails(
    account: Optional[str] = None,
    mailbox: str = "INBOX",
    subject_keyword: Optional[str] = None,
    subject_keywords: Optional[List[str]] = None,
    sender: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    read_status: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_content: bool = False,
    max_content_length: int = 500,
    body_text: Optional[str] = None,
    max_results: Optional[int] = 20,
    output_format: str = "text",
    offset: int = 0,
    limit: Optional[int] = None,
    sort: str = "date_desc",
) -> str:
    """
    Unified search tool with JSON output, pagination, and real date filtering.

    Consolidates subject search, sender search, body content search, and
    cross-account search into a single tool.

    Args:
        account: Account name to search in (e.g., "Gmail", "Work").
            If None, searches ALL accounts (slower).
        mailbox: Mailbox to search (default: "INBOX", use "All" for all mailboxes, or specific folder name)
        subject_keyword: Optional keyword to search in subject
        subject_keywords: Optional list of subject keywords; matches any keyword
        sender: Optional sender email or name to filter by
        has_attachments: Optional filter for emails with attachments (True/False/None)
        read_status: Filter by read status: "all", "read", "unread" (default: "all")
        date_from: Optional start date filter (format: "YYYY-MM-DD")
        date_to: Optional end date filter (format: "YYYY-MM-DD")
        include_content: Whether to include email content preview (slower)
        max_content_length: Maximum content length in characters when include_content=True (default: 500, 0 = unlimited)
        body_text: Optional text to search for in email body content (case-insensitive).
            WARNING: body search is significantly slower as it reads each message body.
        max_results: Backward-compatible alias for limit
        output_format: Output format: "text" or "json" (default: "text")
        offset: Number of matching results to skip before returning data
        limit: Maximum number of results to return per page
        sort: Result sort order: "date_desc" or "date_asc"

    Returns:
        Formatted list of matching emails or JSON payload with stable message metadata
    """
    if output_format not in {"text", "json"}:
        return "Error: Invalid output_format. Use: text, json"

    if limit is None:
        limit = max_results if max_results is not None else 100

    subject_terms = normalize_search_terms(subject_keyword, subject_keywords)

    try:
        records = _search_mail_records(
            account=account,
            mailbox=mailbox,
            subject_terms=subject_terms,
            sender=sender,
            has_attachments=has_attachments,
            read_status=read_status,
            date_from=date_from,
            date_to=date_to,
            include_content=include_content,
            content_length=max_content_length,
            offset=offset,
            limit=limit,
            sort=sort,
            body_text=body_text,
        )
        return _build_search_response(
            records,
            offset=offset,
            limit=limit,
            sort=sort,
            output_format=output_format,
            subject_only=False,
        )
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
@inject_preferences
def get_email_thread(
    account: str, subject_keyword: str, mailbox: str = "INBOX", max_messages: int = 50
) -> str:
    """
    Get an email conversation thread - all messages with the same or similar subject.

    Args:
        account: Account name (e.g., "Gmail", "Work")
        subject_keyword: Keyword to identify the thread (e.g., "Re: Project Update")
        mailbox: Mailbox to search in (default: "INBOX", use "All" for all mailboxes)
        max_messages: Maximum number of thread messages to return (default: 50)

    Returns:
        Formatted thread view with all related messages sorted by date
    """

    # Escape user inputs for AppleScript
    escaped_account = escape_applescript(account)
    escaped_mailbox = escape_applescript(mailbox)

    # For thread detection, we'll strip common prefixes
    thread_keywords = ["Re:", "Fwd:", "FW:", "RE:", "Fw:"]
    cleaned_keyword = subject_keyword
    for prefix in thread_keywords:
        cleaned_keyword = cleaned_keyword.replace(prefix, "").strip()
    escaped_keyword = escape_applescript(cleaned_keyword)

    mailbox_script = f'''
        try
            set searchMailbox to mailbox "{escaped_mailbox}" of targetAccount
        on error
            if "{escaped_mailbox}" is "INBOX" then
                set searchMailbox to mailbox "Inbox" of targetAccount
            else if "{escaped_mailbox}" is "All" then
                set searchMailboxes to every mailbox of targetAccount
                set useAllMailboxes to true
            else
                error "Mailbox not found: {escaped_mailbox}"
            end if
        end try

        if "{escaped_mailbox}" is not "All" then
            set searchMailboxes to {{searchMailbox}}
            set useAllMailboxes to false
        end if
    '''

    script = f'''
    tell application "Mail"
        set outputText to "EMAIL THREAD VIEW" & return & return
        set outputText to outputText & "Thread topic: {escaped_keyword}" & return
        set outputText to outputText & "Account: {escaped_account}" & return & return
        set threadMessages to {{}}

        try
            set targetAccount to account "{escaped_account}"
            {mailbox_script}

            -- Collect all matching messages from all mailboxes
            repeat with currentMailbox in searchMailboxes
                set mailboxMessages to every message of currentMailbox

                repeat with aMessage in mailboxMessages
                    if (count of threadMessages) >= {max_messages} then exit repeat

                    try
                        set messageSubject to subject of aMessage

                        -- Remove common prefixes for matching
                        set cleanSubject to messageSubject
                        if cleanSubject starts with "Re: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if
                        if cleanSubject starts with "RE: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if
                        if cleanSubject starts with "Fwd: " then
                            set cleanSubject to text 6 thru -1 of cleanSubject
                        else if cleanSubject starts with "FW: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        else if cleanSubject starts with "Fw: " then
                            set cleanSubject to text 5 thru -1 of cleanSubject
                        end if

                        -- Check if this message is part of the thread
                        if cleanSubject contains "{escaped_keyword}" or messageSubject contains "{escaped_keyword}" then
                            set end of threadMessages to aMessage
                        end if
                    end try
                end repeat
            end repeat

            -- Display thread messages
            set messageCount to count of threadMessages
            set outputText to outputText & "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501" & return
            set outputText to outputText & "FOUND " & messageCount & " MESSAGE(S) IN THREAD" & return
            set outputText to outputText & "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501" & return & return

            repeat with aMessage in threadMessages
                try
                    set messageSubject to subject of aMessage
                    set messageSender to sender of aMessage
                    set messageDate to date received of aMessage
                    set messageRead to read status of aMessage

                    if messageRead then
                        set readIndicator to "\u2713"
                    else
                        set readIndicator to "\u2709"
                    end if

                    set outputText to outputText & readIndicator & " " & messageSubject & return
                    set outputText to outputText & "   From: " & messageSender & return
                    set outputText to outputText & "   Date: " & (messageDate as string) & return

                    -- Get content preview
                    try
                        set msgContent to content of aMessage
                        set AppleScript's text item delimiters to {{return, linefeed}}
                        set contentParts to text items of msgContent
                        set AppleScript's text item delimiters to " "
                        set cleanText to contentParts as string
                        set AppleScript's text item delimiters to ""

                        if length of cleanText > 150 then
                            set contentPreview to text 1 thru 150 of cleanText & "..."
                        else
                            set contentPreview to cleanText
                        end if

                        set outputText to outputText & "   Preview: " & contentPreview & return
                    end try

                    set outputText to outputText & return
                end try
            end repeat

        on error errMsg
            return "Error: " & errMsg
        end try

        return outputText
    end tell
    '''

    result = run_applescript(script)
    return result


@mcp.tool()
@inject_preferences
def get_email(
    message_id: Optional[Union[str, int]] = None,
    internet_message_id: Optional[str] = None,
    account: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> str:
    """
    Fetch a single email by ID with full details: headers, body, and attachments.

    Provide exactly one of message_id or internet_message_id. Both are returned
    by search_emails and list_inbox_emails (JSON output).

    Args:
        message_id: Apple Mail internal numeric ID (fast lookup, from search results).
        internet_message_id: RFC 2822 Message-ID header, e.g. "<abc@mail.gmail.com>"
                             (stable across database rebuilds, from search results).
        account: Optional account name to narrow the search scope (faster when known).
        mailbox: Optional mailbox name to narrow the search scope (fastest when known).
                 Defaults to searching all non-system mailboxes. Use "INBOX" for inbox only.

    Returns:
        JSON with subject, from, to, cc, reply_to, date_received, date_sent, is_read,
        is_flagged, mailbox, account, message_id, internet_message_id, mail_link,
        body (full text), and attachments (list of name, size_bytes, mime_type, resource_uri).
    """
    # MCP clients often pass numeric IDs as integers — FastMCP/Pydantic rejects
    # those for a plain Optional[str], so coerce here.
    if message_id is not None:
        message_id = str(message_id).strip()

    if not message_id and not internet_message_id:
        return json.dumps({"error": "Provide message_id or internet_message_id"})

    if message_id:
        if not message_id.isdigit():
            return json.dumps({"error": f"message_id must be numeric, got: {message_id!r}"})
        lookup_condition = f"id is {message_id}"
    else:
        escaped_mid = escape_applescript(internet_message_id.strip())
        lookup_condition = f'message id is "{escaped_mid}"'

    if account:
        account_setup = f'set searchAccounts to {{account "{escape_applescript(account)}"}}'
    else:
        account_setup = "set searchAccounts to every account"

    skip_folders = '{"Trash", "Junk", "Junk Email", "Deleted Items", "Sent", "Sent Items", "Sent Messages", "Drafts", "Spam", "Deleted Messages"}'
    if mailbox:
        escaped_mb = escape_applescript(mailbox)
        mailbox_setup = f'''
                try
                    set searchMailboxes to {{mailbox "{escaped_mb}" of targetAccount}}
                on error
                    if "{escaped_mb}" is "INBOX" then
                        set searchMailboxes to {{mailbox "Inbox" of targetAccount}}
                    else
                        set searchMailboxes to {{}}
                    end if
                end try
        '''
        mailbox_loop_open = "repeat with currentMailbox in searchMailboxes"
        mailbox_loop_skip = ""
        mailbox_loop_close = "end repeat"
    else:
        mailbox_setup = "set searchMailboxes to every mailbox of targetAccount"
        mailbox_loop_open = f"""
                    repeat with currentMailbox in searchMailboxes
                        set mbName to name of currentMailbox
                        if mbName is in {skip_folders} then
                        """
        mailbox_loop_skip = "else"
        mailbox_loop_close = """
                        end if
                    end repeat
        """

    script = f'''
    on format_recipients(recipientList)
        set result to ""
        repeat with aRecipient in recipientList
            set rName to name of aRecipient
            set rAddr to address of aRecipient
            if length of result > 0 then
                set result to result & ", "
            end if
            if rName is not "" then
                set result to result & rName & " <" & rAddr & ">"
            else
                set result to result & rAddr
            end if
        end repeat
        return result
    end format_recipients

    on pad2(n)
        if n < 10 then return "0" & (n as string)
        return n as string
    end pad2

    on month_number(m)
        -- `months` is an AppleScript reserved word (treated as `every month`).
        set monthValues to {{January, February, March, April, May, June, July, August, September, October, November, December}}
        repeat with i from 1 to 12
            if item i of monthValues is m then return i
        end repeat
        return 0
    end month_number

    on iso_datetime(d)
        set y to year of d as integer
        set mo to my month_number(month of d)
        set dy to day of d as integer
        set h to hours of d
        set mi to minutes of d
        set s to seconds of d
        return (y as string) & "-" & my pad2(mo) & "-" & my pad2(dy) & "T" & my pad2(h) & ":" & my pad2(mi) & ":" & my pad2(s)
    end iso_datetime

    tell application "Mail"
        with timeout of 60 seconds
            set foundMsg to missing value
            set foundMailboxName to ""
            set foundAccountName to ""
            {account_setup}

            repeat with targetAccount in searchAccounts
                if foundMsg is not missing value then exit repeat
                {mailbox_setup}
                {mailbox_loop_open}
                    {mailbox_loop_skip}
                    if foundMsg is missing value then
                        try
                            set matchingMsgs to every message of currentMailbox whose {lookup_condition}
                            if (count of matchingMsgs) > 0 then
                                set foundMsg to item 1 of matchingMsgs
                                set foundMailboxName to name of currentMailbox
                                set foundAccountName to name of targetAccount
                            end if
                        end try
                    end if
                    {mailbox_loop_close}
            end repeat

            if foundMsg is missing value then
                return "NOT_FOUND"
            end if

            set msgNumId to id of foundMsg as string
            set msgInternetId to ""
            try
                set msgInternetId to message id of foundMsg
            end try
            set msgSubject to subject of foundMsg
            set msgSender to sender of foundMsg
            set msgDateRecv to my iso_datetime(date received of foundMsg)
            set msgDateSent to ""
            try
                set msgDateSent to my iso_datetime(date sent of foundMsg)
            end try
            set msgRead to read status of foundMsg as string
            set msgFlagged to flagged status of foundMsg as string
            set msgReplyTo to ""
            try
                set msgReplyTo to reply to of foundMsg
            end try

            set msgTo to ""
            try
                set msgTo to my format_recipients(to recipients of foundMsg)
            end try
            set msgCc to ""
            try
                set msgCc to my format_recipients(cc recipients of foundMsg)
            end try

            set msgBody to ""
            try
                set msgBody to content of foundMsg
            end try

            -- Attachments: one line per attachment.
            -- Use index-based loop: for-in on Mail attachment collections applies
            -- the property access to the whole collection, not each item.
            -- The correct property is `file size`, not `size`.
            set attachLines to {{}}
            set realAttList to mail attachments of foundMsg
            set realAttCount to count of realAttList
            repeat with i from 1 to realAttCount
                try
                    set anAtt to item i of realAttList
                    set attName to name of anAtt
                    set attSizeNum to (file size of anAtt)
                    set attSize to attSizeNum as string
                    set attMime to ""
                    try
                        set attMime to mime type of anAtt
                    end try
                    set end of attachLines to attName & "|||" & attSize & "|||" & attMime
                end try
            end repeat

            set AppleScript's text item delimiters to linefeed
            set attachStr to attachLines as string
            set AppleScript's text item delimiters to ""

            return "MSG_ID:" & msgNumId & linefeed & ¬
                   "INTERNET_ID:" & msgInternetId & linefeed & ¬
                   "SUBJECT:" & msgSubject & linefeed & ¬
                   "FROM:" & msgSender & linefeed & ¬
                   "TO:" & msgTo & linefeed & ¬
                   "CC:" & msgCc & linefeed & ¬
                   "REPLY_TO:" & msgReplyTo & linefeed & ¬
                   "DATE_RECV:" & msgDateRecv & linefeed & ¬
                   "DATE_SENT:" & msgDateSent & linefeed & ¬
                   "IS_READ:" & msgRead & linefeed & ¬
                   "IS_FLAGGED:" & msgFlagged & linefeed & ¬
                   "MAILBOX:" & foundMailboxName & linefeed & ¬
                   "ACCOUNT:" & foundAccountName & linefeed & ¬
                   "ATTACH_COUNT:" & realAttCount & linefeed & ¬
                   "ATTACHMENTS:" & attachStr & linefeed & ¬
                   "BODY_START" & linefeed & msgBody & linefeed & "BODY_END"
        end timeout
    end tell
    '''

    try:
        raw = run_applescript(script, timeout=60)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    if raw.strip() == "NOT_FOUND":
        return json.dumps({"error": "Email not found"})

    result = _parse_get_email_output(raw)
    resolved_id = result.get("message_id", "")
    for i, att in enumerate(result.get("attachments", [])):
        if resolved_id:
            att["resource_uri"] = f"mail-attachment://{resolved_id}/{i}"
    return json.dumps(result, ensure_ascii=False)


def _parse_get_email_output(raw: str) -> Dict[str, Any]:
    """Parse the labelled output from the get_email AppleScript into a dict."""
    lines = raw.splitlines()

    record: Dict[str, Any] = {}
    body_lines: List[str] = []
    in_body = False
    in_attachments = False
    attach_lines: List[str] = []

    for line in lines:
        if in_body:
            if line == "BODY_END":
                in_body = False
            else:
                body_lines.append(line)
            continue

        if line == "BODY_START":
            in_body = True
            in_attachments = False
            continue

        if in_attachments:
            if line:
                attach_lines.append(line)
            continue

        if line.startswith("ATTACHMENTS:"):
            in_attachments = True
            first = line[len("ATTACHMENTS:"):].strip()
            if first:
                attach_lines.append(first)
            continue

        for prefix, key in [
            ("MSG_ID:", "message_id"),
            ("INTERNET_ID:", "internet_message_id"),
            ("SUBJECT:", "subject"),
            ("FROM:", "from"),
            ("TO:", "to"),
            ("CC:", "cc"),
            ("REPLY_TO:", "reply_to"),
            ("DATE_RECV:", "date_received"),
            ("DATE_SENT:", "date_sent"),
            ("IS_READ:", "is_read"),
            ("IS_FLAGGED:", "is_flagged"),
            ("MAILBOX:", "mailbox"),
            ("ACCOUNT:", "account"),
            ("ATTACH_COUNT:", "attachment_count"),
        ]:
            if line.startswith(prefix):
                record[key] = line[len(prefix):]
                break

    record["is_read"] = record.get("is_read", "").lower() == "true"
    record["is_flagged"] = record.get("is_flagged", "").lower() == "true"
    try:
        record["attachment_count"] = int(record.get("attachment_count", 0))
    except ValueError:
        record["attachment_count"] = 0

    record["body"] = "\n".join(body_lines)

    attachments = []
    for att_line in attach_lines:
        fields = att_line.split("|||")
        if len(fields) >= 2:
            try:
                size = int(fields[1].strip())
            except ValueError:
                size = 0
            name = fields[0].strip()
            mime_type = fields[2].strip() if len(fields) >= 3 else ""
            # AppleScript's mime type is often empty on Exchange/Outlook mail.
            if not mime_type and name:
                guessed, _ = mimetypes.guess_type(name)
                mime_type = guessed or "application/octet-stream"
            attachments.append(
                {
                    "name": name,
                    "size_bytes": size,
                    "mime_type": mime_type,
                }
            )
    record["attachments"] = attachments

    internet_mid = record.get("internet_message_id", "")
    if internet_mid:
        msg_id = internet_mid.strip("<>")
        record["mail_link"] = f"message://%3C{quote(msg_id, safe='@')}%3E"

    return record


ATTACHMENT_RESOURCE_URI = "mail-attachment://{message_id}/{index}"


@mcp.resource(ATTACHMENT_RESOURCE_URI, mime_type="application/octet-stream")
def get_attachment_resource(
    message_id: Union[str, int], index: Union[str, int]
) -> bytes:
    """Fetch the binary content of an email attachment.

    URI: mail-attachment://{message_id}/{index}
      message_id — Apple Mail numeric ID (from get_email or search_emails)
      index      — 0-based attachment index (from get_email's attachments list)

    The resource URIs are returned by get_email inside each attachment object
    as the "resource_uri" field, so clients do not need to construct them manually.
    """
    message_id = str(message_id).strip()
    if not message_id.isdigit():
        raise ValueError(f"message_id must be numeric, got: {message_id!r}")

    try:
        idx = int(index)
    except (ValueError, TypeError):
        raise ValueError(f"index must be an integer, got: {index!r}")

    if idx < 0:
        raise ValueError(f"index must be >= 0, got: {idx}")

    applescript_index = idx + 1  # AppleScript lists are 1-based

    skip_folders = '{"Trash", "Junk", "Junk Email", "Deleted Items", "Sent", "Sent Items", "Sent Messages", "Drafts", "Spam", "Deleted Messages"}'

    tmp_fd, tmp_path = tempfile.mkstemp()
    os.close(tmp_fd)
    escaped_tmp = escape_applescript(tmp_path)

    script = f'''
    tell application "Mail"
        with timeout of 60 seconds
            set foundMsg to missing value

            repeat with targetAccount in every account
                if foundMsg is not missing value then exit repeat
                repeat with currentMailbox in every mailbox of targetAccount
                    set mbName to name of currentMailbox
                    if mbName is not in {skip_folders} then
                        try
                            set msgs to every message of currentMailbox whose id is {message_id}
                            if (count of msgs) > 0 then
                                set foundMsg to item 1 of msgs
                                exit repeat
                            end if
                        end try
                    end if
                end repeat
            end repeat

            if foundMsg is missing value then
                error "Email not found: " & {message_id}
            end if

            set atts to mail attachments of foundMsg
            if {applescript_index} > (count of atts) then
                error "Attachment index {idx} out of range (message has " & (count of atts) & " attachment(s))"
            end if

            save item {applescript_index} of atts in POSIX file "{escaped_tmp}"
        end timeout
    end tell
    '''

    try:
        run_applescript(script, timeout=60)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
