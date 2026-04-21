#!/usr/bin/env python3
"""
Bulk outbound calls via Exotel -> Kavitha AI agent.

Usage:
  python3 bulk_call.py candidates.csv              # call all simultaneously
  python3 bulk_call.py candidates.csv --batch 5    # call 5 at a time

CSV format:
  name,phone
  Pooja,+919345473240
  Ravi,+918297084848
"""

import os
import sys
import csv
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

EXOTEL_API_KEY      = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN    = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_ACCOUNT_SID  = os.getenv("EXOTEL_ACCOUNT_SID", "supernan1")
EXOTEL_PHONE_NUMBER = os.getenv("EXOTEL_PHONE_NUMBER")
FLOW_URL            = "http://my.exotel.com/supernan1/exoml/start_voice/1218626"
STATUS_URL          = "https://web-production-966f.up.railway.app/status"
EXOTEL_API_URL      = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect"

POST_CALL_BUFFER = 30  # seconds to wait after call ends before next call


def normalize_number(phone):
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+91"):
        return "0" + phone[3:]
    elif phone.startswith("91") and len(phone) == 12:
        return "0" + phone[2:]
    return phone


def make_call(name, phone):
    to_number = normalize_number(phone)
    from_number = EXOTEL_PHONE_NUMBER.replace("-", "")

    payload = {
        "From":           to_number,
        "To":             from_number,
        "CallerId":       from_number,
        "Url":            FLOW_URL,
        "StatusCallback": STATUS_URL,
        "Record":         "false",
    }

    response = requests.post(
        EXOTEL_API_URL,
        data=payload,
        auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
        timeout=30,
    )

    if not response.ok:
        print(f"  [FAIL] {name} ({to_number}) — {response.status_code}: {response.text[:200]}")
        return None

    # Extract call SID
    import re
    sid_match = re.search(r"<Sid>(.*?)</Sid>", response.text)
    sid = sid_match.group(1) if sid_match else None
    print(f"  [OK] Called {name} ({to_number}) — SID: {sid}")
    return sid


def wait_for_call_to_end(sid, poll_interval=10, max_wait=600):
    """Poll Exotel until call is completed or max_wait reached."""
    if not sid:
        return
    url = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/{sid}"
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            resp = requests.get(url, auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN), timeout=10)
            import re
            status_match = re.search(r"<Status>(.*?)</Status>", resp.text)
            status = status_match.group(1) if status_match else "unknown"
            print(f"  Call status: {status} ({waited}s elapsed)")
            if status in ("completed", "failed", "busy", "no-answer", "canceled"):
                return
        except Exception as e:
            print(f"  Poll error: {e}")


def load_candidates(filepath):
    candidates = []
    with open(filepath, newline="", encoding="utf-8") as f:
        # Detect if it's just numbers or has names
        sample = f.read(200)
        f.seek(0)
        has_comma = "," in sample

        if has_comma:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if not row:
                    continue
                if i == 0 and not any(c.isdigit() for c in row[0]):
                    continue  # skip header
                if len(row) >= 2:
                    candidates.append((row[0].strip(), row[1].strip()))
                elif len(row) == 1:
                    candidates.append((f"Candidate {i+1}", row[0].strip()))
        else:
            for i, line in enumerate(f):
                line = line.strip()
                if line:
                    candidates.append((f"Candidate {i+1}", line))

    return candidates


def call_and_wait(name, phone, index, total):
    print(f"  [{index}/{total}] Calling {name} — {phone}")
    sid = make_call(name, phone)
    if sid:
        wait_for_call_to_end(sid)
        print(f"  [{index}/{total}] {name} — call ended.")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", help="CSV file with name,phone columns")
    parser.add_argument("--batch", type=int, default=None, help="Max parallel calls at once (default: all at once)")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"[ERROR] File not found: {args.csv}")
        sys.exit(1)

    candidates = load_candidates(args.csv)
    total = len(candidates)
    batch = args.batch or total

    print(f"\nLoaded {total} candidates — calling {batch} at a time\n" + "-" * 40)

    for start in range(0, total, batch):
        chunk = candidates[start:start + batch]
        threads = []
        for i, (name, phone) in enumerate(chunk, start + 1):
            t = threading.Thread(target=call_and_wait, args=(name, phone, i, total))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        if start + batch < total:
            print(f"  Batch done. Waiting {POST_CALL_BUFFER}s before next batch...")
            time.sleep(POST_CALL_BUFFER)

    print("\n" + "-" * 40)
    print(f"Done. Called {total} candidates.")


if __name__ == "__main__":
    main()
