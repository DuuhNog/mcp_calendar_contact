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
    CNContactStore,
    CNEntityTypeContacts,
    CNMutableContact,
    CNContactFetchRequest,
    CNSaveRequest,
    CNLabeledValue,
    CNPhoneNumber,
    CNLabelHome,
    CNLabelWork,
    CNLabelOther,
    CNLabelPhoneNumberMobile,
    CNLabelPhoneNumberMain,
    CNLabelPhoneNumberiPhone,
    CNContactGivenNameKey,
    CNContactFamilyNameKey,
    CNContactOrganizationNameKey,
    CNContactPhoneNumbersKey,
    CNContactEmailAddressesKey,
    CNContactIdentifierKey,
)

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
_KEYS = [
    CNContactGivenNameKey,
    CNContactFamilyNameKey,
    CNContactOrganizationNameKey,
    CNContactPhoneNumbersKey,
    CNContactEmailAddressesKey,
    CNContactIdentifierKey,
]

_PHONE_LABELS = {
    "mobile": CNLabelPhoneNumberMobile,
    "iphone": CNLabelPhoneNumberiPhone,
    "home": CNLabelHome,
    "work": CNLabelWork,
    "main": CNLabelPhoneNumberMain,
    "other": CNLabelOther,
}

_EMAIL_LABELS = {
    "home": CNLabelHome,
    "work": CNLabelWork,
    "other": CNLabelOther,
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


def _fetch_all_contacts():
    """Fetches every contact (given/family name, org, phones, emails, id)."""
    request = CNContactFetchRequest.alloc().initWithKeysToFetch_(_KEYS)
    contacts = []
    ok, err = store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request, None, lambda contact, stop: contacts.append(contact)
    )
    if not ok:
        log.error("Failed to enumerate contacts: %s", err)
    return contacts


def _full_name(c) -> str:
    name = f"{c.givenName() or ''} {c.familyName() or ''}".strip()
    if not name:
        name = c.organizationName() or "(no name)"
    return name


def _format_contact(c) -> str:
    """Formats one contact as a listing line, including its id."""
    name = _full_name(c)
    phones = ", ".join(str(p.value().stringValue()) for p in c.phoneNumbers())
    emails = ", ".join(str(e.value()) for e in c.emailAddresses())
    org = c.organizationName() or ""
    extras = []
    if org:
        extras.append(f"[{org}]")
    if phones:
        extras.append(f"tel: {phones}")
    if emails:
        extras.append(f"email: {emails}")
    extras.append(f"(id: {c.identifier()})")
    return f"- {name} " + " | ".join(extras)


# --- Create the MCP server ---
# "bruno-contact" is the name shown to Claude.
mcp = FastMCP("bruno-contact")


# =========================================================
# TOOL 1: list contacts (optionally filtered by name)
# =========================================================
@mcp.tool()
def list_contacts(query: str = "") -> str:
    """Lists contacts from Contacts.app, with id, name, phones and emails.

    Each line includes the contact id, needed by update_contact and
    delete_contact.

    Args:
        query: optional case-insensitive substring to filter by name,
            organization, phone or email. Empty = list everyone.
    """
    log.info("list_contacts(query=%r) called", query)
    if not _ensure_access():
        return ACCESS_DENIED_MSG

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
    lines = [_format_contact(c) for c in contacts]
    return f"Contacts ({len(lines)}):\n" + "\n".join(lines)


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
# TOOL 3: create a new contact
# =========================================================
@mcp.tool()
def create_contact(
    first_name: str,
    last_name: str = "",
    phones: str = "",
    emails: str = "",
    organization: str = "",
) -> str:
    """Creates a new contact in Contacts.app.

    Args:
        first_name: given name.
        last_name: family name.
        phones: comma-separated phone numbers, optionally labeled, e.g.
            'mobile:+55 11 99999-9999, home:1122223333'. A bare number with
            no label defaults to 'mobile'. Labels: mobile, iphone, home,
            work, main, other.
        emails: comma-separated emails, optionally labeled, e.g.
            'work:eduardo@company.com, home:eduardo@gmail.com'. A bare
            address defaults to 'home'. Labels: home, work, other.
        organization: company/organization name.
    """
    log.info(
        "create_contact(first_name=%r, last_name=%r, phones=%r, emails=%r) called",
        first_name, last_name, phones, emails,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if not first_name.strip() and not last_name.strip() and not organization.strip():
        return "At least a first_name, last_name or organization is required."

    contact = CNMutableContact.alloc().init()
    contact.setGivenName_(first_name.strip())
    contact.setFamilyName_(last_name.strip())
    if organization.strip():
        contact.setOrganizationName_(organization.strip())

    if phones.strip():
        labeled = [
            CNLabeledValue.labeledValueWithLabel_value_(label, CNPhoneNumber.phoneNumberWithStringValue_(value))
            for label, value in _parse_labeled_list(phones, _PHONE_LABELS, "mobile")
        ]
        contact.setPhoneNumbers_(labeled)

    if emails.strip():
        labeled = [
            CNLabeledValue.labeledValueWithLabel_value_(label, value)
            for label, value in _parse_labeled_list(emails, _EMAIL_LABELS, "home")
        ]
        contact.setEmailAddresses_(labeled)

    save_request = CNSaveRequest.alloc().init()
    save_request.addContact_toContainerWithIdentifier_(contact, None)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to create contact: %s", err)
        return f"Failed to create contact: {err}"

    log.info("Contact created: %s", contact.identifier())
    return f"Contact created: {_full_name(contact)} (id: {contact.identifier()})"


# =========================================================
# TOOL 4: update an existing contact
# =========================================================
@mcp.tool()
def update_contact(
    contact_id: str,
    first_name: str = "",
    last_name: str = "",
    phones: str = "",
    emails: str = "",
    organization: str = "",
) -> str:
    """Updates an existing contact. Only the fields you pass are changed.

    Passing phones or emails REPLACES the entire existing list for that
    field (it does not append) — pass the full desired list, e.g. to keep
    an old number and add a new one, include both.

    Args:
        contact_id: the contact id, as shown by list_contacts.
        first_name: new given name. Empty = leave unchanged.
        last_name: new family name. Empty = leave unchanged.
        phones: new full list of phone numbers (see create_contact for the
            'label:value' format). Empty = leave unchanged.
        emails: new full list of emails (see create_contact for the
            'label:value' format). Empty = leave unchanged.
        organization: new organization name. Empty = leave unchanged.
    """
    log.info(
        "update_contact(contact_id=%r, first_name=%r, last_name=%r, phones=%r, emails=%r) called",
        contact_id, first_name, last_name, phones, emails,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    contact_id = contact_id.strip()
    existing, err = store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, _KEYS, None)
    if existing is None:
        log.error("Contact not found: %s (%s)", contact_id, err)
        return f"Contact not found: {contact_id}. Use list_contacts to get ids."

    contact = existing.mutableCopy()
    changes = []

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
        labeled = [
            CNLabeledValue.labeledValueWithLabel_value_(label, CNPhoneNumber.phoneNumberWithStringValue_(value))
            for label, value in _parse_labeled_list(phones, _PHONE_LABELS, "mobile")
        ]
        contact.setPhoneNumbers_(labeled)
        changes.append("phones")
    if emails.strip():
        labeled = [
            CNLabeledValue.labeledValueWithLabel_value_(label, value)
            for label, value in _parse_labeled_list(emails, _EMAIL_LABELS, "home")
        ]
        contact.setEmailAddresses_(labeled)
        changes.append("emails")

    if not changes:
        return "Nothing to update: no field was provided."

    save_request = CNSaveRequest.alloc().init()
    save_request.updateContact_(contact)

    ok, err = store.executeSaveRequest_error_(save_request, None)
    if not ok:
        log.error("Failed to update contact: %s", err)
        return f"Failed to update contact: {err}"

    log.info("Contact updated: %s (%s)", contact_id, ", ".join(changes))
    return f"Contact updated: {_full_name(contact)} — changed: {', '.join(changes)} (id: {contact_id})"


# =========================================================
# TOOL 5: delete a contact
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
    existing, err = store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, _KEYS, None)
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

    log.info("Contact deleted: %s", contact_id)
    return f"Contact deleted: {name}"


# --- Entry point ---
# Runs the server over stdio, which is how Claude Desktop connects.
if __name__ == "__main__":
    log.info("Entering mcp.run()")
    mcp.run()
