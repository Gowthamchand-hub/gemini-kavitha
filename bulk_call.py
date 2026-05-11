#!/usr/bin/env python3
"""
Bulk outbound calls via Exotel → Kavitha AI agent.

Reads pending candidates from the call-queue Google Sheet tab,
dials them one by one, and updates their status in the sheet.

Usage:
  python3 bulk_call.py                    # call all pending
  python3 bulk_call.py --batch 5          # call 5 at a time (parallel)
  python3 bulk_call.py --dry-run          # preview without dialing
"""

import os
import sys
import json
import time
import threading
import argparse
from datetime import datetime
from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

EXOTEL_API_KEY      = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN    = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_ACCOUNT_SID  = os.getenv("EXOTEL_ACCOUNT_SID", "supernan1")
EXOTEL_PHONE_NUMBER = os.getenv("EXOTEL_PHONE_NUMBER")
SERVER_BASE_URL     = os.getenv("SERVER_WS_BASE_URL", "").replace("wss://", "https://").replace("ws://", "http://")

SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "112yETKsk2aaM6knc5Bk8ZUFd-IHK0bD_puMOKrDz7XQ")
QUEUE_SHEET_NAME = "call-queue"
EXOTEL_API_URL   = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect"

MAX_ATTEMPTS  = 3
CALL_GAP_SECS = 5   # seconds between each outbound dial

# Sheet column indices (0-based)
COL_PHONE   = 0
COL_NAME    = 1
COL_STATUS  = 2
COL_ATTEMPT = 3
COL_LAST    = 4
COL_RETRY   = 5
COL_SID     = 6


def get_queue_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        print("[ERROR] GOOGLE_CREDENTIALS_JSON not set")
        sys.exit(1)
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(QUEUE_SHEET_NAME)


def get_pending_candidates(sheet) -> list[dict]:
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return []
    now = datetime.now()
    pending = []
    for i, row in enumerate(rows[1:], start=2):  # row index in sheet (1-based, header is row 1)
        if len(row) < 3:
            continue
        status  = row[COL_STATUS].strip()
        attempt = int(row[COL_ATTEMPT]) if len(row) > COL_ATTEMPT and row[COL_ATTEMPT].isdigit() else 0
        retry_time_str = row[COL_RETRY].strip() if len(row) > COL_RETRY else ""

        # Skip if done or currently calling or max attempts reached
        if status in ("Calling", "Not Reachable - Final") or attempt >= MAX_ATTEMPTS:
            continue
        # Only include Pending or retries where retry time has passed
        if status == "Pending":
            pending.append({"row": i, "phone": row[COL_PHONE], "name": row[COL_NAME], "attempt": attempt})
        elif "Not Reachable - Attempt" in status and retry_time_str:
            try:
                retry_time = datetime.strptime(retry_time_str, "%Y-%m-%d %H:%M:%S")
                if now >= retry_time:
                    pending.append({"row": i, "phone": row[COL_PHONE], "name": row[COL_NAME], "attempt": attempt})
            except ValueError:
                pass
    return pending


def dial(phone: str, name: str) -> str | None:
    answer_url = f"{SERVER_BASE_URL.rstrip('/')}/answer?outbound=1"
    status_url = f"{SERVER_BASE_URL.rstrip('/')}/status"

    payload = {
        "From":                      EXOTEL_PHONE_NUMBER,
        "To":                        phone,
        "CallerId":                  EXOTEL_PHONE_NUMBER,
        "Url":                       answer_url,
        "StatusCallback":            status_url,
        "StatusCallbackEvents[0]":   "terminal",
        "Record":                    "false",
    }

    try:
        resp = requests.post(
            EXOTEL_API_URL,
            data=payload,
            auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
            timeout=30,
        )
        if not resp.ok:
            print(f"  [FAIL] {name} ({phone}) — {resp.status_code}: {resp.text[:200]}")
            return None
        call_data = resp.json().get("Call", {})
        sid = call_data.get("Sid", "")
        print(f"  [DIALED] {name} ({phone}) — SID: {sid}")
        return sid
    except Exception as e:
        print(f"  [ERROR] {name} ({phone}) — {e}")
        return None


def update_row_calling(sheet, row_index: int, sid: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.update(f"C{row_index}:G{row_index}", [["Calling", "", now_str, "", sid]])


def main():
    parser = argparse.ArgumentParser(description="Dial pending candidates from call-queue sheet")
    parser.add_argument("--batch",   type=int, default=1, help="Parallel calls at a time (default: 1)")
    parser.add_argument("--dry-run", action="store_true",  help="Preview candidates without dialing")
    args = parser.parse_args()

    if not EXOTEL_API_KEY or not EXOTEL_API_TOKEN:
        print("[ERROR] EXOTEL_API_KEY / EXOTEL_API_TOKEN not set in .env")
        sys.exit(1)
    if not SERVER_BASE_URL:
        print("[ERROR] SERVER_WS_BASE_URL not set in .env")
        sys.exit(1)

    print("Reading call-queue sheet...")
    sheet     = get_queue_sheet()
    pending   = get_pending_candidates(sheet)
    total     = len(pending)

    if total == 0:
        print("No pending candidates to call.")
        return

    print(f"Found {total} candidates to call\n" + "-" * 40)

    if args.dry_run:
        for c in pending:
            print(f"  [DRY RUN] {c['name']} ({c['phone']}) — attempt {c['attempt'] + 1}")
        return

    lock = threading.Lock()

    def call_one(candidate, index):
        name    = candidate["name"]
        phone   = candidate["phone"]
        row_idx = candidate["row"]
        print(f"[{index}/{total}] Calling {name} ({phone})...")
        sid = dial(phone, name)
        with lock:
            update_row_calling(sheet, row_idx, sid or "")

    # Dial in batches
    batch = args.batch
    for start in range(0, total, batch):
        chunk   = pending[start:start + batch]
        threads = []
        for i, candidate in enumerate(chunk, start + 1):
            t = threading.Thread(target=call_one, args=(candidate, i))
            t.start()
            threads.append(t)
            time.sleep(CALL_GAP_SECS)
        for t in threads:
            t.join()

    print("-" * 40)
    print(f"Done. Dialed {total} candidates.")
    print("Track results in the call-queue sheet.")


if __name__ == "__main__":
    main()
