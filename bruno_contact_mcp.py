#!/usr/bin/env python3
# bruno_contact_mcp.py
# MCP server: lists, creates, edits and deletes macOS Contacts (Contacts.app)
# via the Contacts framework. With debug logging to a file.

# --- MCP SDK imports ---
# FastMCP handles the whole MCP protocol. You only write decorated functions.
from mcp.server.fastmcp import FastMCP

# Contacts is the native macOS framework for accessing the Contacts database.
# It comes via pyobjc; we import the classes we need.
from Contacts import (
    CNContact,
    CNContainer,
    CNGroup,
    CNMutableGroup,
    CNContactStore,
    CNEntityTypeContacts,
    CNMutableContact,
    CNContactFetchRequest,
    CNSaveRequest,
    CNLabeledValue,
    CNPhoneNumber,
    CNMutablePostalAddress,
    CNLabelHome,
    CNLabelWork,
    CNLabelOther,
    CNLabelPhoneNumberMobile,
    CNLabelPhoneNumberMain,
    CNLabelPhoneNumberiPhone,
    CNLabelURLAddressHomePage,
    CNContactGivenNameKey,
    CNContactFamilyNameKey,
    CNContactOrganizationNameKey,
    CNContactPhoneNumbersKey,
    CNContactEmailAddressesKey,
    CNContactIdentifierKey,
    CNContactBirthdayKey,
    CNContactPostalAddressesKey,
    CNContactUrlAddressesKey,
    CNContactNoteKey,
    CNContainerTypeLocal,
    CNContainerTypeExchange,
    CNContainerTypeCardDAV,
    CNContainerTypeUnassigned,
)
from Foundation import NSDateComponents

from typing import Literal

# logging for debug: writes everything to a file you can follow live.
import logging
import os
import re
import threading

# --- Debug log configuration ---
# The log goes to your home directory. Follow it with: tail -f ~/bruno_mcp_debug.log
LOG_PATH = os.path.expanduser("~/bruno_mcp_debug.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,  # DEBUG = logs absolutely everything
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bruno-contact")
log.info("=== Server starting ===")

# --- ContactStore instance ---
# CNContactStore is the central object that talks to the Contacts database.
store = CNContactStore.alloc().init()

ACCESS_DENIED_MSG = "Contacts access denied. Check Settings > Privacy > Contacts."

# Keys we always fetch: enough to list, edit and save a contact.
_KEYS_BASE = [
    CNContactGivenNameKey,
    CNContactFamilyNameKey,
    CNContactOrganizationNameKey,
    CNContactPhoneNumbersKey,
    CNContactEmailAddressesKey,
    CNContactIdentifierKey,
    CNContactBirthdayKey,
    CNContactPostalAddressesKey,
    CNContactUrlAddressesKey,
]

# CNContactNoteKey requires the com.apple.developer.contacts.notes
# entitlement, which most apps (including this one, most likely) don't
# have. Fetching/saving it without the entitlement raises. We probe once
# and remember the result so we stop trying after the first failure.
_notes_supported = None  # None = untested, True/False once known


def _keys(with_note: bool = True) -> list:
    keys = list(_KEYS_BASE)
    if with_note and _notes_supported is not False:
        keys.append(CNContactNoteKey)
    return keys


_PHONE_LABELS = {
    "mobile": CNLabelPhoneNumberMobile,
    "iphone": CNLabelPhoneNumberiPhone,
    "home": CNLabelHome,
    "personal": CNLabelHome,
    "work": CNLabelWork,
    "main": CNLabelPhoneNumberMain,
    "other": CNLabelOther,
}

_EMAIL_LABELS = {
    "home": CNLabelHome,
    "personal": CNLabelHome,
    "work": CNLabelWork,
    "other": CNLabelOther,
}

_ADDRESS_LABELS = {
    "home": CNLabelHome,
    "personal": CNLabelHome,
    "work": CNLabelWork,
    "other": CNLabelOther,
}

_URL_LABELS = {
    "homepage": CNLabelURLAddressHomePage,
    "home": CNLabelHome,
    "work": CNLabelWork,
    "other": CNLabelOther,
}

_CONTAINER_TYPES = {
    CNContainerTypeLocal: "Local",
    CNContainerTypeExchange: "Exchange",
    CNContainerTypeCardDAV: "CardDAV (iCloud/Google/etc.)",
    CNContainerTypeUnassigned: "Unassigned",
}


def _ensure_access() -> bool:
    """Ensures Contacts access permission.

    On the first run macOS shows a pop-up asking for authorization.
    We need to wait for the user's response, so we use an Event
    to block until the async callback returns.
    """
    log.debug("Requesting contacts access...")
    done = threading.Event()      # synchronizes the async callback
    result = {"granted": False}   # stores the callback result

    def handler(granted, err):
        # Callback invoked by macOS with the user's decision.
        result["granted"] = bool(granted)
        if err:
            log.error("Error requesting access: %s", err)
        log.debug("Access granted? %s", result["granted"])
        done.set()  # releases the wait() below

    store.requestAccessForEntityType_completionHandler_(CNEntityTypeContacts, handler)
    done.wait(timeout=60)  # waits up to 60s for the user's response
    return result["granted"]


def _normalize_phone(text: str) -> str:
    """Strips everything but digits, for duplicate/comparison purposes."""
    return re.sub(r"\D", "", text or "")


def _parse_labeled_list(text: str, labels: dict, default_label: str):
    """Parses 'label:value,label:value' (or bare 'value') into (CNLabel, value) pairs.

    A bare value with no 'label:' prefix gets default_label. Unknown labels
    fall back to 'other'.
    """
    pairs = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            label_key, value = chunk.split(":", 1)
            label_key = label_key.strip().lower()
            value = value.strip()
        else:
            label_key, value = default_label, chunk
        if not value:
            continue
        cn_label = labels.get(label_key, labels.get("other"))
        pairs.append((cn_label, value))
    return pairs


def _parse_address_list(text: str):
    """Parses 'label:street|city|state|zip|country; label:...' into
    (CNLabel, street, city, state, zip, country) tuples. Missing subfields
    are empty strings. A chunk with no 'label:' prefix defaults to 'home'.
    """
    entries = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            label_key, rest = chunk.split(":", 1)
            label_key = label_key.strip().lower()
        else:
            label_key, rest = "home", chunk
        parts = [p.strip() for p in rest.split("|")]
        parts += [""] * (5 - len(parts))
        street, city, state, zip_code, country = parts[:5]
        cn_label = _ADDRESS_LABELS.get(label_key, _ADDRESS_LABELS.get("other"))
        entries.append((cn_label, street, city, state, zip_code, country))
    return entries


def _format_address(addr) -> str:
    parts = [
        addr.street() or "",
        addr.city() or "",
        addr.state() or "",
        addr.postalCode() or "",
        addr.country() or "",
    ]
    return ", ".join(p for p in parts if p)


def _parse_birthday(text: str):
    """Parses 'YYYY-MM-DD' or 'MM-DD' into an NSDateComponents. Returns None
    if text is empty or malformed."""
    text = text.strip()
    if not text:
        return None
    parts = text.split("-")
    try:
        if len(parts) == 3:
            year, month, day = (int(p) for p in parts)
        elif len(parts) == 2:
            year = None
            month, day = (int(p) for p in parts)
        else:
            return None
    except ValueError:
        log.warning("Could not parse birthday %r", text)
        return None
    comps = NSDateComponents.alloc().init()
    comps.setMonth_(month)
    comps.setDay_(day)
    if year:
        comps.setYear_(year)
    return comps


def _format_birthday(comps) -> str:
    if comps is None:
        return ""
    month, day, year = comps.month(), comps.day(), comps.year()
    # pyobjc reports unset NSDateComponents fields as NSDateComponentUndefined (a huge sentinel int)
    if not month or not day or month > 1000 or day > 1000:
        return ""
    if year and 0 < year < 10000:
        return f"{year:04d}-{month:02d}-{day:02d}"
    return f"{month:02d}-{day:02d}"


def _fetch_all_contacts():
    """Fetches every contact (name, org, phones, emails, birthday, address, url, note, id).

    Falls back to retrying without CNContactNoteKey if fetching it raises or
    fails (e.g. this app lacks the notes entitlement), so the rest of the
    contact data still loads.
    """
    global _notes_supported
    keys = _keys()
    request = CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    contacts = []
    try:
        ok, err = store.enumerateContactsWithFetchRequest_error_usingBlock_(
            request, None, lambda contact, stop: contacts.append(contact)
        )
    except Exception as e:
        if CNContactNoteKey in keys:
            log.warning("Fetch with note key raised (%s); retrying without notes", e)
            _notes_supported = False
            return _fetch_all_contacts()
        raise
    if not ok:
        if CNContactNoteKey in keys and _notes_supported is None:
            log.warning("Fetch failed with note key (%s); retrying without notes", err)
            _notes_supported = False
            return _fetch_all_contacts()
        log.error("Failed to enumerate contacts: %s", err)
        return contacts
    if _notes_supported is None:
        _notes_supported = True
    return contacts


def _fetch_contact(contact_id: str):
    """Fetches one contact by id, with the same note-key fallback as _fetch_all_contacts()."""
    global _notes_supported
    keys = _keys()
    try:
        contact, err = store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, keys, None)
    except Exception as e:
        if CNContactNoteKey in keys:
            log.warning("Fetch of %s with note key raised (%s); retrying without notes", contact_id, e)
            _notes_supported = False
            return _fetch_contact(contact_id)
        raise
    if contact is None and CNContactNoteKey in keys and _notes_supported is None:
        log.warning("Fetch of %s failed with note key (%s); retrying without notes", contact_id, err)
        _notes_supported = False
        return _fetch_contact(contact_id)
    if contact is not None and _notes_supported is None:
        _notes_supported = True
    return contact, err


def _full_name(c) -> str:
    name = f"{c.givenName() or ''} {c.familyName() or ''}".strip()
    if not name:
        name = c.organizationName() or "(no name)"
    return name


def _build_account_map() -> dict:
    """Maps contact identifier -> account/container name (iCloud, Google, etc.).

    CNContact carries no account field directly; the association only
    exists via CNContainer, so it's rebuilt by asking each container
    which contacts it holds.
    """
    containers, err = store.containersMatchingPredicate_error_(None, None)
    if not containers:
        if err:
            log.error("Failed to fetch containers for account map: %s", err)
        return {}

    account_map = {}
    for container in containers:
        predicate = CNContact.predicateForContactsInContainerWithIdentifier_(container.identifier())
        contacts, err2 = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            predicate, [CNContactIdentifierKey], None
        )
        if not contacts:
            if err2:
                log.error("Failed to fetch contacts for container %s: %s", container.name(), err2)
            continue
        for contact in contacts:
            account_map[contact.identifier()] = container.name()
    return account_map


def _build_group_map() -> dict:
    """Maps contact identifier -> list of group ('subpasta') names.

    Mirrors _build_account_map(): CNContact carries no group field
    directly, so it's rebuilt by asking each group which contacts it holds.
    """
    groups, err = store.groupsMatchingPredicate_error_(None, None)
    if not groups:
        if err:
            log.error("Failed to fetch groups for group map: %s", err)
        return {}

    group_map = {}
    for group in groups:
        predicate = CNContact.predicateForContactsInGroupWithIdentifier_(group.identifier())
        contacts, err2 = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            predicate, [CNContactIdentifierKey], None
        )
        if not contacts:
            if err2:
                log.error("Failed to fetch contacts for group %s: %s", group.name(), err2)
            continue
        for contact in contacts:
            group_map.setdefault(contact.identifier(), []).append(group.name())
    return group_map


def _resolve_group_ids(text: str) -> tuple:
    """Resolves a comma-separated list of group ids or names to (ids, unknown_names).

    Accepts either the group identifier (as shown by list_contact_groups) or
    its exact name (case-insensitive), so callers don't have to look up ids
    first for common cases.
    """
    names = [n.strip() for n in text.split(",") if n.strip()]
    if not names:
        return [], []
    groups, err = store.groupsMatchingPredicate_error_(None, None)
    if not groups:
        if err:
            log.error("Failed to fetch groups while resolving %r: %s", text, err)
        return [], names
    by_id = {g.identifier(): g for g in groups}
    by_name = {g.name().lower(): g for g in groups}
    ids, unknown = [], []
    for n in names:
        if n in by_id:
            ids.append(n)
        elif n.lower() in by_name:
            ids.append(by_name[n.lower()].identifier())
        else:
            unknown.append(n)
    return ids, unknown


def _contact_container_id(contact_id: str):
    """Returns the identifier of the account/container a contact currently lives in."""
    predicate = CNContainer.predicateForContainerOfContactWithIdentifier_(contact_id)
    containers, err = store.containersMatchingPredicate_error_(predicate, None)
    if not containers:
        if err:
            log.error("Failed to find container for contact %s: %s", contact_id, err)
        return None
    return containers[0].identifier()


def _clone_contact_for_move(contact) -> "CNMutableContact":
    """Copies a contact's fields into a brand-new, unsaved CNMutableContact.

    Needed to move a contact to another account: CNSaveRequest's
    addContact_toContainerWithIdentifier_ requires a contact that hasn't
    been saved yet, so an existing contact's mutableCopy() (which keeps
    the original identifier) can't be reused directly.
    """
    new_contact = CNMutableContact.alloc().init()
    new_contact.setGivenName_(contact.givenName() or "")
    new_contact.setFamilyName_(contact.familyName() or "")
    if contact.organizationName():
        new_contact.setOrganizationName_(contact.organizationName())
    new_contact.setPhoneNumbers_(contact.phoneNumbers())
    new_contact.setEmailAddresses_(contact.emailAddresses())
    new_contact.setPostalAddresses_(contact.postalAddresses())
    new_contact.setUrlAddresses_(contact.urlAddresses())
    if contact.birthday():
        new_contact.setBirthday_(contact.birthday())
    if _notes_supported is not False:
        try:
            if contact.note():
                new_contact.setNote_(contact.note())
        except Exception as e:
            log.warning("Could not copy note while moving contact: %s", e)
    return new_contact


def _format_contact(c, account_map: dict = None, group_map: dict = None) -> str:
    """Formats one contact as a listing line, including its id, account and groups."""
    name = _full_name(c)
    phones = ", ".join(str(p.value().stringValue()) for p in c.phoneNumbers())
    emails = ", ".join(str(e.value()) for e in c.emailAddresses())
    org = c.organizationName() or ""
    birthday = _format_birthday(c.birthday())
    addresses = "; ".join(_format_address(a.value()) for a in c.postalAddresses())
    urls = ", ".join(str(u.value()) for u in c.urlAddresses())
    note = ""
    if _notes_supported is not False:
        try:
            note = c.note() or ""
        except Exception:
            note = ""
    extras = []
    if org:
        extras.append(f"[{org}]")
    if phones:
        extras.append(f"tel: {phones}")
    if emails:
        extras.append(f"email: {emails}")
    if birthday:
        extras.append(f"birthday: {birthday}")
    if addresses:
        extras.append(f"address: {addresses}")
    if urls:
        extras.append(f"url: {urls}")
    if note:
        shown = note if len(note) <= 40 else note[:37] + "..."
        extras.append(f"notes: {shown}")
    if account_map is not None:
        extras.append(f"account: {account_map.get(c.identifier(), '?')}")
    if group_map is not None:
        groups = group_map.get(c.identifier())
        if groups:
            extras.append(f"groups: {', '.join(groups)}")
    extras.append(f"(id: {c.identifier()})")
    return f"- {name} " + " | ".join(extras)


# --- Create the MCP server ---
# "bruno-contact" is the name shown to Claude.
mcp = FastMCP("bruno-contact")


# =========================================================
# TOOL 1: list contacts (optionally filtered by name)
# =========================================================
@mcp.tool()
def list_contacts(query: str = "", group: str = "", page: int = 1, page_size: int = 100) -> str:
    """Lists contacts from Contacts.app, with id, name, phones, emails, account and groups.

    Each line includes the contact id, needed by update_contact and
    delete_contact, the account (iCloud, Google, etc.) it belongs to, and
    any groups ('subpastas', e.g. Family, Work) it's a member of.
    Results are paginated to avoid oversized responses;
    when the footer says more pages remain, call again with page+1 to
    keep fetching until every page has been retrieved.

    Args:
        query: optional case-insensitive substring to filter by name,
            organization, phone or email. Empty = list everyone.
        group: optional group id or name (see list_contact_groups) to
            restrict the listing to members of that group. Empty = no filter.
        page: 1-based page number (default 1).
        page_size: contacts per page (default 100, max 500).
    """
    log.info("list_contacts(query=%r, group=%r, page=%d, page_size=%d) called", query, group, page, page_size)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if page < 1:
        return "page must be >= 1."
    page_size = max(1, min(page_size, 500))

    group_map = _build_group_map()

    if group.strip():
        group_ids, unknown = _resolve_group_ids(group)
        if unknown or not group_ids:
            return f"Group not found: '{group}'. Use list_contact_groups to get valid ids/names."
        group_id = group_ids[0]
        predicate = CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        contacts, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(predicate, _keys(), None)
        if contacts is None:
            log.error("Failed to fetch contacts for group %s: %s", group_id, err)
            return f"Failed to fetch contacts for group '{group}': {err}"
        contacts = list(contacts)
    else:
        contacts = _fetch_all_contacts()
    log.debug("Found %d contacts", len(contacts))

    if query:
        q = query.strip().lower()
        def matches(c):
            haystacks = [
                _full_name(c).lower(),
                (c.organizationName() or "").lower(),
                *[str(p.value().stringValue()).lower() for p in c.phoneNumbers()],
                *[str(e.value()).lower() for e in c.emailAddresses()],
            ]
            return any(q in h for h in haystacks)
        contacts = [c for c in contacts if matches(c)]

    if not contacts:
        return "No contacts found." if not query else f"No contacts match '{query}'."

    contacts = sorted(contacts, key=lambda c: _full_name(c).lower())
    total = len(contacts)
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return f"Page {page} is out of range ({total_pages} page(s) total for {total} contact(s))."

    start = (page - 1) * page_size
    page_contacts = contacts[start:start + page_size]
    account_map = _build_account_map()
    lines = [_format_contact(c, account_map, group_map) for c in page_contacts]

    header = f"Contacts {start + 1}-{start + len(lines)} of {total} (page {page}/{total_pages}):"
    footer = ""
    if page < total_pages:
        footer = f"\n\nMore results available: call list_contacts(query={query!r}, group={group!r}, page={page + 1}, page_size={page_size}) to continue."
    return header + "\n" + "\n".join(lines) + footer


# =========================================================
# TOOL 2: find likely duplicate contacts
# =========================================================
@mcp.tool()
def find_duplicate_contacts() -> str:
    """Finds contacts that are likely duplicates.

    Groups contacts that share the exact same full name (case-insensitive)
    or at least one identical phone number (compared by digits only, so
    formatting differences don't matter). Review the groups and use
    delete_contact to remove the ones you don't want to keep.
    """
    log.info("find_duplicate_contacts() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    contacts = _fetch_all_contacts()
    if not contacts:
        return "No contacts found."

    by_name = {}
    by_phone = {}
    for c in contacts:
        name_key = _full_name(c).lower()
        if name_key and name_key != "(no name)":
            by_name.setdefault(name_key, []).append(c)
        for p in c.phoneNumbers():
            digits = _normalize_phone(str(p.value().stringValue()))
            if digits:
                by_phone.setdefault(digits, []).append(c)

    seen_groups = []  # list of frozenset(identifier) to avoid reporting the same group twice
    lines = []

    def add_group(reason, group):
        ids = frozenset(c.identifier() for c in group)
        if len(ids) < 2 or ids in seen_groups:
            return
        seen_groups.append(ids)
        lines.append(f"\n{reason}:")
        for c in group:
            lines.append("  " + _format_contact(c))

    for name_key, group in by_name.items():
        if len(group) > 1:
            add_group(f"Same name '{_full_name(group[0])}'", group)

    for digits, group in by_phone.items():
        if len(group) > 1:
            add_group(f"Same phone number ({digits})", group)

    if not lines:
        return "No likely duplicates found."
    return f"Found {len(seen_groups)} potential duplicate group(s):" + "".join(lines)


# =========================================================
# TOOL 3: list contact accounts (containers)
# =========================================================
@mcp.tool()
def list_contact_accounts() -> str:
    """Lists the contact accounts (containers) available in Contacts.app,
    e.g. iCloud, Google, Exchange, On My Mac.

    Each line includes the account id, needed by create_contact's
    `account` argument to file a new contact under a specific account
    instead of the system default.
    """
    log.info("list_contact_accounts() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    containers, err = store.containersMatchingPredicate_error_(None, None)
    if containers is None:
        log.error("Failed to fetch containers: %s", err)
        return f"Failed to fetch accounts: {err}"
    if not containers:
        return "No contact accounts found."

    lines = []
    for c in containers:
        type_name = _CONTAINER_TYPES.get(c.type(), "Unknown")
        lines.append(f"- {c.name()} [{type_name}] (id: {c.identifier()})")
    return "Contact accounts:\n" + "\n".join(lines)


# =========================================================
# TOOL 3b: list contact groups (subfolders within an account)
# =========================================================
@mcp.tool()
def list_contact_groups() -> str:
    """Lists the contact groups ('subpastas', e.g. Family, Work) in Contacts.app.

    Groups live inside an account (see list_contact_accounts) and are how
    Contacts.app organizes contacts into subfolders. Each line includes the
    group id and its account, needed by create_contact's `groups` argument
    and update_contact's `add_to_groups`/`remove_from_groups` arguments —
    the group name also works in those, as a shortcut.
    """
    log.info("list_contact_groups() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    groups, err = store.groupsMatchingPredicate_error_(None, None)
    if groups is None:
        log.error("Failed to fetch groups: %s", err)
        return f"Failed to fetch groups: {err}"
    if not groups:
        return "No contact groups found."

    containers, _ = store.containersMatchingPredicate_error_(None, None)
    container_by_id = {c.identifier(): c.name() for c in containers or []}

    lines = []
    for g in sorted(groups, key=lambda g: (g.name() or "").lower()):
        predicate = CNContainer.predicateForContainerOfGroupWithIdentifier_(g.identifier())
        owning, err2 = store.containersMatchingPredicate_error_(predicate, None)
        account_name = container_by_id.get(owning[0].identifier()) if owning else "?"
        lines.append(f"- {g.name()} [account: {account_name}] (id: {g.identifier()})")
    return "Contact groups:\n" + "\n".join(lines)


# =========================================================
# TOOL 3c: create a contact group (subfolder)
# =========================================================
@mcp.tool()
def create_contact_group(name: str, account: str = "") -> str:
    """Creates a new contact group ('subpasta', e.g. Family, Work) in Contacts.app.

    Args:
        name: name of the new group.
        account: id of the account/container to create the group under (see
            list_contact_accounts). Empty = system default account.
    """
    log.info("create_contact_group(name=%r, account=%r) called", name, account)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    name = name.strip()
    if not name:
        return "name is required."

    group = CNMutableGroup.alloc().init()
    group.setName_(name)

    save_request = CNSaveRequest.alloc().init()
    save_request.addGroup_toContainerWithIdentifier_(group, account.strip() or None)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to create group: %s", err)
        return f"Failed to create group: {err}"

    log.info("Group created: %s (id: %s)", name, group.identifier())
    return f"Group created: {name} (id: {group.identifier()})"


# =========================================================
# TOOL 3d: rename a contact group (subfolder)
# =========================================================
@mcp.tool()
def update_contact_group(group_id: str, name: str) -> str:
    """Renames an existing contact group ('subpasta').

    Args:
        group_id: the group id or exact name, as shown by list_contact_groups.
        name: new name for the group.
    """
    log.info("update_contact_group(group_id=%r, name=%r) called", group_id, name)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    name = name.strip()
    if not name:
        return "name is required."

    ids, unknown = _resolve_group_ids(group_id)
    if unknown or not ids:
        return f"Group not found: '{group_id}'. Use list_contact_groups to get valid ids/names."

    all_groups, err = store.groupsMatchingPredicate_error_(None, None)
    if all_groups is None:
        log.error("Failed to fetch groups: %s", err)
        return f"Failed to fetch groups: {err}"
    group = next((g for g in all_groups if g.identifier() == ids[0]), None)
    if group is None:
        return f"Group not found: '{group_id}'. Use list_contact_groups to get valid ids/names."

    old_name = group.name()
    mutable_group = group.mutableCopy()
    mutable_group.setName_(name)

    save_request = CNSaveRequest.alloc().init()
    save_request.updateGroup_(mutable_group)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to rename group %s: %s", group_id, err)
        return f"Failed to rename group: {err}"

    log.info("Group renamed: %s -> %s (id: %s)", old_name, name, mutable_group.identifier())
    return f"Group renamed: '{old_name}' -> '{name}' (id: {mutable_group.identifier()})"


# =========================================================
# TOOL 3e: delete a contact group (subfolder)
# =========================================================
@mcp.tool()
def delete_contact_group(group_id: str) -> str:
    """Deletes a contact group ('subpasta'). Does not delete the contacts in it.

    Args:
        group_id: the group id or exact name, as shown by list_contact_groups.
    """
    log.info("delete_contact_group(group_id=%r) called", group_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    ids, unknown = _resolve_group_ids(group_id)
    if unknown or not ids:
        return f"Group not found: '{group_id}'. Use list_contact_groups to get valid ids/names."

    all_groups, err = store.groupsMatchingPredicate_error_(None, None)
    if all_groups is None:
        log.error("Failed to fetch groups: %s", err)
        return f"Failed to fetch groups: {err}"
    group = next((g for g in all_groups if g.identifier() == ids[0]), None)
    if group is None:
        return f"Group not found: '{group_id}'. Use list_contact_groups to get valid ids/names."

    name = group.name()
    save_request = CNSaveRequest.alloc().init()
    save_request.deleteGroup_(group.mutableCopy())

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to delete group %s: %s", group_id, err)
        return f"Failed to delete group: {err}"

    log.info("Group deleted: %s (id: %s)", name, ids[0])
    return f"Group deleted: {name}"


# =========================================================
# TOOL 4: create a new contact
# =========================================================
def _apply_phones(contact, phones: str):
    labeled = [
        CNLabeledValue.labeledValueWithLabel_value_(label, CNPhoneNumber.phoneNumberWithStringValue_(value))
        for label, value in _parse_labeled_list(phones, _PHONE_LABELS, "mobile")
    ]
    contact.setPhoneNumbers_(labeled)


def _apply_emails(contact, emails: str):
    labeled = [
        CNLabeledValue.labeledValueWithLabel_value_(label, value)
        for label, value in _parse_labeled_list(emails, _EMAIL_LABELS, "home")
    ]
    contact.setEmailAddresses_(labeled)


def _apply_addresses(contact, address: str):
    labeled = []
    for label, street, city, state, zip_code, country in _parse_address_list(address):
        addr = CNMutablePostalAddress.alloc().init()
        addr.setStreet_(street)
        addr.setCity_(city)
        addr.setState_(state)
        addr.setPostalCode_(zip_code)
        addr.setCountry_(country)
        labeled.append(CNLabeledValue.labeledValueWithLabel_value_(label, addr))
    contact.setPostalAddresses_(labeled)


def _apply_urls(contact, url: str):
    labeled = [
        CNLabeledValue.labeledValueWithLabel_value_(label, value)
        for label, value in _parse_labeled_list(url, _URL_LABELS, "homepage")
    ]
    contact.setUrlAddresses_(labeled)


def _apply_birthday(contact, birthday: str) -> bool:
    """Returns False if birthday text was given but couldn't be parsed."""
    if not birthday.strip():
        return True
    comps = _parse_birthday(birthday)
    if comps is None:
        return False
    contact.setBirthday_(comps)
    return True


@mcp.tool()
def create_contact(
    first_name: str,
    last_name: str = "",
    phones: str = "",
    emails: str = "",
    organization: str = "",
    account: str = "",
    birthday: str = "",
    address: str = "",
    url: str = "",
    notes: str = "",
    groups: str = "",
) -> str:
    """Creates a new contact in Contacts.app.

    Args:
        first_name: given name.
        last_name: family name.
        phones: comma-separated phone numbers, optionally labeled, e.g.
            'mobile:+55 11 99999-9999, home:1122223333'. A bare number with
            no label defaults to 'mobile'. Labels: mobile, iphone, home,
            personal, work, main, other.
        emails: comma-separated emails, optionally labeled, e.g.
            'work:eduardo@company.com, home:eduardo@gmail.com'. A bare
            address defaults to 'home'. Labels: home, personal, work, other.
        organization: company/organization name.
        account: id of the account/container to file the contact under
            (see list_contact_accounts). Empty = system default account.
        birthday: 'YYYY-MM-DD' (with year) or 'MM-DD' (no year), e.g.
            '1990-05-20' or '05-20'.
        address: postal address(es) as 'label:street|city|state|zip|country',
            multiple separated by ';', e.g. 'home:Rua X 123|São Paulo|SP|01000-000|Brasil'.
            A chunk with no label defaults to 'home'. Labels: home, personal, work, other.
            Missing subfields (e.g. no country) can be left blank between the '|'s.
        url: comma-separated URLs, optionally labeled, e.g.
            'homepage:https://example.com, work:https://company.com'. A bare
            URL defaults to 'homepage'. Labels: homepage, home, work, other.
        notes: free-text notes. May silently fail to save: Apple restricts
            programmatic access to Contacts notes to apps with a special
            entitlement, which this server likely doesn't have.
        groups: comma-separated group ids or names (see list_contact_groups)
            to add the new contact to, e.g. 'Family, Work'. Empty = no group.
    """
    log.info(
        "create_contact(first_name=%r, last_name=%r, phones=%r, emails=%r, account=%r, birthday=%r, groups=%r) called",
        first_name, last_name, phones, emails, account, birthday, groups,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if not first_name.strip() and not last_name.strip() and not organization.strip():
        return "At least a first_name, last_name or organization is required."

    group_ids, unknown_groups = ([], [])
    if groups.strip():
        group_ids, unknown_groups = _resolve_group_ids(groups)
        if unknown_groups:
            return f"Group(s) not found: {', '.join(unknown_groups)}. Use list_contact_groups to get valid ids/names."

    contact = CNMutableContact.alloc().init()
    contact.setGivenName_(first_name.strip())
    contact.setFamilyName_(last_name.strip())
    if organization.strip():
        contact.setOrganizationName_(organization.strip())

    if phones.strip():
        _apply_phones(contact, phones)

    if emails.strip():
        _apply_emails(contact, emails)

    if address.strip():
        _apply_addresses(contact, address)

    if url.strip():
        _apply_urls(contact, url)

    if not _apply_birthday(contact, birthday):
        return f"Could not parse birthday '{birthday}'. Use 'YYYY-MM-DD' or 'MM-DD'."

    note_warning = ""
    if notes.strip() and _notes_supported is not False:
        try:
            contact.setNote_(notes.strip())
        except Exception as e:
            log.warning("Could not set note on new contact: %s", e)
            note_warning = " (notes could not be set — this app likely lacks the Contacts notes entitlement)"

    save_request = CNSaveRequest.alloc().init()
    save_request.addContact_toContainerWithIdentifier_(contact, account.strip() or None)

    if group_ids:
        all_groups, _ = store.groupsMatchingPredicate_error_(None, None)
        group_by_id = {g.identifier(): g for g in all_groups or []}
        for gid in group_ids:
            group = group_by_id.get(gid)
            if group is not None:
                save_request.addMember_toGroup_(contact, group)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to create contact: %s", err)
        return f"Failed to create contact: {err}"

    group_note = f", added to group(s): {groups}" if group_ids else ""
    log.info("Contact created: %s", contact.identifier())
    return f"Contact created: {_full_name(contact)} (id: {contact.identifier()}){note_warning}{group_note}"


# =========================================================
# TOOL 5: update an existing contact
# =========================================================
@mcp.tool()
def update_contact(
    contact_id: str,
    first_name: str = "",
    last_name: str = "",
    phones: str = "",
    emails: str = "",
    organization: str = "",
    account: str = "",
    birthday: str = "",
    address: str = "",
    url: str = "",
    notes: str = "",
    add_to_groups: str = "",
    remove_from_groups: str = "",
) -> str:
    """Updates an existing contact. Only the fields you pass are changed.

    Passing phones, emails, address or url REPLACES the entire existing
    list for that field (it does not append) — pass the full desired list,
    e.g. to keep an old number and add a new one, include both.

    Args:
        contact_id: the contact id, as shown by list_contacts.
        first_name: new given name. Empty = leave unchanged.
        last_name: new family name. Empty = leave unchanged.
        phones: new full list of phone numbers (see create_contact for the
            'label:value' format). Empty = leave unchanged, 'none' clears.
        emails: new full list of emails (see create_contact for the
            'label:value' format). Empty = leave unchanged, 'none' clears.
        organization: new organization name. Empty = leave unchanged.
        account: id of the account/container to move the contact to (see
            list_contact_accounts). Empty = leave in its current account.
        birthday: 'YYYY-MM-DD' or 'MM-DD' (see create_contact). Empty =
            leave unchanged, 'none' clears.
        address: new full list of postal addresses (see create_contact for
            the 'label:street|city|state|zip|country' format). Empty =
            leave unchanged, 'none' clears.
        url: new full list of URLs (see create_contact for the
            'label:value' format). Empty = leave unchanged, 'none' clears.
        notes: new notes text. Empty = leave unchanged, 'none' clears. May
            silently fail: Apple restricts programmatic access to Contacts
            notes to apps with a special entitlement, which this server
            likely doesn't have.
        add_to_groups: comma-separated group ids or names (see
            list_contact_groups) to add the contact to, e.g. 'Family, Work'.
        remove_from_groups: comma-separated group ids or names to remove the
            contact from.
    """
    log.info(
        "update_contact(contact_id=%r, first_name=%r, last_name=%r, phones=%r, emails=%r, account=%r, birthday=%r, "
        "add_to_groups=%r, remove_from_groups=%r) called",
        contact_id, first_name, last_name, phones, emails, account, birthday, add_to_groups, remove_from_groups,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    contact_id = contact_id.strip()
    existing, err = _fetch_contact(contact_id)
    if existing is None:
        log.error("Contact not found: %s (%s)", contact_id, err)
        return f"Contact not found: {contact_id}. Use list_contacts to get ids."

    add_group_ids, unknown_add = ([], [])
    if add_to_groups.strip():
        add_group_ids, unknown_add = _resolve_group_ids(add_to_groups)
        if unknown_add:
            return f"Group(s) not found: {', '.join(unknown_add)}. Use list_contact_groups to get valid ids/names."

    remove_group_ids, unknown_remove = ([], [])
    if remove_from_groups.strip():
        remove_group_ids, unknown_remove = _resolve_group_ids(remove_from_groups)
        if unknown_remove:
            return f"Group(s) not found: {', '.join(unknown_remove)}. Use list_contact_groups to get valid ids/names."

    contact = existing.mutableCopy()
    changes = []
    note_warning = ""

    if first_name.strip():
        contact.setGivenName_(first_name.strip())
        changes.append("first name")
    if last_name.strip():
        contact.setFamilyName_(last_name.strip())
        changes.append("last name")
    if organization.strip():
        contact.setOrganizationName_(organization.strip())
        changes.append("organization")
    if phones.strip():
        _apply_phones(contact, "" if phones.strip().lower() == "none" else phones)
        changes.append("phones")
    if emails.strip():
        _apply_emails(contact, "" if emails.strip().lower() == "none" else emails)
        changes.append("emails")
    if address.strip():
        _apply_addresses(contact, "" if address.strip().lower() == "none" else address)
        changes.append("address")
    if url.strip():
        _apply_urls(contact, "" if url.strip().lower() == "none" else url)
        changes.append("url")
    if birthday.strip():
        if birthday.strip().lower() == "none":
            contact.setBirthday_(None)
        elif not _apply_birthday(contact, birthday):
            return f"Could not parse birthday '{birthday}'. Use 'YYYY-MM-DD' or 'MM-DD'."
        changes.append("birthday")
    if notes.strip() and _notes_supported is not False:
        try:
            contact.setNote_("" if notes.strip().lower() == "none" else notes.strip())
            changes.append("notes")
        except Exception as e:
            log.warning("Could not set note on contact %s: %s", contact_id, e)
            note_warning = " (notes could not be set — this app likely lacks the Contacts notes entitlement)"

    account_id = account.strip()
    moving = False
    if account_id:
        current_container_id = _contact_container_id(contact_id)
        moving = current_container_id != account_id

    if not changes and not moving and not add_group_ids and not remove_group_ids:
        return "Nothing to update: no field was provided."

    all_groups = None
    if add_group_ids or remove_group_ids:
        all_groups, _ = store.groupsMatchingPredicate_error_(None, None)
    group_by_id = {g.identifier(): g for g in all_groups or []}
    group_note = ""
    if add_group_ids or remove_group_ids:
        parts = []
        if add_group_ids:
            parts.append(f"added to {', '.join(g.name() for gid in add_group_ids if (g := group_by_id.get(gid)))}")
        if remove_group_ids:
            parts.append(f"removed from {', '.join(g.name() for gid in remove_group_ids if (g := group_by_id.get(gid)))}")
        group_note = f" — {'; '.join(parts)}" if parts else ""

    save_request = CNSaveRequest.alloc().init()

    if moving:
        new_contact = _clone_contact_for_move(contact)
        save_request.addContact_toContainerWithIdentifier_(new_contact, account_id)
        save_request.deleteContact_(existing.mutableCopy())
        for gid in add_group_ids:
            group = group_by_id.get(gid)
            if group is not None:
                save_request.addMember_toGroup_(new_contact, group)

        ok, err = store.executeSaveRequest_error_(save_request, None)
        if not ok:
            log.error("Failed to move contact: %s", err)
            return f"Failed to move contact: {err}"

        change_note = f" — changed: {', '.join(changes)}" if changes else ""
        log.info(
            "Contact moved: old id %s -> new id %s (account %s)%s",
            contact_id, new_contact.identifier(), account_id, change_note,
        )
        return (
            f"Contact moved to another account: {_full_name(new_contact)}{change_note}{note_warning}{group_note} "
            f"(new id: {new_contact.identifier()}, old id {contact_id} removed)"
        )

    if changes:
        save_request.updateContact_(contact)
    for gid in add_group_ids:
        group = group_by_id.get(gid)
        if group is not None:
            save_request.addMember_toGroup_(contact, group)
    for gid in remove_group_ids:
        group = group_by_id.get(gid)
        if group is not None:
            save_request.removeMember_fromGroup_(contact, group)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to update contact: %s", err)
        return f"Failed to update contact: {err}"

    log.info("Contact updated: %s (%s)", contact_id, ", ".join(changes))
    changed_note = f" — changed: {', '.join(changes)}" if changes else ""
    return f"Contact updated: {_full_name(contact)}{changed_note}{note_warning}{group_note} (id: {contact_id})"


# =========================================================
# TOOL 6: delete a contact
# =========================================================
@mcp.tool()
def delete_contact(contact_id: str) -> str:
    """Deletes a contact from Contacts.app.

    Args:
        contact_id: the contact id, as shown by list_contacts or
            find_duplicate_contacts.
    """
    log.info("delete_contact(contact_id=%r) called", contact_id)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    contact_id = contact_id.strip()
    existing, err = _fetch_contact(contact_id)
    if existing is None:
        log.error("Contact not found: %s (%s)", contact_id, err)
        return f"Contact not found: {contact_id}. Use list_contacts to get ids."

    name = _full_name(existing)
    contact = existing.mutableCopy()

    save_request = CNSaveRequest.alloc().init()
    save_request.deleteContact_(contact)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to delete contact: %s", err)
        return f"Failed to delete contact: {err}"

    still_there, _ = store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, [CNContactIdentifierKey], None)
    if still_there is not None:
        log.warning("Contact %s still present after delete (likely account sync, e.g. Google)", contact_id)
        return (
            f"Delete for '{name}' was accepted locally, but the contact is still present — "
            "this usually means it belongs to a synced account (Google, iCloud, Exchange) and "
            "the deletion hasn't propagated to the server yet. Check the account's sync status "
            "and try again in a bit."
        )

    log.info("Contact deleted: %s", contact_id)
    return f"Contact deleted: {name}"


# --- Entry point ---
# Runs the server over stdio, which is how Claude Desktop connects.
if __name__ == "__main__":
    log.info("Entering mcp.run()")
    mcp.run()
