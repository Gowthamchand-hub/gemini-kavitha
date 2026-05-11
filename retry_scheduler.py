#!/usr/bin/env python3
"""
Retry scheduler — runs continuously, checks call-queue every 30 minutes,
redials candidates where retry time has passed.

Runs as a standalone process alongside gemini_server.py on Railway.

Usage:
  python3 retry_scheduler.py
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

EXOTEL_API_KEY      = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN    = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_ACCOUNT_SID  = os.getenv("EXOTEL_ACCOUNT_SID", "supernan1")
EXOTEL_PHONE_NUMBER = os.getenv("EXOTEL_PHONE_NUMBER")
SERVER_BASE_URL     = os.getenv("SERVER_WS_BASE_URL", "").replace("wss://", "https://").replace("ws://", "http://")

SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "112yETKsk2aaM6knc5Bk8ZUFd-IHK0bD_puMOKrDz7XQ")
QUEUE_SHEET_NAME = "call-queue"
EXOTEL_API_URL   = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect"

CHECK_INTERVAL_SECS = 30 * 60   # check every 30 minutes
MAX_ATTEMPTS        = 3

COL_PHONE   = 0
COL_NAME    = 1
COL_STATUS  = 2
COL_ATTEMPT = 3
COL_LAST    = 4
COL_RETRY   = 5
COL_SID     = 6


def get_queue_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(QUEUE_SHEET_NAME)


def get_due_retries(sheet) -> list[dict]:
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return []
    now = datetime.now()
    due = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 3:
            continue
        status  = row[COL_STATUS].strip()
        attempt = int(row[COL_ATTEMPT]) if len(row) > COL_ATTEMPT and row[COL_ATTEMPT].isdigit() else 0
        retry_str = row[COL_RETRY].strip() if len(row) > COL_RETRY else ""

        if "Not Reachable - Attempt" not in status:
            continue
        if attempt >= MAX_ATTEMPTS:
            continue
        if not retry_str:
            continue
        try:
            retry_time = datetime.strptime(retry_str, "%Y-%m-%d %H:%M:%S")
            if now >= retry_time:
                due.append({"row": i, "phone": row[COL_PHONE], "name": row[COL_NAME], "attempt": attempt})
        except ValueError:
            continue
    return due


def dial(phone: str, name: str) -> str | None:
    answer_url = f"{SERVER_BASE_URL.rstrip('/')}/answer?outbound=1"
    status_url = f"{SERVER_BASE_URL.rstrip('/')}/status"
    payload = {
        "From":                    EXOTEL_PHONE_NUMBER,
        "To":                      phone,
        "CallerId":                EXOTEL_PHONE_NUMBER,
        "Url":                     answer_url,
        "StatusCallback":          status_url,
        "StatusCallbackEvents[0]": "terminal",
        "Record":                  "false",
    }
    try:
        resp = requests.post(
            EXOTEL_API_URL,
            data=payload,
            auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
            timeout=30,
        )
        if not resp.ok:
            log.error(f"Exotel error {resp.status_code} for {phone}: {resp.text[:200]}")
            return None
        sid = resp.json().get("Call", {}).get("Sid", "")
        log.info(f"Retry dialed {name} ({phone}) — SID: {sid}")
        return sid
    except Exception as e:
        log.error(f"Dial error for {phone}: {e}")
        return None


def run_retries():
    try:
        sheet = get_queue_sheet()
        due   = get_due_retries(sheet)

        if not due:
            log.info("No retries due.")
            return

        log.info(f"Found {len(due)} retry(s) due.")
        for candidate in due:
            phone   = candidate["phone"]
            name    = candidate["name"]
            row_idx = candidate["row"]
            sid = dial(phone, name)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.update(f"C{row_idx}:G{row_idx}", [["Calling", "", now_str, "", sid or ""]])
            time.sleep(3)  # small gap between retries

    except Exception as e:
        log.error(f"Retry cycle error: {e}")


def main():
    if not EXOTEL_API_KEY or not SERVER_BASE_URL:
        log.error("EXOTEL_API_KEY or SERVER_WS_BASE_URL not set — exiting.")
        sys.exit(1)

    log.info(f"Retry scheduler started — checking every {CHECK_INTERVAL_SECS // 60} minutes.")
    while True:
        log.info("Running retry check...")
        run_retries()
        log.info(f"Next check in {CHECK_INTERVAL_SECS // 60} minutes.")
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    main()
