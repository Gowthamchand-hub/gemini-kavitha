#!/usr/bin/env python3
"""
Load candidates into the call-queue Google Sheet tab.

Accepts a CSV file (name, phone) or a plain text file (one number per line).
Skips duplicates based on phone number.

Usage:
  python3 load_candidates.py --csv candidates.csv
  python3 load_candidates.py --csv numbers.txt   # plain list, one number per line
  python3 load_candidates.py --csv candidates.csv --phone-col 0 --name-col 1
"""

import os
import csv
import sys
import json
import argparse
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "112yETKsk2aaM6knc5Bk8ZUFd-IHK0bD_puMOKrDz7XQ")
QUEUE_SHEET_NAME = "call-queue"
QUEUE_HEADERS    = ["Phone", "Name", "Status", "Attempt Count", "Last Called", "Next Retry Time", "Call SID"]


def get_queue_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        print("[ERROR] GOOGLE_CREDENTIALS_JSON not set in .env")
        sys.exit(1)
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        sheet = spreadsheet.worksheet(QUEUE_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=QUEUE_SHEET_NAME, rows=2000, cols=7)
        sheet.insert_row(QUEUE_HEADERS, 1)
        print(f"Created new sheet tab: {QUEUE_SHEET_NAME}")
    return sheet


def normalize_phone(phone: str) -> str:
    phone = str(phone).strip().replace(" ", "").replace("-", "")
    if phone.startswith("+91"):
        return "0" + phone[3:]
    elif phone.startswith("91") and len(phone) == 12:
        return "0" + phone[2:]
    return phone


def load_from_file(filepath: str, name_col: int, phone_col: int) -> list[tuple[str, str]]:
    candidates = []
    with open(filepath, newline="", encoding="utf-8") as f:
        sample = f.read(500)
        f.seek(0)
        has_comma = "," in sample

        if has_comma:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if not row:
                    continue
                # Skip header row if it contains no digits in phone column
                if i == 0 and phone_col < len(row) and not any(c.isdigit() for c in row[phone_col]):
                    continue
                if len(row) > max(name_col, phone_col):
                    name  = row[name_col].strip() if name_col < len(row) else f"Candidate {i+1}"
                    phone = normalize_phone(row[phone_col])
                    if phone:
                        candidates.append((phone, name))
                elif len(row) == 1:
                    candidates.append((normalize_phone(row[0]), f"Candidate {i+1}"))
        else:
            for i, line in enumerate(f):
                line = line.strip()
                if line and any(c.isdigit() for c in line):
                    candidates.append((normalize_phone(line), f"Candidate {i+1}"))

    return candidates


def main():
    parser = argparse.ArgumentParser(description="Load candidates into call-queue Google Sheet")
    parser.add_argument("--csv",       required=True, help="CSV or plain text file with candidates")
    parser.add_argument("--name-col",  type=int, default=0, help="Column index for name (default 0)")
    parser.add_argument("--phone-col", type=int, default=1, help="Column index for phone (default 1)")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"[ERROR] File not found: {args.csv}")
        sys.exit(1)

    candidates = load_from_file(args.csv, args.name_col, args.phone_col)
    print(f"Loaded {len(candidates)} candidates from {args.csv}")

    sheet = get_queue_sheet()

    # Get existing phones to avoid duplicates
    existing_rows = sheet.get_all_values()
    existing_phones = set(row[0] for row in existing_rows[1:] if row and row[0])

    rows_to_add = []
    added   = 0
    skipped = 0
    for phone, name in candidates:
        if phone in existing_phones:
            skipped += 1
            continue
        rows_to_add.append([phone, name, "Pending", 0, "", "", ""])
        existing_phones.add(phone)
        added += 1

    if rows_to_add:
        sheet.append_rows(rows_to_add, value_input_option="RAW")

    print(f"Added: {added} | Skipped duplicates: {skipped}")
    print(f"Total in queue now: {len(existing_phones)}")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == "__main__":
    main()
