#!/usr/bin/env python3
# bruno_calendar_mcp.py
# MCP server: lists, creates, deletes and reschedules Apple Calendar (macOS)
# events via EventKit. With debug logging to a file.

# --- MCP SDK imports ---
# FastMCP handles the whole MCP protocol. You only write decorated functions.
from mcp.server.fastmcp import FastMCP

# EventKit is the native macOS framework for accessing the Calendar.
# It comes via pyobjc; we import the classes we need.
from EventKit import (
    EKEventStore,
    EKEntityTypeEvent,
    EKEvent,
    EKAlarm,
    EKCalendar,
    EKSpanThisEvent,
    EKSpanFutureEvents,
    EKRecurrenceRule,
    EKRecurrenceEnd,
    EKRecurrenceFrequencyDaily,
    EKRecurrenceFrequencyWeekly,
    EKRecurrenceFrequencyMonthly,
    EKRecurrenceFrequencyYearly,
)

# Foundation provides macOS date support (NSDate) used by EventKit.
from Foundation import NSDate, NSTimeZone, NSURL

# datetime to build time ranges in a readable way in Python.
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Literal

# logging for debug: writes everything to a file you can follow live.
import logging
import os
import threading

# --- Debug log configuration ---
# The log goes to your home directory. Follow it with: tail -f ~/bruno_mcp_debug.log
LOG_PATH = os.path.expanduser("~/bruno_mcp_debug.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,  # DEBUG = logs absolutely everything
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bruno-calendar")
log.info("=== Server starting ===")

# --- EventStore instance ---
# EKEventStore is the central object that talks to the Calendar database.
store = EKEventStore.alloc().init()

ACCESS_DENIED_MSG = "Calendar access denied. Check Settings > Privacy."


def _ensure_access() -> bool:
    """Ensures calendar access permission.

    On the first run macOS shows a pop-up asking for authorization.
    We need to wait for the user's response, so we use an Event
    to block until the async callback returns.
    """
    log.debug("Requesting calendar access...")
    done = threading.Event()      # synchronizes the async callback
    result = {"granted": False}   # stores the callback result

    def handler(granted, err):
        # Callback invoked by macOS with the user's decision.
        result["granted"] = bool(granted)
        if err:
            log.error("Error requesting access: %s", err)
        log.debug("Access granted? %s", result["granted"])
        done.set()  # releases the wait() below

    # macOS 14+ requires full access for writing; fall back to the old API.
    if store.respondsToSelector_("requestFullAccessToEventsWithCompletion:"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, handler)
    done.wait(timeout=60)  # waits up to 60s for the user's response
    return result["granted"]


def _to_nsdate(dt: datetime) -> NSDate:
    """Converts a Python datetime to NSDate (via Unix timestamp)."""
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _to_datetime(nsdate) -> datetime:
    """Converts an NSDate back to a Python datetime."""
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970())


def _to_nsdate_in_tz(dt: datetime, tz) -> NSDate:
    """Converts a naive wall-clock datetime to NSDate, interpreting that
    wall-clock time IN the given NSTimeZone, rather than localizing it to
    the device's zone and converting (which shifts the instant by the
    offset difference between the two zones — see the reschedule_event/
    create_event timezone gotcha in CLAUDE.md).

    tz=None (floating) falls back to local-time interpretation, matching
    EventKit's floating-event semantics (device-local wall clock, no
    explicit zone).
    """
    if tz is None:
        return _to_nsdate(dt)
    aware = dt.replace(tzinfo=ZoneInfo(str(tz.name())))
    return NSDate.dateWithTimeIntervalSince1970_(aware.timestamp())


def _parse_dt(text: str) -> datetime:
    """Parses 'YYYY-MM-DD HH:MM' (or ISO 8601) into a datetime."""
    return datetime.fromisoformat(text.strip())


def _parse_alerts(text: str):
    """Parses 'minutes,minutes,...' (minutes before the event) into EKAlarms.

    0 = alert at the event's start time. Raises ValueError on bad input.
    """
    alarms = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        minutes = int(chunk)
        if minutes < 0:
            raise ValueError("alert minutes cannot be negative")
        alarms.append(EKAlarm.alarmWithRelativeOffset_(-minutes * 60))
    return alarms


_TIMEZONE_UNCHANGED = object()  # sentinel: caller passed '' (only relevant on reschedule)


def _parse_timezone(text: str):
    """Parses a timezone argument into an NSTimeZone, or None for 'floating'.

    '' means "leave unchanged" (only meaningful on reschedule_event) and is
    represented by the _TIMEZONE_UNCHANGED sentinel. 'floating'/'none' means
    an explicit floating time (EventKit treats timeZone=nil as floating:
    the event keeps the same wall-clock time in any time zone). Anything
    else must be an IANA name (e.g. 'America/Sao_Paulo'). Raises ValueError
    on an unrecognized name.
    """
    text = text.strip()
    if not text:
        return _TIMEZONE_UNCHANGED
    if text.lower() in ("floating", "none"):
        return None
    tz = NSTimeZone.timeZoneWithName_(text)
    if tz is None:
        raise ValueError(f"unrecognized time zone '{text}'")
    return tz


def _find_event(event_id: str):
    """Looks up an event (or the exact occurrence of a series) by identifier.

    eventWithIdentifier_ returns the FIRST occurrence of a recurring series,
    even when given an occurrence-specific id (suffix _N). Acting on that
    object would hit the wrong occurrence, so when the returned id doesn't
    match we search a wide date window for the exact occurrence.
    Returns None if not found.
    """
    event_id = event_id.strip()
    store.refreshSourcesIfNecessary()
    event = store.eventWithIdentifier_(event_id)
    if event is not None and str(event.eventIdentifier()) == event_id:
        return event

    # Recurring series: search occurrences for the exact id.
    now = datetime.now()
    window_start = now - timedelta(days=365)
    window_end = now + timedelta(days=730)
    calendars = [event.calendar()] if event is not None else None
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(window_start), _to_nsdate(window_end), calendars
    )
    for occ in store.eventsMatchingPredicate_(pred):
        if str(occ.eventIdentifier()) == event_id:
            return occ

    log.warning("Occurrence id %s not found in search window", event_id)
    return None


def _first_occurrence_from(event, from_dt: datetime):
    """Finds the first occurrence of the same series starting at/after from_dt.

    Occurrences of one series share calendarItemExternalIdentifier, which is
    stable across the id-suffix scheme (_N) used by synced calendars.
    """
    ext = str(event.calendarItemExternalIdentifier())
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(from_dt),
        _to_nsdate(from_dt + timedelta(days=730)),
        [event.calendar()],
    )
    occs = [
        o for o in store.eventsMatchingPredicate_(pred)
        if str(o.calendarItemExternalIdentifier()) == ext
    ]
    if not occs:
        return None
    return min(occs, key=lambda o: o.startDate().timeIntervalSince1970())


def _occurrence_still_exists(window_start: datetime, window_end: datetime,
                             event_id: str) -> bool:
    """Checks whether the given occurrence id still exists after a write.

    The window must be captured BEFORE the removal (the event object may be
    invalidated by it). We reset() the store first: it drops every cached
    object and forces a re-read from the Calendar database, so the check
    sees committed state instead of this process's in-memory snapshot —
    an in-memory-only removal would otherwise look like a success.
    """
    store.reset()
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(window_start), _to_nsdate(window_end), None
    )
    return any(
        str(occ.eventIdentifier()) == event_id
        for occ in store.eventsMatchingPredicate_(pred)
    )


# Reference points for naming a calendar's RGB color.
_COLOR_NAMES = {
    "red": (1.0, 0.0, 0.0),
    "orange": (1.0, 0.55, 0.0),
    "yellow": (1.0, 0.9, 0.1),
    "green": (0.2, 0.7, 0.2),
    "blue": (0.1, 0.4, 0.9),
    "purple": (0.6, 0.2, 0.8),
    "pink": (1.0, 0.5, 0.7),
    "brown": (0.55, 0.35, 0.2),
    "gray": (0.5, 0.5, 0.5),
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
}


def _calendar_color(cal) -> str:
    """Returns 'name (#RRGGBB)' for a calendar's color, or '' if unknown."""
    try:
        from AppKit import NSColorSpace
        ns = cal.color()  # NSColor (AppKit ships with pyobjc EventKit deps)
        rgb = ns.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb is None:
            return ""
        r, g, b = rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent()
        name = min(
            _COLOR_NAMES,
            key=lambda k: sum(
                (a - b_) ** 2
                for a, b_ in zip(_COLOR_NAMES[k], (r, g, b))
            ),
        )
        return f"{name} (#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X})"
    except Exception as exc:  # color is cosmetic; never break the listing
        log.debug("Could not read calendar color: %s", exc)
        return ""


def _format_event(e) -> str:
    """Formats one event as a listing line, including its id."""
    ev_start = _to_datetime(e.startDate())
    ev_end = _to_datetime(e.endDate())
    cal_name = e.calendar().title()
    title = e.title() or "(no title)"
    recurring = ", recurring" if e.hasRecurrenceRules() else ""
    return (
        f"- {ev_start:%d/%m %H:%M}–{ev_end:%H:%M} | {title} [{cal_name}]"
        f" (id: {e.eventIdentifier()}{recurring})"
    )


# --- Create the MCP server ---
# "bruno-calendar" is the name shown to Claude.
mcp = FastMCP("bruno-calendar")


# =========================================================
# TOOL 1: list all available calendars
# =========================================================
@mcp.tool()
def list_calendars() -> str:
    """Lists all Apple Calendar calendars (name, id, color, writability).

    Events inherit their calendar's color; to create an event "in red",
    pass the calendar_id of a red calendar to create_event.
    """
    log.info("list_calendars() called")
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    # calendarsForEntityType_ returns all event calendars.
    cals = store.calendarsForEntityType_(EKEntityTypeEvent)
    log.debug("Found %d calendars", len(cals))

    if not cals:
        return "No calendars found."

    lines = []
    for c in cals:
        # title() = name; calendarIdentifier() = unique id for filtering later.
        writable = "writable" if c.allowsContentModifications() else "read-only"
        color = _calendar_color(c)
        color_part = f", color: {color}" if color else ""
        lines.append(
            f"- {c.title()} (id: {c.calendarIdentifier()}, {writable}{color_part})"
        )
    return "Calendars:\n" + "\n".join(lines)


# =========================================================
# TOOL: create a new calendar
# =========================================================
@mcp.tool()
def create_calendar(
    name: str,
    color: Literal[
        "default", "red", "orange", "yellow", "green",
        "blue", "purple", "pink", "brown", "gray",
    ] = "default",
    account: str = "",
) -> str:
    """Creates a new calendar in Apple Calendar.

    In Apple Calendar, color belongs to the calendar (events inherit it),
    so this is where a color can actually be chosen.

    Args:
        name: the calendar's name.
        color: calendar color; 'default' lets the system pick one.
        account: which account to create it in, matched by name (e.g.
            'iCloud', 'Google'). Defaults to the account of the system
            default calendar.
    """
    log.info(
        "create_calendar(name=%r, color=%r, account=%r) called",
        name, color, account,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if not name.strip():
        return "Calendar name cannot be empty."

    if account:
        matches = [
            s for s in store.sources()
            if account.strip().lower() in str(s.title()).lower()
        ]
        if not matches:
            available = ", ".join(str(s.title()) for s in store.sources())
            return f"Account not found: {account}. Available: {available}"
        source = matches[0]
    else:
        default_cal = store.defaultCalendarForNewEvents()
        source = default_cal.source() if default_cal else None
        if source is None:
            return "No account available. Pass an account name."

    cal = EKCalendar.calendarForEntityType_eventStore_(EKEntityTypeEvent, store)
    cal.setTitle_(name.strip())
    cal.setSource_(source)

    if color != "default":
        from AppKit import NSColor
        r, g, b = _COLOR_NAMES[color]
        cal.setColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))

    ok, err = store.saveCalendar_commit_error_(cal, True, None)
    if not ok:
        log.error("Failed to create calendar: %s", err)
        return f"Failed to create calendar: {err}"

    log.info("Calendar created: %s", cal.calendarIdentifier())
    color_desc = f", color {color}" if color != "default" else ""
    return (
        f"Calendar created: {name} in account '{source.title()}'"
        f"{color_desc} (id: {cal.calendarIdentifier()})"
    )


# =========================================================
# TOOL 2: list events within a range of days
# =========================================================
@mcp.tool()
def list_events(days_ahead: int = 7, page: int = 1, page_size: int = 100) -> str:
    """Lists events for the next N days across all calendars.

    Each line includes the event id, needed by delete_event and
    reschedule_event. Results are paginated to avoid oversized responses;
    when the footer says more pages remain, call again with page+1 to
    keep fetching until every page has been retrieved.

    Args:
        days_ahead: how many days ahead to search (default 7).
        page: 1-based page number (default 1).
        page_size: events per page (default 100, max 500).
    """
    log.info(
        "list_events(days_ahead=%d, page=%d, page_size=%d) called",
        days_ahead, page, page_size,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    if page < 1:
        return "page must be >= 1."
    page_size = max(1, min(page_size, 500))

    # Build the range: from now until now + days_ahead.
    start = datetime.now()
    end = start + timedelta(days=days_ahead)
    log.debug("Range: %s -> %s", start, end)

    # predicateForEventsWithStartDate_endDate_calendars_ creates the filter.
    # None for calendars = all calendars.
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(start), _to_nsdate(end), None
    )
    events = store.eventsMatchingPredicate_(pred)
    log.debug("Found %d events", len(events))

    if not events:
        return f"No events in the next {days_ahead} days."

    # Sort by start date.
    events = sorted(events, key=lambda e: e.startDate().timeIntervalSince1970())
    total = len(events)
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return f"Page {page} is out of range ({total_pages} page(s) total for {total} event(s))."

    start_idx = (page - 1) * page_size
    page_events = events[start_idx:start_idx + page_size]
    lines = [_format_event(e) for e in page_events]

    header = (
        f"Events (next {days_ahead} days) {start_idx + 1}-{start_idx + len(lines)} "
        f"of {total} (page {page}/{total_pages}):"
    )
    footer = ""
    if page < total_pages:
        footer = (
            f"\n\nMore results available: call list_events(days_ahead={days_ahead}, "
            f"page={page + 1}, page_size={page_size}) to continue."
        )
    return header + "\n" + "\n".join(lines) + footer


# =========================================================
# TOOL 3: create a new event
# =========================================================
@mcp.tool()
def create_event(
    title: str,
    start: str,
    end: str = "",
    calendar_id: str = "",
    notes: str = "",
    repeat: Literal["none", "daily", "weekly", "monthly", "yearly"] = "none",
    repeat_interval: int = 1,
    repeat_until: str = "",
    alerts: str = "",
    all_day: bool = False,
    location: str = "",
    timezone: str = "",
    url: str = "",
) -> str:
    """Creates an event in Apple Calendar, optionally recurring.

    Args:
        title: event title.
        start: start date/time, format 'YYYY-MM-DD HH:MM'.
        end: end date/time, same format. Defaults to start + 1 hour.
        calendar_id: target calendar id (see list_calendars). Defaults to
            the system default calendar.
        notes: optional notes/description for the event.
        repeat: recurrence frequency; 'none' creates a single event.
        repeat_interval: every N days/weeks/months/years (default 1;
            e.g. repeat='weekly', repeat_interval=2 = every two weeks).
        repeat_until: 'YYYY-MM-DD' last date of the recurrence. Empty =
            repeats forever.
        alerts: comma-separated minutes before the event to alert, e.g.
            '10' or '0,60' (0 = at the event's start time). Empty = no
            alert.
        all_day: whether this is an all-day event. start/end are snapped to
            midnight; for a single day, end can be left empty or equal to
            start. For multiple days, end is exclusive (the day AFTER the
            last day), e.g. start='2026-08-10', end='2026-08-13' covers
            Aug 10-12.
        location: optional free-text location.
        timezone: IANA name (e.g. 'America/Sao_Paulo'). Empty = the
            device's local time zone (default). 'floating' (or 'none')
            makes the event keep the same wall-clock time regardless of
            time zone — matches Calendar.app's "Floating" option, useful
            for events abroad you want anchored to a fixed local time.
        url: optional URL associated with the event.
    """
    log.info(
        "create_event(title=%r, start=%r, end=%r, repeat=%r, alerts=%r, "
        "all_day=%r, location=%r, timezone=%r, url=%r) called",
        title, start, end, repeat, alerts, all_day, location, timezone, url,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    try:
        dt_start = _parse_dt(start)
        dt_end = _parse_dt(end) if end else dt_start + timedelta(hours=1)
    except ValueError as exc:
        return f"Invalid date format ({exc}). Use 'YYYY-MM-DD HH:MM'."
    if dt_end <= dt_start:
        return "End must be after start."

    try:
        tz = _parse_timezone(timezone)
    except ValueError as exc:
        return f"Invalid timezone ({exc}). Use an IANA name or 'floating'."

    if all_day:
        # EventKit all-day events need midnight-aligned dates; endDate is
        # exclusive (the day AFTER the last day), matching iCal's DTEND.
        day_start = dt_start.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = dt_end.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_end <= day_start:
            day_end = day_start + timedelta(days=1)
        dt_start, dt_end = day_start, day_end

    if calendar_id:
        calendar = store.calendarWithIdentifier_(calendar_id.strip())
        if calendar is None:
            return f"Calendar not found: {calendar_id}. Use list_calendars."
    else:
        calendar = store.defaultCalendarForNewEvents()
        if calendar is None:
            return "No default calendar available. Pass a calendar_id."
    if not calendar.allowsContentModifications():
        return f"Calendar '{calendar.title()}' is read-only."

    try:
        alarms = _parse_alerts(alerts)
    except ValueError as exc:
        return f"Invalid alerts ({exc}). Use comma-separated minutes, e.g. '10,60'."

    # tz is a real NSTimeZone only when a specific one was requested;
    # all-day events and the "leave unchanged" sentinel keep local-time
    # interpretation (see _to_nsdate_in_tz).
    dates_tz = None if (all_day or tz is _TIMEZONE_UNCHANGED) else tz

    event = EKEvent.eventWithEventStore_(store)
    event.setTitle_(title)
    event.setStartDate_(_to_nsdate_in_tz(dt_start, dates_tz))
    event.setEndDate_(_to_nsdate_in_tz(dt_end, dates_tz))
    event.setCalendar_(calendar)
    if notes:
        event.setNotes_(notes)
    if alarms:
        event.setAlarms_(alarms)
    if all_day:
        event.setAllDay_(True)
    if location:
        event.setLocation_(location)
    if tz is not _TIMEZONE_UNCHANGED:
        event.setTimeZone_(tz)
    if url:
        event.setURL_(NSURL.URLWithString_(url))

    repeat = repeat.strip().lower()
    recurrence_desc = ""
    if repeat != "none":
        frequencies = {
            "daily": EKRecurrenceFrequencyDaily,
            "weekly": EKRecurrenceFrequencyWeekly,
            "monthly": EKRecurrenceFrequencyMonthly,
            "yearly": EKRecurrenceFrequencyYearly,
        }
        if repeat not in frequencies:
            return "Invalid repeat. Use 'daily', 'weekly', 'monthly' or 'yearly'."
        if repeat_interval < 1:
            return "repeat_interval must be >= 1."

        rec_end = None
        if repeat_until:
            try:
                until = _parse_dt(repeat_until).replace(hour=23, minute=59)
            except ValueError as exc:
                return f"Invalid repeat_until ({exc}). Use 'YYYY-MM-DD'."
            if until < dt_start:
                return "repeat_until must be after start."
            rec_end = EKRecurrenceEnd.recurrenceEndWithEndDate_(
                _to_nsdate(until)
            )

        rule = EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
            frequencies[repeat], repeat_interval, rec_end
        )
        event.setRecurrenceRules_([rule])

        every = f"every {repeat_interval} " if repeat_interval > 1 else ""
        until_desc = f" until {repeat_until}" if repeat_until else ""
        recurrence_desc = f", repeating {every}{repeat}{until_desc}"

    # commit=True writes immediately; pyobjc returns (success, NSError).
    # EKSpanFutureEvents is required when saving a recurring event.
    span = EKSpanThisEvent if repeat == "none" else EKSpanFutureEvents
    ok, err = store.saveEvent_span_commit_error_(event, span, True, None)
    if not ok:
        log.error("Failed to save event: %s", err)
        return f"Failed to create event: {err}"

    log.info("Event created: %s", event.eventIdentifier())
    alerts_desc = ""
    if alarms:
        mins = ", ".join(chunk.strip() for chunk in alerts.split(",") if chunk.strip())
        alerts_desc = f", alerts at -{mins} min"
    extra_desc = ""
    if all_day:
        extra_desc += ", all-day"
    if location:
        extra_desc += f", location '{location}'"
    if tz is not _TIMEZONE_UNCHANGED:
        extra_desc += ", floating time zone" if tz is None else f", time zone {tz.name()}"
    if url:
        extra_desc += f", url '{url}'"
    return (
        f"Event created: {title} on {dt_start:%d/%m/%Y %H:%M}–{dt_end:%H:%M}"
        f" in '{calendar.title()}'{recurrence_desc}{alerts_desc}{extra_desc}"
        f" (id: {event.eventIdentifier()})"
    )


# =========================================================
# TOOL 4: delete an event
# =========================================================
@mcp.tool()
def delete_event(
    event_id: str,
    scope: Literal["this", "this_and_future", "all"] = "this",
    from_date: str = "",
) -> str:
    """Deletes an event from Apple Calendar.

    To delete a recurring series "from date X onward", make ONE call with
    scope='this_and_future' and from_date=X. NEVER call this repeatedly on
    multiple occurrences of the same series — each call truncates the
    series further back and can destroy past events.

    For recurring events on synced calendars (Google/CalDAV), removing a
    single occurrence (scope='this') is often rejected by the sync server
    and the event reappears; deleting from a date onward syncs reliably.

    Args:
        event_id: the event id, as shown by list_events (any occurrence of
            the series works when from_date is given).
        scope: for recurring events — 'this' (only this occurrence),
            'this_and_future' (from an occurrence onward), or 'all'
            (the entire series). Ignored for non-recurring events.
        from_date: 'YYYY-MM-DD'. With scope='this_and_future', anchors the
            deletion at the first occurrence on/after this date, regardless
            of which occurrence id was passed. Recommended.
    """
    log.info(
        "delete_event(event_id=%r, scope=%r, from_date=%r) called",
        event_id, scope, from_date,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    scope = scope.strip().lower()
    if scope not in ("this", "this_and_future", "all"):
        return "Invalid scope. Use 'this', 'this_and_future' or 'all'."

    event = _find_event(event_id)
    if event is None:
        return f"Event not found: {event_id}. Use list_events to get ids."

    title = event.title() or "(no title)"
    is_recurring = bool(event.hasRecurrenceRules())

    if scope == "all" and is_recurring:
        # Removing the FIRST occurrence with the future span kills the
        # whole series. eventWithIdentifier_ conveniently returns it.
        target = store.eventWithIdentifier_(event_id.strip()) or event
        span = EKSpanFutureEvents
    elif scope == "this_and_future" and is_recurring:
        target, span = event, EKSpanFutureEvents
        if from_date:
            try:
                anchor = _parse_dt(from_date)
            except ValueError as exc:
                return f"Invalid from_date ({exc}). Use 'YYYY-MM-DD'."
            target = _first_occurrence_from(event, anchor)
            if target is None:
                return (
                    f"No occurrence of '{title}' found on/after {from_date}."
                )
        # Guard against wiping history: refuse to truncate a series from an
        # occurrence that already happened unless the date was explicit.
        target_start = _to_datetime(target.startDate())
        today = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0)
        if target_start < today and not from_date:
            return (
                f"Refusing: that occurrence is in the past "
                f"({target_start:%d/%m/%Y}); deleting from it onward would "
                "erase past events. Pass from_date='YYYY-MM-DD' to state "
                "the cutoff explicitly."
            )
        event = target  # verification window must match the real target
    else:
        target, span = event, EKSpanThisEvent

    # Capture everything we need before removing — the event object is
    # invalidated by the removal/reset. With from_date the real target may
    # be a different occurrence than the id passed in.
    target_id = str(target.eventIdentifier())
    window_start = _to_datetime(target.startDate()) - timedelta(hours=1)
    window_end = _to_datetime(target.endDate()) + timedelta(hours=1)
    # EKSourceTypeLocal == 0; anything else (CalDAV/Exchange/...) syncs.
    is_synced = event.calendar().source().sourceType() != 0

    # commit=True forces the write to the Calendar database immediately;
    # the commit-less variant proved unreliable for recurring occurrences.
    ok, err = store.removeEvent_span_commit_error_(target, span, True, None)
    if not ok:
        log.error("Failed to delete event: %s", err)
        return f"Failed to delete event: {err}"

    # Don't trust the return value alone: verify the occurrence is gone.
    if _occurrence_still_exists(window_start, window_end, target_id):
        log.error("Delete reported success but occurrence %s persists", target_id)
        if is_recurring and scope == "this":
            return (
                f"Could not delete only this occurrence of '{title}': the "
                "calendar's sync server restored it (common with Google/"
                "CalDAV calendars). Nothing was deleted. Ask the user if "
                "deleting from this date onward is OK, then retry with "
                "scope='this_and_future' — that operation syncs reliably."
            )
        return f"Delete failed to persist: '{title}' still exists."

    log.info("Event deleted: %s (scope=%s)", event_id, scope)
    if is_recurring:
        scope_desc = {
            "this": "this occurrence only",
            "this_and_future": "this occurrence and all future ones",
            "all": "the entire series",
        }[scope]
        if scope == "this_and_future":
            # window_start was captured pre-removal as target start - 1h.
            cutoff = window_start + timedelta(hours=1)
            scope_desc = f"from {cutoff:%d/%m/%Y} onward"
        msg = f"Event deleted: {title} ({scope_desc})"
        if scope == "this" and is_synced:
            msg += (
                ". Note: this calendar is synced (Google/CalDAV); if the "
                "occurrence reappears after sync, retry with "
                "scope='this_and_future' to delete from this date onward."
            )
        return msg
    return f"Event deleted: {title}"


# =========================================================
# TOOL 5: reschedule (move) an event
# =========================================================
@mcp.tool()
def reschedule_event(
    event_id: str,
    new_start: str,
    new_end: str = "",
    alerts: str = "",
    all_day: Literal["", "true", "false"] = "",
    location: str = "",
    timezone: str = "",
    url: str = "",
) -> str:
    """Reschedules an existing event to a new date/time, and/or its alerts,
    all-day flag, location, time zone, or URL.

    Args:
        event_id: the event id, as shown by list_events.
        new_start: new start date/time, format 'YYYY-MM-DD HH:MM'.
        new_end: new end date/time, same format. If omitted, the original
            duration is kept.
        alerts: comma-separated minutes before the event to alert, e.g.
            '10' or '0,60' (0 = at the event's start time). REPLACES the
            event's existing alerts. Leave empty to keep them unchanged;
            pass 'none' to remove all alerts.
        all_day: 'true'/'false' to change the all-day flag. Leave empty to
            keep it unchanged.
        location: new location. Leave empty to keep it unchanged; pass
            'none' to clear it.
        timezone: IANA name (e.g. 'America/Sao_Paulo') to change the time
            zone. Leave empty to keep it unchanged. 'floating' (or 'none')
            makes the event keep the same wall-clock time regardless of
            time zone — matches Calendar.app's "Floating" option.
        url: new URL. Leave empty to keep it unchanged; pass 'none' to
            clear it.
    """
    log.info(
        "reschedule_event(event_id=%r, new_start=%r, new_end=%r, alerts=%r, "
        "all_day=%r, location=%r, timezone=%r, url=%r) called",
        event_id, new_start, new_end, alerts, all_day, location, timezone, url,
    )
    if not _ensure_access():
        return ACCESS_DENIED_MSG

    event = _find_event(event_id)
    if event is None:
        return f"Event not found: {event_id}. Use list_events to get ids."

    try:
        dt_start = _parse_dt(new_start)
        if new_end:
            dt_end = _parse_dt(new_end)
        else:
            # Keep the original duration.
            duration = (
                event.endDate().timeIntervalSince1970()
                - event.startDate().timeIntervalSince1970()
            )
            dt_end = dt_start + timedelta(seconds=duration)
    except ValueError as exc:
        return f"Invalid date format ({exc}). Use 'YYYY-MM-DD HH:MM'."
    if dt_end <= dt_start:
        return "End must be after start."

    alerts = alerts.strip()
    alerts_changed = bool(alerts)
    if alerts_changed:
        try:
            alarms = [] if alerts.lower() == "none" else _parse_alerts(alerts)
        except ValueError as exc:
            return f"Invalid alerts ({exc}). Use comma-separated minutes, e.g. '10,60'."

    location = location.strip()
    location_changed = bool(location)
    new_location = "" if location.lower() == "none" else location

    try:
        tz = _parse_timezone(timezone)
    except ValueError as exc:
        return f"Invalid timezone ({exc}). Use an IANA name or 'floating'."
    tz_changed = tz is not _TIMEZONE_UNCHANGED

    all_day = all_day.strip().lower()
    if all_day not in ("", "true", "false"):
        return "Invalid all_day. Use 'true' or 'false'."
    all_day_changed = bool(all_day)

    url = url.strip()
    url_changed = bool(url)
    new_url = None if url.lower() == "none" else url

    # Interpret new_start/new_end in whichever zone the event will end up
    # with: the newly requested one, or (if timezone was left empty) the
    # one it already had. All-day events keep local-time interpretation.
    is_all_day = (all_day == "true") if all_day_changed else event.isAllDay()
    effective_tz = tz if tz_changed else event.timeZone()
    dates_tz = None if is_all_day else effective_tz

    old_start = _to_datetime(event.startDate())
    event.setStartDate_(_to_nsdate_in_tz(dt_start, dates_tz))
    event.setEndDate_(_to_nsdate_in_tz(dt_end, dates_tz))
    if alerts_changed:
        event.setAlarms_(alarms)
    if location_changed:
        event.setLocation_(new_location)
    if tz_changed:
        event.setTimeZone_(tz)
    if all_day_changed:
        event.setAllDay_(all_day == "true")
    if url_changed:
        event.setURL_(None if new_url is None else NSURL.URLWithString_(new_url))

    old_id = str(event.eventIdentifier())
    ok, err = store.saveEvent_span_commit_error_(event, EKSpanThisEvent, True, None)
    if not ok:
        log.error("Failed to reschedule event: %s", err)
        return f"Failed to reschedule event: {err}"

    # Verify the change persisted: the occurrence must now exist at the
    # new time (rescheduling may detach it and change its id).
    check_start = dt_start - timedelta(hours=1)
    check_end = dt_end + timedelta(hours=1)
    store.refreshSourcesIfNecessary()
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(check_start), _to_nsdate(check_end), None
    )
    moved = any(
        abs(occ.startDate().timeIntervalSince1970() - dt_start.timestamp()) < 60
        and (occ.title() or "") == (event.title() or "")
        for occ in store.eventsMatchingPredicate_(pred)
    )
    if not moved:
        log.error("Reschedule reported success but %s not found at new time", old_id)
        return (
            "Reschedule failed to persist: the event was not found at the "
            "new time. Check the calendar manually."
        )

    title = event.title() or "(no title)"
    log.info("Event rescheduled: %s", event_id)
    alerts_desc = ""
    if alerts_changed:
        if alarms:
            mins = ", ".join(chunk.strip() for chunk in alerts.split(",") if chunk.strip())
            alerts_desc = f", alerts set to -{mins} min"
        else:
            alerts_desc = ", alerts removed"
    extra_desc = ""
    if all_day_changed:
        extra_desc += f", all-day set to {all_day}"
    if location_changed:
        extra_desc += f", location cleared" if not new_location else f", location set to '{new_location}'"
    if tz_changed:
        extra_desc += ", time zone set to floating" if tz is None else f", time zone set to {tz.name()}"
    if url_changed:
        extra_desc += ", url cleared" if new_url is None else f", url set to '{new_url}'"
    return (
        f"Event rescheduled: {title} moved from {old_start:%d/%m/%Y %H:%M}"
        f" to {dt_start:%d/%m/%Y %H:%M}–{dt_end:%H:%M}{alerts_desc}{extra_desc}"
    )


# --- Entry point ---
# Runs the server over stdio, which is how Claude Desktop connects.
if __name__ == "__main__":
    log.info("Entering mcp.run()")
    mcp.run()
