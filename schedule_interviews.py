#!/usr/bin/env python3
"""
Interview scheduler — reads shortlisted candidates from Google Sheet,
finds the next available slot on the interview calendar, books it,
and sends a Google Calendar invite to the interviewer and candidate.

Usage:
  python3 schedule_interviews.py            # schedule all unscheduled shortlisted candidates
  python3 schedule_interviews.py --dry-run  # preview without booking
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SHEET_ID          = os.getenv("GOOGLE_SHEET_ID", "112yETKsk2aaM6knc5Bk8ZUFd-IHK0bD_puMOKrDz7XQ")
RESULTS_SHEET     = "gemini-kavitha-sheets"   # where Kavitha saves candidate results
INTERVIEWER_EMAIL = os.getenv("INTERVIEWER_EMAIL", "interviewer@supernan.app")  # replace with real email
CALENDAR_ID       = os.getenv("INTERVIEW_CALENDAR_ID", "primary")               # or a shared calendar ID
OFFICE_LOCATION   = "Supernan Childcare Solutions, Amruthahalli, Bangalore"

# Slot config
SLOT_DURATION_MINS = 60
WORK_START_HOUR    = 10   # 10am
WORK_END_HOUR      = 18   # 6pm
LUNCH_HOUR         = 13   # skip 1pm slot
MAX_PER_DAY        = 6
DAYS_AHEAD         = 7    # look up to 7 days ahead for a free slot

# Sheet columns in gemini-kavitha-sheets (0-based)
# Timestamp, Name, Area, Experience, Languages, Age Group, Timing, Salary, Smartphone, Reference Name, Reference Number, Status
COL_TIMESTAMP = 0
COL_NAME      = 1
COL_STATUS    = 11
COL_SCHEDULED = 12   # we'll add this column: "Interview Slot"


# ---------------------------------------------------------------------------
# Google auth (shared service account — same creds as Sheets)
# ---------------------------------------------------------------------------

def get_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        print("[ERROR] GOOGLE_CREDENTIALS_JSON not set")
        sys.exit(1)
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar",
    ]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)


def get_results_sheet(creds):
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(RESULTS_SHEET)


def get_calendar_service(creds):
    return build("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Slot logic
# ---------------------------------------------------------------------------

def all_slots_for_day(d: date) -> list[datetime]:
    """All possible interview slots on a given date."""
    slots = []
    hour = WORK_START_HOUR
    while hour < WORK_END_HOUR:
        if hour != LUNCH_HOUR:
            slots.append(datetime(d.year, d.month, d.day, hour, 0))
        hour += 1
    return slots


def get_booked_slots(calendar, start_date: date, end_date: date) -> set[datetime]:
    """Fetch all booked slots on the interview calendar between two dates."""
    time_min = datetime(start_date.year, start_date.month, start_date.day).isoformat() + "Z"
    time_max = datetime(end_date.year, end_date.month, end_date.day, 23, 59).isoformat() + "Z"
    try:
        events = calendar.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as e:
        print(f"[WARN] Could not fetch calendar events: {e}")
        return set()

    booked = set()
    daily_counts = {}
    for event in events.get("items", []):
        start = event.get("start", {}).get("dateTime", "")
        if start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                booked.add(dt.replace(second=0, microsecond=0))
                day_key = dt.date()
                daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
            except Exception:
                pass
    return booked, daily_counts


def find_next_slot(booked_slots: set, daily_counts: dict) -> datetime | None:
    """Find the next available slot starting from tomorrow."""
    today = date.today()
    for day_offset in range(1, DAYS_AHEAD + 1):
        d = today + timedelta(days=day_offset)
        if d.weekday() == 6:  # skip Sunday
            continue
        if daily_counts.get(d, 0) >= MAX_PER_DAY:
            continue
        for slot in all_slots_for_day(d):
            if slot not in booked_slots:
                return slot
    return None


# ---------------------------------------------------------------------------
# Calendar event
# ---------------------------------------------------------------------------

def book_slot(calendar, slot: datetime, candidate_name: str, candidate_phone: str, dry_run: bool) -> bool:
    start_iso = slot.strftime("%Y-%m-%dT%H:%M:%S")
    end_iso   = (slot + timedelta(minutes=SLOT_DURATION_MINS)).strftime("%Y-%m-%dT%H:%M:%S")

    event = {
        "summary": f"Supernan Interview — {candidate_name}",
        "location": OFFICE_LOCATION,
        "description": (
            f"Candidate: {candidate_name}\n"
            f"Phone: {candidate_phone}\n"
            f"Documents: Aadhaar card, Bank passbook, 1 passport photo\n"
            f"Duration: 45 minutes + practical assessment"
        ),
        "start": {"dateTime": start_iso, "timeZone": "Asia/Kolkata"},
        "end":   {"dateTime": end_iso,   "timeZone": "Asia/Kolkata"},
        "attendees": [
            {"email": INTERVIEWER_EMAIL},
        ],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 60},
                {"method": "popup",  "minutes": 15},
            ],
        },
    }

    if dry_run:
        print(f"  [DRY RUN] Would book: {candidate_name} on {slot.strftime('%A %d %b, %I:%M %p')}")
        return True

    try:
        calendar.events().insert(calendarId=CALENDAR_ID, body=event, sendUpdates="all").execute()
        print(f"  [BOOKED] {candidate_name} — {slot.strftime('%A %d %b, %I:%M %p')}")
        return True
    except Exception as e:
        print(f"  [ERROR] Could not book slot for {candidate_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def get_unscheduled_shortlisted(sheet) -> list[dict]:
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return []

    # Ensure header has Interview Slot column
    header = rows[0]
    if len(header) <= COL_SCHEDULED:
        sheet.update_cell(1, COL_SCHEDULED + 1, "Interview Slot")

    candidates = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= COL_STATUS:
            continue
        status = row[COL_STATUS].strip()
        if not status.startswith("Shortlisted"):
            continue
        scheduled = row[COL_SCHEDULED].strip() if len(row) > COL_SCHEDULED else ""
        if scheduled:
            continue  # already scheduled
        candidates.append({
            "row":   i,
            "name":  row[COL_NAME].strip(),
            "phone": row[10].strip() if len(row) > 10 else "",  # Reference Number col has phone
        })
    return candidates


def mark_scheduled(sheet, row_index: int, slot: datetime):
    slot_str = slot.strftime("%d %b %Y, %I:%M %p")
    sheet.update_cell(row_index, COL_SCHEDULED + 1, slot_str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Schedule interviews for shortlisted candidates")
    parser.add_argument("--dry-run", action="store_true", help="Preview without booking")
    args = parser.parse_args()

    creds    = get_creds()
    sheet    = get_results_sheet(creds)
    calendar = get_calendar_service(creds)

    candidates = get_unscheduled_shortlisted(sheet)
    if not candidates:
        print("No unscheduled shortlisted candidates found.")
        return

    print(f"Found {len(candidates)} candidate(s) to schedule.\n" + "-" * 40)

    # Fetch booked slots once for the full window
    today     = date.today()
    end_date  = today + timedelta(days=DAYS_AHEAD)
    result    = get_booked_slots(calendar, today, end_date)
    booked    = result[0] if isinstance(result, tuple) else result
    daily_counts = result[1] if isinstance(result, tuple) else {}

    scheduled = 0
    for candidate in candidates:
        slot = find_next_slot(booked, daily_counts)
        if not slot:
            print(f"  [SKIP] No available slots in next {DAYS_AHEAD} days for {candidate['name']}")
            continue

        success = book_slot(calendar, slot, candidate["name"], candidate["phone"], args.dry_run)
        if success:
            if not args.dry_run:
                mark_scheduled(sheet, candidate["row"], slot)
            # Mark slot as booked so next candidate gets a different slot
            booked.add(slot)
            d = slot.date()
            daily_counts[d] = daily_counts.get(d, 0) + 1
            scheduled += 1

    print("-" * 40)
    print(f"Done. Scheduled {scheduled}/{len(candidates)} candidates.")
    if args.dry_run:
        print("(Dry run — nothing was actually booked)")


if __name__ == "__main__":
    main()
