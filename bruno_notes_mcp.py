#!/usr/bin/env python3
# bruno_notes_mcp.py
# MCP server: lists, creates, edits, moves and deletes Apple Notes
# (macOS) notes/folders/accounts via ScriptingBridge (Notes.app has no
# public EventKit-like framework; it's only reachable through Apple
# Events / AppleScript's object model). With debug logging to a file.

# --- MCP SDK imports ---
from mcp.server.fastmcp import FastMCP

# ScriptingBridge lets us drive Notes.app through Apple Events using plain
# Python method calls instead of building AppleScript source strings (which
# would risk script-injection if any argument contains a quote or
# "end tell"). It comes via pyobjc.
from ScriptingBridge import SBApplication

# Foundation provides NSURL, needed for saving an attachment to disk.
from Foundation import NSURL

from datetime import datetime

import html
import logging
import os

# --- Debug log configuration ---
LOG_PATH = os.path.expanduser("~/bruno_mcp_debug.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bruno-notes")
log.info("=== Server starting ===")

NOTES_BUNDLE_ID = "com.apple.Notes"
app = SBApplication.applicationWithBundleIdentifier_(NOTES_BUNDLE_ID)

ACCESS_DENIED_MSG = (
    "Could not reach any Notes account. On first use, macOS shows an "
    "Automation permission pop-up for this process to control 'Notes' — "
    "accept it (or check Settings > Privacy & Security > Automation)."
)


def _g(getter, default=""):
    """Safely calls a ScriptingBridge property getter — isolates one bad
    property read (e.g. a locked note's body) from breaking a listing."""
    try:
        value = getter()
        return default if value is None else value
    except Exception as exc:
        log.debug("Property read failed: %s", exc)
        return default


def _ensure_access() -> bool:
    """Checks that Notes.app is reachable and has at least one account.

    Like Mail, Notes has no formal access-request API — it's driven by
    Apple Events (the Automation TCC category), which macOS grants/denies
    silently: a denied request returns empty results rather than raising.
    Referencing any property auto-launches Notes in the background if
    it isn't running already.
    """
    try:
        return len(app.accounts()) > 0
    except Exception as exc:
        log.error("Notes.app not reachable: %s", exc)
        return False


def _find_account(identifier: str):
    identifier = identifier.strip()
    if not identifier:
        return None
    for acc in app.accounts():
        if str(_g(acc.id)) == identifier:
            return acc
        if str(_g(acc.name)).lower() == identifier.lower():
            return acc
    return None


def _find_folder(folder_id: str):
    """Notes exposes a flat, app-wide 'every folder' collection (across
    every account and nesting level), so a folder can be found directly
    by id without walking the account/folder tree."""
    folder_id = folder_id.strip()
    if not folder_id:
        return None
    for f in app.folders():
        if str(_g(f.id)) == folder_id:
            return f
    return None


def _find_note(note_id: str):
    """Notes exposes a flat, app-wide 'every note' collection, so a note
    can be found directly by id without walking folders."""
    note_id = note_id.strip()
    if not note_id:
        return None
    for n in app.notes():
        if str(_g(n.id)) == note_id:
            return n
    return None


def _resolve_target_folder(account: str, folder: str):
    """Picks the folder a new note/folder should be created in.

    Returns (folder_or_account_obj, error_message).
    """
    if folder:
        f = _find_folder(folder)
        if f is None:
            return None, f"Folder not found: {folder}. Use list_note_folders."
        return f, None
    if account:
        acc = _find_account(account)
        if acc is None:
            return None, f"Account not found: {account}. Use list_note_accounts."
        return acc.defaultFolder(), None
    acc = app.defaultAccount()
    if acc is None:
        return None, "No default Notes account available. Pass account= or folder=."
    return acc.defaultFolder(), None


def _build_body_html(title: str, body: str) -> str:
    """Builds simple HTML for a new/updated note's body. Notes renders a
    note's name as the first line of its body, so title (when given)
    becomes the first <div>."""
    parts = []
    if title:
        parts.append(f"<div><b>{html.escape(title)}</b></div>")
    for line in body.splitlines():
        parts.append(f"<div>{html.escape(line)}</div>" if line.strip() else "<div><br></div>")
    return "".join(parts)


def _format_note_line(note) -> str:
    name = _g(note.name, "(untitled)")
    mod = _g(note.modificationDate)
    mod_str = ""
    if mod:
        try:
            mod_str = datetime.fromtimestamp(mod.timeIntervalSince1970()).strftime("%d/%m/%Y %H:%M")
        except Exception:
            mod_str = ""
    locked = "locked, " if _g(note.passwordProtected, False) else ""
    shared = "shared, " if _g(note.shared, False) else ""
    return f"- [{mod_str}] {locked}{shared}{name} (id: {_g(note.id)})"


# --- Create the MCP server ---
mcp = FastMCP("bruno-notes")


@mcp.tool()
def list_note_accounts() -> str:
    """Lists Notes accounts (iCloud, On My Mac, IMAP accounts with Notes
    enabled), with name and id."""
    log.info("list_note_accounts() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    lines = [f"- {_g(acc.name)} (id: {_g(acc.id)})" for acc in app.accounts()]
    if not lines:
        return "No Notes accounts found."
    return "Notes accounts:\n" + "\n".join(lines)


@mcp.tool()
def list_note_folders(account: str = "") -> str:
    """Lists folders (including nested ones), with name and id.

    Args:
        account: account name or id (see list_note_accounts). Empty =
            every account.

    Smart Folders (filter-based) aren't scriptable and won't appear here.
    """
    log.info("list_note_folders(account=%r) called", account)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if account:
        acc = _find_account(account)
        if acc is None:
            return f"Account not found: {account}. Use list_note_accounts."
        accounts = [acc]
    else:
        accounts = list(app.accounts())

    lines = []

    def walk(f, depth):
        indent = "  " * depth
        shared = ", shared" if _g(f.shared, False) else ""
        lines.append(f"{indent}- {_g(f.name)} (id: {_g(f.id)}{shared})")
        for child in f.folders():
            walk(child, depth + 1)

    for acc in accounts:
        lines.append(f"{_g(acc.name)}:")
        for f in acc.folders():
            walk(f, 1)

    return "Folders:\n" + "\n".join(lines)


@mcp.tool()
def create_note_folder(name: str, account: str = "", parent: str = "") -> str:
    """Creates a folder, optionally nested inside another folder.

    Args:
        name: the new folder's name.
        account: account name or id to create it in (see
            list_note_accounts). Defaults to the default Notes account.
            Ignored if parent is given.
        parent: id of a parent folder (see list_note_folders), to create
            a subfolder instead of a top-level one.
    """
    log.info("create_note_folder(name=%r, account=%r, parent=%r) called", name, account, parent)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not name.strip():
        return "Folder name cannot be empty."

    if parent:
        target = _find_folder(parent)
        if target is None:
            return f"Parent folder not found: {parent}. Use list_note_folders."
    elif account:
        acc = _find_account(account)
        if acc is None:
            return f"Account not found: {account}. Use list_note_accounts."
        target = acc
    else:
        target = app.defaultAccount()
        if target is None:
            return "No default Notes account available. Pass account= or parent=."

    folder_cls = app.classForScriptingClass_("folder")
    new_folder = folder_cls.alloc().initWithProperties_({"name": name.strip()})
    try:
        target.folders().addObject_(new_folder)
    except Exception as exc:
        log.error("Failed to create folder: %s", exc)
        return f"Failed to create folder: {exc}"

    log.info("Folder created: %s", name)
    return f"Folder created: {name} (id: {_g(new_folder.id)})"


@mcp.tool()
def rename_note_folder(folder_id: str, name: str) -> str:
    """Renames a folder.

    Args:
        folder_id: id as shown by list_note_folders.
        name: the new name.
    """
    log.info("rename_note_folder(folder_id=%r, name=%r) called", folder_id, name)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not name.strip():
        return "name cannot be empty."
    folder = _find_folder(folder_id)
    if folder is None:
        return f"Folder not found: {folder_id}. Use list_note_folders."
    old_name = _g(folder.name)
    try:
        folder.setName_(name.strip())
    except Exception as exc:
        log.error("Failed to rename folder: %s", exc)
        return f"Failed to rename folder: {exc}"
    log.info("Folder renamed: %s -> %s", old_name, name)
    return f"Folder renamed: {old_name} -> {name}"


@mcp.tool()
def delete_note_folder(folder_id: str) -> str:
    """Deletes a folder. Notes moves its notes to the account's 'Recently
    Deleted' folder rather than erasing them outright, but the folder
    itself is gone for good.

    Args:
        folder_id: id as shown by list_note_folders.
    """
    log.info("delete_note_folder(folder_id=%r) called", folder_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    folder = _find_folder(folder_id)
    if folder is None:
        return f"Folder not found: {folder_id}. Use list_note_folders."
    name = _g(folder.name)
    try:
        folder.delete()
    except Exception as exc:
        log.error("Failed to delete folder: %s", exc)
        return f"Failed to delete folder: {exc}"
    log.info("Folder deleted: %s", folder_id)
    return f"Folder deleted: {name}"


@mcp.tool()
def list_notes(
    account: str = "",
    folder: str = "",
    query: str = "",
    page: int = 1,
    page_size: int = 100,
) -> str:
    """Lists notes, most recently modified first, paginated.

    Args:
        account: restrict to one account (name or id). Ignored if folder
            is given. Empty + no folder = every account.
        folder: restrict to one folder id (see list_note_folders).
        query: client-side substring filter over the note's plain text
            (Notes has no full-text search predicate via scripting),
            applied before paginating.
        page: 1-based page number (default 1).
        page_size: notes per page (default 100, max 300).
    """
    log.info(
        "list_notes(account=%r, folder=%r, query=%r, page=%d, page_size=%d) called",
        account, folder, query, page, page_size,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if folder:
        f = _find_folder(folder)
        if f is None:
            return f"Folder not found: {folder}. Use list_note_folders."
        notes = list(f.notes())
    elif account:
        acc = _find_account(account)
        if acc is None:
            return f"Account not found: {account}. Use list_note_accounts."
        notes = list(acc.notes())
    else:
        notes = list(app.notes())

    if query.strip():
        q = query.strip().lower()
        notes = [n for n in notes if q in str(_g(n.plaintext)).lower()]

    notes.sort(
        key=lambda n: _g(n.modificationDate) and n.modificationDate().timeIntervalSince1970() or 0,
        reverse=True,
    )

    total = len(notes)
    if total == 0:
        return "No notes found."
    if page < 1:
        return "page must be >= 1."
    page_size = max(1, min(page_size, 300))
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return f"Page {page} is out of range ({total_pages} page(s) total for {total} note(s))."

    start_idx = (page - 1) * page_size
    page_notes = notes[start_idx:start_idx + page_size]
    lines = [_format_note_line(n) for n in page_notes]

    header = f"Notes {start_idx + 1}-{start_idx + len(lines)} of {total} (page {page}/{total_pages}):"
    footer = ""
    if page < total_pages:
        extra = f", folder={folder!r}" if folder else (f", account={account!r}" if account else "")
        footer = (
            f"\n\nMore results available: call list_notes(page={page + 1}, "
            f"page_size={page_size}{extra}) to continue."
        )
    return header + "\n" + "\n".join(lines) + footer


@mcp.tool()
def get_note(note_id: str) -> str:
    """Shows a note's full detail: folder, dates, body, attachments.

    Args:
        note_id: id as shown by list_notes.
    """
    log.info("get_note(note_id=%r) called", note_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."

    if _g(note.passwordProtected, False):
        return (
            f"'{_g(note.name)}' is password-protected — its body can't be "
            "read via scripting while locked (no way to unlock it from "
            "here; it must be unlocked in the Notes app's UI first)."
        )

    name = _g(note.name, "(untitled)")
    folder = note.container()
    folder_desc = f"{_g(folder.name)} (id: {_g(folder.id)})" if folder else ""
    created = _g(note.creationDate)
    modified = _g(note.modificationDate)

    def fmt(d):
        if not d:
            return ""
        try:
            return datetime.fromtimestamp(d.timeIntervalSince1970()).strftime("%d/%m/%Y %H:%M")
        except Exception:
            return ""

    lines = [
        f"Name: {name}",
        f"Folder: {folder_desc}",
        f"Created: {fmt(created)}, Modified: {fmt(modified)}",
        f"Shared: {_g(note.shared, False)}",
    ]
    attachments = list(note.attachments())
    if attachments:
        att_lines = [f"  - {_g(a.name)} (id: {_g(a.id)})" for a in attachments]
        lines.append("Attachments:\n" + "\n".join(att_lines))

    body = _g(note.plaintext)
    return "\n".join(lines) + "\n\n" + str(body)


@mcp.tool()
def create_note(title: str, body: str = "", account: str = "", folder: str = "") -> str:
    """Creates a new note.

    Args:
        title: the note's title (becomes the first line of its body).
        body: the note's plain-text content, one line per paragraph.
        account: account to create it in (name or id, see
            list_note_accounts). Defaults to the default Notes account.
            Ignored if folder is given.
        folder: id of a specific folder to create it in (see
            list_note_folders).
    """
    log.info("create_note(title=%r, account=%r, folder=%r) called", title, account, folder)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not title.strip() and not body.strip():
        return "title and body cannot both be empty."

    target, err = _resolve_target_folder(account, folder)
    if err:
        return err

    body_html = _build_body_html(title.strip(), body)
    note_cls = app.classForScriptingClass_("note")
    new_note = note_cls.alloc().initWithProperties_({"body": body_html})
    try:
        target.notes().addObject_(new_note)
    except Exception as exc:
        log.error("Failed to create note: %s", exc)
        return f"Failed to create note: {exc}"

    log.info("Note created: %s", title or "(untitled)")
    return f"Note created: {_g(new_note.name, title)} (id: {_g(new_note.id)})"


@mcp.tool()
def update_note(note_id: str, title: str = "", body: str = "") -> str:
    """Replaces a note's content. This REPLACES the whole body — pass the
    full desired title/text, not just the part to change.

    Args:
        note_id: id as shown by list_notes.
        title: the note's new title (becomes the first line of its body).
        body: the note's new plain-text content, one line per paragraph.
    """
    log.info("update_note(note_id=%r) called", note_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    if not title.strip() and not body.strip():
        return "title and body cannot both be empty."
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."
    if _g(note.passwordProtected, False):
        return f"'{_g(note.name)}' is password-protected and can't be edited via scripting while locked."

    body_html = _build_body_html(title.strip(), body)
    try:
        note.setBody_(body_html)
    except Exception as exc:
        log.error("Failed to update note: %s", exc)
        return f"Failed to update note: {exc}"
    log.info("Note updated: %s", note_id)
    return f"Note updated: {_g(note.name)}"


@mcp.tool()
def move_note(note_id: str, to_folder: str) -> str:
    """Moves a note to a different folder (can cross accounts).

    Args:
        note_id: id as shown by list_notes.
        to_folder: destination folder id (see list_note_folders).
    """
    log.info("move_note(note_id=%r, to_folder=%r) called", note_id, to_folder)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."
    dest = _find_folder(to_folder)
    if dest is None:
        return f"Destination folder not found: {to_folder}. Use list_note_folders."

    name = _g(note.name)
    try:
        note.moveTo_(dest)
    except Exception as exc:
        log.error("Failed to move note: %s", exc)
        return f"Failed to move note: {exc}"
    log.info("Note moved: %s -> %s", note_id, to_folder)
    return f"Note moved: '{name}' to {_g(dest.name)}"


@mcp.tool()
def delete_note(note_id: str) -> str:
    """Deletes a note (moves it to the account's 'Recently Deleted' folder).

    Args:
        note_id: id as shown by list_notes.
    """
    log.info("delete_note(note_id=%r) called", note_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."
    name = _g(note.name)
    try:
        note.delete()
    except Exception as exc:
        log.error("Failed to delete note: %s", exc)
        return f"Failed to delete note: {exc}"
    log.info("Note deleted: %s", note_id)
    return f"Note deleted: {name}"


@mcp.tool()
def list_note_attachments(note_id: str) -> str:
    """Lists a note's attachments (name, id, and URL for URL attachments).

    Args:
        note_id: id as shown by list_notes.
    """
    log.info("list_note_attachments(note_id=%r) called", note_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."

    lines = []
    for a in note.attachments():
        url = _g(a.URL)
        url_part = f", url: {url}" if url else ""
        lines.append(f"- {_g(a.name)} (id: {_g(a.id)}{url_part})")
    if not lines:
        return "No attachments."
    return "Attachments:\n" + "\n".join(lines)


@mcp.tool()
def download_note_attachment(note_id: str, attachment_id: str, dest_path: str) -> str:
    """Saves one of a note's attachments to a local file path.

    Args:
        note_id: id as shown by list_notes.
        attachment_id: id as shown by list_note_attachments.
        dest_path: absolute local file path to save it to.
    """
    log.info(
        "download_note_attachment(note_id=%r, attachment_id=%r, dest_path=%r) called",
        note_id, attachment_id, dest_path,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG
    note = _find_note(note_id)
    if note is None:
        return f"Note not found: {note_id}. Use list_notes to get ids."
    att = next((a for a in note.attachments() if str(_g(a.id)) == attachment_id.strip()), None)
    if att is None:
        return f"Attachment not found: {attachment_id}. Use list_note_attachments."
    try:
        att.saveIn_(NSURL.fileURLWithPath_(dest_path))
    except Exception as exc:
        log.error("Failed to save attachment: %s", exc)
        return f"Failed to save attachment: {exc}"
    log.info("Attachment saved: %s -> %s", attachment_id, dest_path)
    return f"Attachment saved to {dest_path}"


# --- Entry point ---
if __name__ == "__main__":
    log.info("Entering mcp.run()")
    mcp.run()
