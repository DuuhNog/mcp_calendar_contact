#!/usr/bin/env python3
# bruno_mail_mcp.py
# MCP server: lists, reads, sends, replies, forwards, moves and deletes
# Apple Mail (macOS) messages/mailboxes/accounts via ScriptingBridge
# (Mail.app has no public EventKit-like framework; it's only reachable
# through Apple Events / AppleScript's object model). With debug logging
# to a file.

# --- MCP SDK imports ---
from mcp.server.fastmcp import FastMCP

# ScriptingBridge lets us drive Mail.app through Apple Events using plain
# Python method calls instead of building AppleScript source strings (which
# would risk script-injection if any argument contains a quote or
# "end tell"). It comes via pyobjc.
from ScriptingBridge import SBApplication

# Foundation provides NSURL, needed for 'file'-typed properties/commands
# (attachment file name, saving an attachment to disk).
from Foundation import NSURL

from datetime import datetime
from typing import Literal

import logging
import os

# --- Debug log configuration ---
LOG_PATH = os.path.expanduser("~/bruno_mcp_debug.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bruno-mail")
log.info("=== Server starting ===")

MAIL_BUNDLE_ID = "com.apple.mail"
app = SBApplication.applicationWithBundleIdentifier_(MAIL_BUNDLE_ID)

ACCESS_DENIED_MSG = (
    "Could not reach any Mail account. On first use, macOS shows an "
    "Automation permission pop-up for this process to control 'Mail' — "
    "accept it (or check Settings > Privacy & Security > Automation). "
    "Mail.app also needs at least one account configured."
)


def _g(getter, default=""):
    """Safely calls a ScriptingBridge property getter.

    Mail's AppleScript object model can raise on edge cases (e.g. a
    mailbox with no parent container) and a handful of properties aren't
    exercised by this module; isolating each read keeps one bad property
    from breaking an entire listing.
    """
    try:
        value = getter()
        return default if value is None else value
    except Exception as exc:
        log.debug("Property read failed: %s", exc)
        return default


def _message_text(msg) -> str:
    """Reads a message's body text.

    'content' is declared as 'rich text' in Mail's dictionary, so
    ScriptingBridge hands back a reference to that rich-text object
    rather than a plain string — .get() resolves it to actual text
    (unlike plain 'text'-typed properties, e.g. subject/sender, which
    come back as strings directly).
    """
    try:
        return str(msg.content().get())
    except Exception as exc:
        log.debug("Could not read message content: %s", exc)
        return ""


def _ensure_access() -> bool:
    """Checks that Mail.app is reachable and has at least one account.

    Unlike EventKit/Contacts, Mail has no formal access-request API — it's
    driven entirely by Apple Events (the Automation TCC category), which
    macOS grants or denies silently: a denied request returns empty
    results rather than raising. Referencing any property auto-launches
    Mail in the background if it isn't running already, so no explicit
    launch call is needed.
    """
    try:
        return len(app.accounts()) > 0
    except Exception as exc:
        log.error("Mail.app not reachable: %s", exc)
        return False


def _find_account(identifier: str):
    """Finds an account by id or by name (case-insensitive), or None."""
    identifier = identifier.strip()
    if not identifier:
        return None
    for acc in app.accounts():
        if str(_g(acc.id)) == identifier:
            return acc
        if str(_g(acc.name)).lower() == identifier.lower():
            return acc
    return None


def _find_mailbox(mailbox_ref: str):
    """Resolves a mailbox_ref ('account::path/to/mailbox') back to an
    object, by walking nested mailboxes() from the matching account."""
    mailbox_ref = mailbox_ref.strip()
    if "::" not in mailbox_ref:
        return None
    acc_ref, path = mailbox_ref.split("::", 1)
    account = _find_account(acc_ref)
    if account is None:
        return None
    current = list(account.mailboxes())
    target = None
    for segment in [s for s in path.split("/") if s]:
        target = next((m for m in current if str(_g(m.name)) == segment), None)
        if target is None:
            return None
        current = list(target.mailboxes())
    return target


def _find_message(message_id: str):
    """Resolves a message_id ('mailbox_ref::numeric_id') back to an object.

    Mail has no flat, app-wide message collection to search, so a message
    is always addressed through its mailbox — this keeps the search
    bounded to one mailbox instead of scanning every account.
    """
    message_id = message_id.strip()
    mailbox_ref, sep, numeric = message_id.rpartition("::")
    if not sep:
        return None
    mailbox = _find_mailbox(mailbox_ref)
    if mailbox is None:
        return None
    for msg in mailbox.messages():
        if str(_g(msg.id)) == numeric:
            return msg
    return None


def _format_message_line(msg, mailbox_ref: str) -> str:
    subject = _g(msg.subject, "(no subject)")
    sender = _g(msg.sender)
    date_sent = _g(msg.dateSent)
    date_str = ""
    if date_sent:
        try:
            date_str = datetime.fromtimestamp(
                date_sent.timeIntervalSince1970()
            ).strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = ""
    read = "" if _g(msg.readStatus, True) else "UNREAD, "
    flagged = "flagged, " if _g(msg.flaggedStatus, False) else ""
    msg_id = f"{mailbox_ref}::{_g(msg.id)}"
    return f"- [{date_str}] {read}{flagged}{subject} — from {sender} (id: {msg_id})"


def _resolve_send_account(account: str):
    """Picks the account/email address a new message should be sent from.

    Returns (full_name, email, error_message). On error, full_name/email
    are None and error_message explains what to do.
    """
    accounts = [a for a in app.accounts() if _g(a.enabled, True)]
    if account:
        acc = _find_account(account)
        if acc is None:
            names = ", ".join(str(_g(a.name)) for a in accounts)
            return None, None, f"Account not found: {account}. Available: {names}"
    elif len(accounts) == 1:
        acc = accounts[0]
    else:
        names = ", ".join(str(_g(a.name)) for a in accounts)
        return None, None, (
            f"Multiple accounts configured ({names}); pass account= to "
            "choose which one to send from."
        )
    emails = list(_g(acc.emailAddresses, []))
    if not emails:
        return None, None, f"Account '{_g(acc.name)}' has no email address configured."
    full_name = _g(acc.fullName)
    return full_name, str(emails[0]), None


def _parse_addresses(text: str) -> list:
    return [a.strip() for a in text.split(",") if a.strip()]


def _compose_and_send(
    subject: str,
    body: str,
    to_addrs: list,
    cc_addrs: list,
    bcc_addrs: list,
    sender: str,
    attachments: list,
) -> tuple:
    """Builds an outgoing message, attaches files best-effort, sends it.

    Returns (ok: bool, detail: str) — detail is an error on failure, or a
    note about any attachments that couldn't be added on success.
    """
    msg_cls = app.classForScriptingClass_("outgoing message")
    msg = msg_cls.alloc().initWithProperties_({
        "subject": subject,
        "content": body,
        "visible": False,
    })
    app.outgoingMessages().addObject_(msg)
    if sender:
        msg.setSender_(sender)

    recipient_cls = app.classForScriptingClass_("to recipient")
    cc_cls = app.classForScriptingClass_("cc recipient")
    bcc_cls = app.classForScriptingClass_("bcc recipient")
    for addr in to_addrs:
        msg.toRecipients().addObject_(
            recipient_cls.alloc().initWithProperties_({"address": addr})
        )
    for addr in cc_addrs:
        msg.ccRecipients().addObject_(
            cc_cls.alloc().initWithProperties_({"address": addr})
        )
    for addr in bcc_addrs:
        msg.bccRecipients().addObject_(
            bcc_cls.alloc().initWithProperties_({"address": addr})
        )

    failed_attachments = []
    if attachments:
        attachment_cls = app.classForScriptingClass_("attachment")
        content_rt = msg.content()
        for path in attachments:
            path = path.strip()
            if not path:
                continue
            if not os.path.isfile(path):
                failed_attachments.append(f"{path} (file not found)")
                continue
            try:
                att = attachment_cls.alloc().initWithProperties_({
                    "fileName": NSURL.fileURLWithPath_(path),
                })
                content_rt.attachments().addObject_(att)
            except Exception as exc:
                log.warning("Could not attach %s: %s", path, exc)
                failed_attachments.append(path)

    try:
        ok = bool(msg.send())
    except Exception as exc:
        log.error("send() raised: %s", exc)
        return False, str(exc)

    if not ok:
        return False, "Mail reported the send as unsuccessful."
    detail = ""
    if failed_attachments:
        detail = "Sent, but could not attach: " + ", ".join(failed_attachments)
    return True, detail


# --- Create the MCP server ---
mcp = FastMCP("bruno-mail")


@mcp.tool()
def list_mail_accounts() -> str:
    """Lists configured Mail.app accounts (name, id, type, email, enabled)."""
    log.info("list_mail_accounts() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    lines = []
    for acc in app.accounts():
        emails = ", ".join(str(e) for e in _g(acc.emailAddresses, []))
        enabled = "enabled" if _g(acc.enabled, True) else "disabled"
        lines.append(
            f"- {_g(acc.name)} <{emails}> (id: {_g(acc.id)}, {enabled})"
        )
    if not lines:
        return "No Mail accounts configured."
    return "Mail accounts:\n" + "\n".join(lines)


@mcp.tool()
def list_mailboxes(account: str = "") -> str:
    """Lists mailboxes (folders), including nested ones, for an account.

    Args:
        account: account name or id (see list_mail_accounts). Empty =
            every account.
    """
    log.info("list_mailboxes(account=%r) called", account)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if account:
        acc = _find_account(account)
        if acc is None:
            return f"Account not found: {account}. Use list_mail_accounts."
        accounts = [acc]
    else:
        accounts = list(app.accounts())

    lines = []

    # Mailboxes have no native id, so one is identified as
    # 'account::nested/path'. The path is built forward, from the same
    # mailboxes() traversal used to find them later (_find_mailbox) —
    # walking backward via container() is unreliable: some providers
    # (e.g. Gmail's IMAP) expose hidden intermediate nodes through
    # container() that never appear in the forward mailboxes() listing.
    def walk(mb, depth, account_name, path_prefix):
        indent = "  " * depth
        unread = _g(mb.unreadCount, 0)
        unread_desc = f", {unread} unread" if unread else ""
        ref = f"{account_name}::{path_prefix}{_g(mb.name)}"
        lines.append(f"{indent}- {_g(mb.name)} (id: {ref}{unread_desc})")
        for child in mb.mailboxes():
            walk(child, depth + 1, account_name, f"{path_prefix}{_g(mb.name)}/")

    for acc in accounts:
        acc_name = str(_g(acc.name))
        lines.append(f"{acc_name}:")
        for mb in acc.mailboxes():
            walk(mb, 1, acc_name, "")

    return "Mailboxes:\n" + "\n".join(lines)


@mcp.tool()
def create_mailbox(name: str, account: str = "", parent: str = "") -> str:
    """Creates a mailbox (folder) in an account, optionally nested.

    Args:
        name: the new mailbox's name.
        account: account name or id to create it in. Required unless
            parent is given (parent already implies an account).
        parent: id of a parent mailbox (see list_mailboxes), to create a
            subfolder instead of a top-level one.
    """
    log.info("create_mailbox(name=%r, account=%r, parent=%r) called", name, account, parent)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not name.strip():
        return "Mailbox name cannot be empty."

    if parent:
        parent_mb = _find_mailbox(parent)
        if parent_mb is None:
            return f"Parent mailbox not found: {parent}. Use list_mailboxes."
        target = parent_mb
        new_ref = f"{parent}/{name.strip()}"
    else:
        if not account:
            return "Pass account= (or parent=) to say where to create it."
        acc = _find_account(account)
        if acc is None:
            return f"Account not found: {account}. Use list_mail_accounts."
        target = acc
        new_ref = f"{_g(acc.name)}::{name.strip()}"

    mailbox_cls = app.classForScriptingClass_("mailbox")
    new_mb = mailbox_cls.alloc().initWithProperties_({"name": name.strip()})
    try:
        target.mailboxes().addObject_(new_mb)
    except Exception as exc:
        log.error("Failed to create mailbox: %s", exc)
        return f"Failed to create mailbox: {exc}"

    log.info("Mailbox created: %s", name)
    return f"Mailbox created: {name} (id: {new_ref})"


@mcp.tool()
def delete_mailbox(mailbox_id: str) -> str:
    """Deletes a mailbox (folder). This is irreversible for the folder
    itself; depending on the account, its messages may move to Trash or
    be removed outright.

    Args:
        mailbox_id: id as shown by list_mailboxes.
    """
    log.info("delete_mailbox(mailbox_id=%r) called", mailbox_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    mb = _find_mailbox(mailbox_id)
    if mb is None:
        return f"Mailbox not found: {mailbox_id}. Use list_mailboxes."
    name = _g(mb.name)
    try:
        mb.delete()
    except Exception as exc:
        log.error("Failed to delete mailbox: %s", exc)
        return f"Failed to delete mailbox: {exc}"
    log.info("Mailbox deleted: %s", mailbox_id)
    return f"Mailbox deleted: {name}"


@mcp.tool()
def list_messages(
    account: str = "",
    mailbox: str = "",
    page: int = 1,
    page_size: int = 50,
    unread_only: bool = False,
) -> str:
    """Lists messages in a mailbox, newest first, paginated.

    Args:
        account: account name or id (required unless mailbox is given).
        mailbox: mailbox id (see list_mailboxes). Empty = the account's
            Inbox-equivalent isn't assumed; pass a mailbox id explicitly.
        page: 1-based page number (default 1).
        page_size: messages per page (default 50, max 200 — Mail's Apple
            Events are slow per-message, so large pages take a while).
        unread_only: only list unread messages.
    """
    log.info(
        "list_messages(account=%r, mailbox=%r, page=%d, page_size=%d, unread_only=%r) called",
        account, mailbox, page, page_size, unread_only,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not mailbox:
        return "Pass mailbox= (a mailbox id from list_mailboxes)."
    mb = _find_mailbox(mailbox)
    if mb is None:
        return f"Mailbox not found: {mailbox}. Use list_mailboxes."

    if page < 1:
        return "page must be >= 1."
    page_size = max(1, min(page_size, 200))

    messages = list(mb.messages())
    if unread_only:
        messages = [m for m in messages if not _g(m.readStatus, True)]
    messages.sort(
        key=lambda m: _g(m.dateSent) and m.dateSent().timeIntervalSince1970() or 0,
        reverse=True,
    )

    total = len(messages)
    if total == 0:
        return "No messages found."
    page_size_eff = page_size
    total_pages = (total + page_size_eff - 1) // page_size_eff
    if page > total_pages:
        return f"Page {page} is out of range ({total_pages} page(s) total for {total} message(s))."

    start_idx = (page - 1) * page_size_eff
    page_msgs = messages[start_idx:start_idx + page_size_eff]
    lines = [_format_message_line(m, mailbox) for m in page_msgs]

    header = (
        f"Messages {start_idx + 1}-{start_idx + len(lines)} of {total} "
        f"(page {page}/{total_pages}):"
    )
    footer = ""
    if page < total_pages:
        footer = (
            f"\n\nMore results available: call list_messages(mailbox={mailbox!r}, "
            f"page={page + 1}, page_size={page_size}) to continue."
        )
    return header + "\n" + "\n".join(lines) + footer


@mcp.tool()
def search_messages(
    query: str,
    mailbox: str = "",
    account: str = "",
    page: int = 1,
    page_size: int = 50,
) -> str:
    """Searches messages by subject/sender containing query (case-insensitive).

    Args:
        query: text to search for in the subject or sender.
        mailbox: restrict to one mailbox id (see list_mailboxes). Empty =
            search every mailbox of account (or of every account).
        account: restrict to one account's mailboxes. Ignored if mailbox
            is given.
        page: 1-based page number.
        page_size: results per page (default 50, max 200).
    """
    log.info(
        "search_messages(query=%r, mailbox=%r, account=%r, page=%d, page_size=%d) called",
        query, mailbox, account, page, page_size,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not query.strip():
        return "query cannot be empty."

    if mailbox:
        target_mailboxes = [(mailbox, _find_mailbox(mailbox))]
        if target_mailboxes[0][1] is None:
            return f"Mailbox not found: {mailbox}. Use list_mailboxes."
    else:
        accounts = [_find_account(account)] if account else list(app.accounts())
        if account and accounts[0] is None:
            return f"Account not found: {account}. Use list_mail_accounts."
        target_mailboxes = []

        def collect(mb, account_name, path_prefix):
            ref = f"{account_name}::{path_prefix}{_g(mb.name)}"
            target_mailboxes.append((ref, mb))
            for child in mb.mailboxes():
                collect(child, account_name, f"{path_prefix}{_g(mb.name)}/")

        for acc in accounts:
            acc_name = str(_g(acc.name))
            for mb in acc.mailboxes():
                collect(mb, acc_name, "")

    q = query.strip().lower()
    results = []
    for ref, mb in target_mailboxes:
        for m in mb.messages():
            subject = str(_g(m.subject))
            sender = str(_g(m.sender))
            if q in subject.lower() or q in sender.lower():
                results.append((ref, m))

    results.sort(
        key=lambda pair: _g(pair[1].dateSent) and pair[1].dateSent().timeIntervalSince1970() or 0,
        reverse=True,
    )

    total = len(results)
    if total == 0:
        return f"No messages found matching '{query}'."
    page_size = max(1, min(page_size, 200))
    total_pages = (total + page_size - 1) // page_size
    if page < 1 or page > total_pages:
        return f"Page {page} is out of range ({total_pages} page(s) total for {total} result(s))."

    start_idx = (page - 1) * page_size
    page_results = results[start_idx:start_idx + page_size]
    lines = [_format_message_line(m, ref) for ref, m in page_results]
    header = f"Results {start_idx + 1}-{start_idx + len(lines)} of {total} (page {page}/{total_pages}):"
    return header + "\n" + "\n".join(lines)


@mcp.tool()
def get_message(message_id: str) -> str:
    """Shows the full detail of a message: headers, body, attachments.

    Args:
        message_id: id as shown by list_messages/search_messages.
    """
    log.info("get_message(message_id=%r) called", message_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."

    subject = _g(msg.subject, "(no subject)")
    sender = _g(msg.sender)
    content = _message_text(msg)
    date_sent = _g(msg.dateSent)
    date_str = ""
    if date_sent:
        try:
            date_str = datetime.fromtimestamp(date_sent.timeIntervalSince1970()).strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = ""

    def recipients_line(label, recipients):
        addrs = ", ".join(str(_g(r.address)) for r in recipients)
        return f"{label}: {addrs}" if addrs else ""

    lines = [
        f"Subject: {subject}",
        f"From: {sender}",
        recipients_line("To", msg.toRecipients()),
        recipients_line("Cc", msg.ccRecipients()),
        f"Date: {date_str}",
        f"Read: {_g(msg.readStatus, True)}, Flagged: {_g(msg.flaggedStatus, False)}",
    ]
    attachments = list(msg.mailAttachments())
    if attachments:
        att_lines = [
            f"  - {_g(a.name)} ({_g(a.fileSize, 0)} bytes, "
            f"{'downloaded' if _g(a.downloaded, False) else 'not downloaded'}, id: {_g(a.id)})"
            for a in attachments
        ]
        lines.append("Attachments:\n" + "\n".join(att_lines))
    lines = [l for l in lines if l]
    body = "\n\n" + str(content) if content else ""
    return "\n".join(lines) + body


@mcp.tool()
def send_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    account: str = "",
    attachments: str = "",
) -> str:
    """Composes and sends a new email. Sending cannot be undone.

    Args:
        to: comma-separated recipient addresses.
        subject: email subject.
        body: plain-text body (Mail's scripting API doesn't expose rich
            HTML composition).
        cc: comma-separated Cc addresses.
        bcc: comma-separated Bcc addresses.
        account: which configured account to send from (name or id, see
            list_mail_accounts). Required when more than one account is
            configured, to avoid sending from the wrong address.
        attachments: comma-separated local file paths to attach.
    """
    log.info(
        "send_message(to=%r, subject=%r, cc=%r, bcc=%r, account=%r, attachments=%r) called",
        to, subject, cc, bcc, account, attachments,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    to_addrs = _parse_addresses(to)
    if not to_addrs:
        return "to cannot be empty."

    full_name, email, err = _resolve_send_account(account)
    if err:
        return err
    sender = f"{full_name} <{email}>" if full_name else email

    ok, detail = _compose_and_send(
        subject=subject,
        body=body,
        to_addrs=to_addrs,
        cc_addrs=_parse_addresses(cc),
        bcc_addrs=_parse_addresses(bcc),
        sender=sender,
        attachments=_parse_addresses(attachments),
    )
    if not ok:
        return f"Failed to send message: {detail}"
    log.info("Message sent: %r to %s", subject, to_addrs)
    suffix = f" ({detail})" if detail else ""
    return f"Message sent: '{subject}' to {', '.join(to_addrs)}{suffix}"


@mcp.tool()
def reply_message(message_id: str, body: str, reply_all: bool = False) -> str:
    """Sends a reply to a message, quoting the original.

    This composes a new message (subject prefixed 'Re:', original quoted
    below the reply body) rather than using Mail's native threaded reply,
    so the reply isn't linked to the original via In-Reply-To headers.

    Args:
        message_id: id of the message to reply to (see list_messages).
        body: the reply text.
        reply_all: reply to every recipient of the original, not just its
            sender.
    """
    log.info("reply_message(message_id=%r, reply_all=%r) called", message_id, reply_all)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."

    subject = str(_g(msg.subject, "(no subject)"))
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    sender = str(_g(msg.sender))
    to_addrs = [sender] if sender else []
    cc_addrs = []
    if reply_all:
        cc_addrs = [str(_g(r.address)) for r in msg.toRecipients()] + \
                   [str(_g(r.address)) for r in msg.ccRecipients()]

    quoted = _message_text(msg)
    full_body = f"{body}\n\n--- Original message ---\n{quoted}"

    full_name, email, err = _resolve_send_account("")
    sender_addr = f"{full_name} <{email}>" if full_name else email
    if err:
        # Multiple accounts configured with no way to infer which to
        # reply from; fall back to Mail's own default sending identity.
        sender_addr = ""

    ok, detail = _compose_and_send(
        subject=subject,
        body=full_body,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=[],
        sender=sender_addr,
        attachments=[],
    )
    if not ok:
        return f"Failed to send reply: {detail}"
    log.info("Reply sent for message %s", message_id)
    return f"Reply sent: '{subject}' to {', '.join(to_addrs + cc_addrs)}"


@mcp.tool()
def forward_message(message_id: str, to: str, body: str = "") -> str:
    """Forwards a message, quoting the original. Attachments on the
    original are NOT carried over automatically in this version — use
    download_attachment then send_message's attachments= if needed.

    Args:
        message_id: id of the message to forward (see list_messages).
        to: comma-separated recipient addresses.
        body: optional text to add before the quoted original.
    """
    log.info("forward_message(message_id=%r, to=%r) called", message_id, to)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."

    to_addrs = _parse_addresses(to)
    if not to_addrs:
        return "to cannot be empty."

    subject = str(_g(msg.subject, "(no subject)"))
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"
    quoted = _message_text(msg)
    full_body = f"{body}\n\n--- Forwarded message ---\n{quoted}" if body else f"--- Forwarded message ---\n{quoted}"

    full_name, email, err = _resolve_send_account("")
    sender_addr = f"{full_name} <{email}>" if full_name else email
    if err:
        sender_addr = ""

    ok, detail = _compose_and_send(
        subject=subject,
        body=full_body,
        to_addrs=to_addrs,
        cc_addrs=[],
        bcc_addrs=[],
        sender=sender_addr,
        attachments=[],
    )
    if not ok:
        return f"Failed to forward message: {detail}"
    log.info("Message forwarded: %s", message_id)
    return f"Message forwarded: '{subject}' to {', '.join(to_addrs)}"


@mcp.tool()
def move_message(message_id: str, to_mailbox: str) -> str:
    """Moves a message to a different mailbox (can cross accounts).

    Args:
        message_id: id as shown by list_messages.
        to_mailbox: destination mailbox id (see list_mailboxes).
    """
    log.info("move_message(message_id=%r, to_mailbox=%r) called", message_id, to_mailbox)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."
    dest = _find_mailbox(to_mailbox)
    if dest is None:
        return f"Destination mailbox not found: {to_mailbox}. Use list_mailboxes."

    subject = _g(msg.subject, "(no subject)")
    try:
        msg.moveTo_(dest)
    except Exception as exc:
        log.error("Failed to move message: %s", exc)
        return f"Failed to move message: {exc}"
    log.info("Message moved: %s -> %s", message_id, to_mailbox)
    return f"Message moved: '{subject}' to {_g(dest.name)}"


@mcp.tool()
def mark_message(
    message_id: str,
    read: Literal["", "true", "false"] = "",
    flagged: Literal["", "true", "false"] = "",
) -> str:
    """Marks a message's read/flagged status.

    Args:
        message_id: id as shown by list_messages.
        read: 'true'/'false' to set the read status. Empty = unchanged.
        flagged: 'true'/'false' to set the flagged status. Empty = unchanged.
    """
    log.info("mark_message(message_id=%r, read=%r, flagged=%r) called", message_id, read, flagged)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."
    if read not in ("", "true", "false") or flagged not in ("", "true", "false"):
        return "read/flagged must be 'true', 'false', or empty."
    if not read and not flagged:
        return "Nothing to change: pass read= and/or flagged=."

    changes = []
    try:
        if read:
            msg.setReadStatus_(read == "true")
            changes.append(f"read={read}")
        if flagged:
            msg.setFlaggedStatus_(flagged == "true")
            changes.append(f"flagged={flagged}")
    except Exception as exc:
        log.error("Failed to mark message: %s", exc)
        return f"Failed to update message: {exc}"

    log.info("Message updated: %s (%s)", message_id, ", ".join(changes))
    return f"Message updated: {', '.join(changes)}"


@mcp.tool()
def delete_message(message_id: str) -> str:
    """Deletes a message (moves it to the account's Trash mailbox, or
    removes it permanently if it's already in Trash).

    Args:
        message_id: id as shown by list_messages.
    """
    log.info("delete_message(message_id=%r) called", message_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."
    subject = _g(msg.subject, "(no subject)")
    try:
        msg.delete()
    except Exception as exc:
        log.error("Failed to delete message: %s", exc)
        return f"Failed to delete message: {exc}"
    log.info("Message deleted: %s", message_id)
    return f"Message deleted: {subject}"


@mcp.tool()
def download_attachment(message_id: str, attachment_name: str, dest_path: str) -> str:
    """Saves one of a message's attachments to a local file path.

    Args:
        message_id: id as shown by list_messages/get_message.
        attachment_name: the attachment's name, as shown by get_message.
        dest_path: absolute local file path to save it to.
    """
    log.info(
        "download_attachment(message_id=%r, attachment_name=%r, dest_path=%r) called",
        message_id, attachment_name, dest_path,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    msg = _find_message(message_id)
    if msg is None:
        return f"Message not found: {message_id}. Use list_messages to get ids."
    att = next(
        (a for a in msg.mailAttachments() if str(_g(a.name)) == attachment_name),
        None,
    )
    if att is None:
        return f"Attachment not found: {attachment_name}. Use get_message to list them."
    try:
        att.saveIn_(NSURL.fileURLWithPath_(dest_path))
    except Exception as exc:
        log.error("Failed to save attachment: %s", exc)
        return f"Failed to save attachment: {exc}"
    log.info("Attachment saved: %s -> %s", attachment_name, dest_path)
    return f"Attachment saved to {dest_path}"


# --- Entry point ---
if __name__ == "__main__":
    log.info("Entering mcp.run()")
    mcp.run()
