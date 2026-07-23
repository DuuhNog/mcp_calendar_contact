#!/usr/bin/env python3
# test_recurring_delete.py
# Acceptance test for the recurring-occurrence delete bug:
#   create a recurring test series -> delete ONE occurrence ->
#   re-list and assert it's really gone -> clean up the series.
# Run with: ./venv/bin/python test_recurring_delete.py
# (The host app running this needs Calendar access.)

import sys
from datetime import datetime, timedelta

from EventKit import (
    EKEvent,
    EKRecurrenceRule,
    EKRecurrenceFrequencyDaily,
    EKSpanFutureEvents,
)

import bruno_calendar_mcp as m

TEST_TITLE = "MCP_TEST_RECURRING (safe to delete)"


def find_test_occurrences():
    start = datetime.now() - timedelta(days=1)
    end = datetime.now() + timedelta(days=10)
    pred = m.store.predicateForEventsWithStartDate_endDate_calendars_(
        m._to_nsdate(start), m._to_nsdate(end), None
    )
    return [
        e for e in m.store.eventsMatchingPredicate_(pred)
        if (e.title() or "") == TEST_TITLE
    ]


def cleanup():
    occs = find_test_occurrences()
    if occs:
        first = min(occs, key=lambda e: e.startDate().timeIntervalSince1970())
        m.store.removeEvent_span_commit_error_(first, EKSpanFutureEvents, True, None)


def main():
    if not m._ensure_access():
        print("SKIP: calendar access denied for this process.")
        print("Run this script from an app that has Calendar permission.")
        sys.exit(2)

    cleanup()  # remove leftovers from previous runs

    # 1. Create a daily recurring series starting tomorrow, 5 occurrences.
    start = (datetime.now() + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    event = EKEvent.eventWithEventStore_(m.store)
    event.setTitle_(TEST_TITLE)
    event.setStartDate_(m._to_nsdate(start))
    event.setEndDate_(m._to_nsdate(start + timedelta(minutes=30)))
    event.setCalendar_(m.store.defaultCalendarForNewEvents())
    rule = EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
        EKRecurrenceFrequencyDaily, 1, None
    )
    event.setRecurrenceRules_([rule])
    ok, err = m.store.saveEvent_span_commit_error_(
        event, EKSpanFutureEvents, True, None
    )
    assert ok, f"could not create test series: {err}"

    occs = find_test_occurrences()
    assert len(occs) >= 3, f"expected >=3 occurrences, got {len(occs)}"
    # Pick a middle occurrence (not the first) — the buggy case.
    target = sorted(occs, key=lambda e: e.startDate().timeIntervalSince1970())[2]
    target_id = str(target.eventIdentifier())
    print(f"Created series; deleting occurrence id: {target_id}")

    # 2. Delete via the actual MCP tool.
    result = m.delete_event(target_id, scope="this")
    print(f"delete_event returned: {result}")

    # 3. Re-list and assert the occurrence is gone.
    remaining_ids = [str(e.eventIdentifier()) for e in find_test_occurrences()]
    gone = target_id not in remaining_ids
    print(f"Occurrence still listed? {not gone}")

    cleanup()

    if "deleted" in result.lower() and gone:
        print("PASS: occurrence really deleted and success message is truthful.")
        sys.exit(0)
    if "failed" in result.lower() and not gone:
        print("HONEST FAILURE: deletion did not persist, but the tool said so.")
        sys.exit(1)
    print("FAIL: false success — tool reported deleted but occurrence persists.")
    sys.exit(1)


if __name__ == "__main__":
    main()
