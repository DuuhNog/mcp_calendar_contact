# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Bruno_Personal_Mcp** — a personal MCP server for Claude Desktop that will grow to cover multiple personal integrations (calendar, contacts, email, etc.). Modules:

- [bruno_calendar_mcp.py](bruno_calendar_mcp.py) — exposes the Apple Calendar (macOS) via EventKit: listing calendars/events, creating, deleting, and rescheduling events.
- [bruno_contact_mcp.py](bruno_contact_mcp.py) — exposes Contacts.app (macOS) via the Contacts framework: listing/searching contacts, finding likely duplicates, creating, updating, and deleting contacts.

## How to run

```bash
python3 bruno_calendar_mcp.py
python3 bruno_contact_mcp.py
```

The server communicates over stdio (that's how Claude Desktop connects), so running it directly in a terminal only verifies it starts without errors. Real testing happens through Claude Desktop, by registering the server in `claude_desktop_config.json`.

Live debugging (all logging goes to a file, never to stdout — stdout is the MCP protocol channel):

```bash
tail -f ~/bruno_mcp_debug.log
```

There are no tests, linter, or build configured.

## Dependencies

- `mcp` (MCP Python SDK, uses `FastMCP`)
- `pyobjc` (`EventKit`/`Foundation` frameworks for calendar, `Contacts` framework for contacts)

macOS only. On first run the system shows a permission pop-up per data source (Settings > Privacy > Calendar / Contacts).

## Architecture

- `FastMCP("bruno-calendar")` handles the whole protocol; each tool is a function decorated with `@mcp.tool()` that takes/returns plain strings.
- Existing tools: `list_calendars()`, `create_calendar(name, color, account)` (color belongs to the calendar in Apple Calendar — events inherit it; there is no per-event color in EventKit), `list_events(days_ahead)`, `create_event(...)` (supports recurrence via `repeat`/`repeat_interval`/`repeat_until`; recurring saves need `EKSpanFutureEvents`; supports reminders via `alerts`, a comma-separated list of minutes-before-event, e.g. `'10,60'`, built into `EKAlarm`s via `_parse_alerts()`), `delete_event(event_id, scope, from_date)`, `reschedule_event(event_id, new_start, new_end, alerts)` (also the tool for changing alerts on an existing event — `alerts=''` leaves them unchanged, `alerts='none'` clears them, anything else replaces them). Write tools target events by the id shown in `list_events` output.
- **Deleting "from date X onward" must be ONE `delete_event` call** with `scope='this_and_future'` and `from_date=X` (the server re-anchors to the first occurrence on/after that date via `calendarItemExternalIdentifier`). Repeated calls on multiple occurrences truncate the series progressively backwards and can destroy past events — this happened once; there is a guard refusing past-dated truncation without an explicit `from_date`.
- **Recurring-event gotcha**: `eventWithIdentifier_` returns the *first* occurrence of a series even when given an occurrence-specific id (`..._N` suffix). `_find_event()` handles this by falling back to a predicate search for the exact occurrence id — never call `eventWithIdentifier_` directly for occurrence-level operations. `delete_event` takes `scope` ('this' | 'this_and_future' | 'all', mapped to `EKSpanThisEvent`/`EKSpanFutureEvents`) and verifies the occurrence is actually gone before reporting success.
- `_ensure_access()` blocks on a `threading.Event` until macOS's async authorization callback responds (60s timeout). Every tool must call it before touching the `EKEventStore`. It uses `requestFullAccessToEventsWithCompletion_` (macOS 14+, required for writes) with a fallback to the older `requestAccessToEntityType_completion_`.
- Writes go through `saveEvent_span_error_` / `removeEvent_span_error_` with `EKSpanThisEvent`; in pyobjc these return a `(success, NSError)` tuple when `None` is passed for the error out-param.
- Date conversion: EventKit uses `NSDate`; convert with `NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())` going in and `datetime.fromtimestamp(nsdate.timeIntervalSince1970())` coming back.
- Objective-C methods via pyobjc use underscores instead of colons (e.g. `predicateForEventsWithStartDate_endDate_calendars_`).

### bruno_contact_mcp.py

- `FastMCP("bruno-contact")`. Tools: `list_contacts(query)` (client-side substring filter over name/org/phone/email — `CNContactStore` has no simple full-text predicate), `find_duplicate_contacts()` (groups by exact case-insensitive full name or by phone number compared digits-only), `create_contact(...)`, `update_contact(contact_id, ...)`, `delete_contact(contact_id)`. Write tools target contacts by the id shown in `list_contacts`/`find_duplicate_contacts` output.
- `phones`/`emails` args use a `'label:value, label:value'` mini-format (e.g. `'mobile:+55 11 99999-9999, home:1122223333'`); a bare value with no label defaults to `mobile` for phones / `home` for emails. `update_contact` REPLACES the whole list for a field when passed, it does not append — pass the full desired list.
- `_ensure_access()` mirrors the calendar module's pattern (`threading.Event` blocking on the async `requestAccessForEntityType_completionHandler_` callback, 60s timeout) but for `CNContactStore`/`CNEntityTypeContacts`.
- Writes go through a single `CNSaveRequest` per call (`addContact_toContainerWithIdentifier_` / `updateContact_` / `deleteContact_`) executed via `store.executeSaveRequest_error_`. Contacts fetched via `unifiedContactWithIdentifier_keysToFetch_error_` are immutable (`CNContact`); call `.mutableCopy()` before editing, since only `CNMutableContact` can be saved.
- `find_duplicate_contacts()` is a heuristic aid, not an automatic merge/delete — it only lists candidate groups; the user decides what to remove via `delete_contact`.

## Important constraints

- **Never write to stdout/stderr outside the MCP protocol** (no `print`): it corrupts the stdio communication with Claude Desktop. Use the `log` logger, which writes to `~/bruno_mcp_debug.log`.
- New integrations (email, etc.) should follow the same pattern: `@mcp.tool()` functions, file-based logging, explicit permission handling.
