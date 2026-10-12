"""
Learning Pulse Live — daily sync from Metabase questions into the Google Sheet's dump tabs.

Env vars (set as GitHub Actions secrets):
  METABASE_URL                  e.g. https://metabase.newtonschool.co
  METABASE_API_KEY              (preferred)  — or METABASE_USERNAME + METABASE_PASSWORD
  GOOGLE_SERVICE_ACCOUNT_JSON   full JSON key of a service account that has Editor access to the sheet

Run locally:  python sync_learning_pulse.py            (writes to the sheet)
              python sync_learning_pulse.py --dry-run  (fetches + reports, writes nothing)
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

import gspread
import requests
import yaml
from google.oauth2.service_account import Credentials

# Rows the sheet's *_Clean tabs can read for each dump (from the workbook's How to Use tab).
CAPACITY = {
    "Raw_Dump": 5000,
    "Lecture_Dump": 16000,
    "Lecture_Feedback_Dump": 5000,
    "TA_Dump": 3000,
    "TA_Looker_Dump": 3000,
    "Mentor_Sessions_Dump": 3000,
    "Mentor_CSAT_Dump": 6000,
}
WRITE_CHUNK = 5000  # rows per Sheets API update call

ISO_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


# ---------------------------------------------------------------- Metabase
class Metabase:
    def __init__(self):
        self.base = os.environ["METABASE_URL"].rstrip("/")
        self.s = requests.Session()
        api_key = os.environ.get("METABASE_API_KEY")
        if api_key:
            self.s.headers["x-api-key"] = api_key
        else:
            r = self.s.post(f"{self.base}/api/session", json={
                "username": os.environ["METABASE_USERNAME"],
                "password": os.environ["METABASE_PASSWORD"],
            }, timeout=60)
            r.raise_for_status()
            self.s.headers["X-Metabase-Session"] = r.json()["id"]

    def card_rows(self, card_id):
        """All rows of a saved question as (columns, rows). Uses the export endpoint (no 2,000-row cap)."""
        url = f"{self.base}/api/card/{card_id}/query/json"
        r = self.s.post(url, data={"parameters": "[]", "format_rows": "false"}, timeout=900)
        if r.status_code >= 400:  # newer Metabase versions also accept a JSON body
            r = self.s.post(url, json={"parameters": [], "format_rows": False}, timeout=900)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and ("error" in data or "message" in data):
            raise RuntimeError(f"Metabase card {card_id} failed: {data.get('error') or data.get('message')}")
        if not data:
            return [], []
        cols = list(data[0].keys())
        return cols, [[rec.get(c) for c in cols] for rec in data]


# ---------------------------------------------------------------- value cleaning
def clean(v):
    """Make a Metabase value safe + parseable for USER_ENTERED writes."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    m = ISO_DT.match(s)
    if m:  # 2026-09-21T00:00:00+05:30 -> 2026-09-21  /  2026-09-21 14:05:00
        return m.group(1) if m.group(2) == "00:00:00" else f"{m.group(1)} {m.group(2)}"
    if s[:1] in ("=", "+"):  # don't let student comments turn into formulas
        return "'" + s
    return s


def key_part(v):
    """Normalise a key value from either Metabase or the sheet so they compare equal."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    m = ISO_DT.match(s)
    if m:
        return m.group(1)
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else s
    except ValueError:
        return s


def serial_to_date(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 20000 < v < 80000:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(v))).isoformat()
    return v


# ---------------------------------------------------------------- sheet writes
def ensure_size(ws, rows, cols):
    if ws.row_count < rows or ws.col_count < cols:
        ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols))


def write_block(ws, start_row, values):
    for i in range(0, len(values), WRITE_CHUNK):
        chunk = values[i:i + WRITE_CHUNK]
        r0 = start_row + i
        rng = f"A{r0}:{gspread.utils.rowcol_to_a1(r0 + len(chunk) - 1, len(chunk[0]))}"
        ws.update(values=chunk, range_name=rng, value_input_option="USER_ENTERED")


def check_headers(tab, sheet_header, mb_cols):
    missing = [h for h in sheet_header if h and h not in mb_cols]
    if missing:
        print(f"  ! {tab}: sheet expects columns not in the Metabase result: {missing}")


def do_replace(ws, tab, cols, rows, dry):
    sheet_header = [h for h in ws.row_values(1)]
    check_headers(tab, sheet_header, cols)
    if not rows:
        print(f"  ! {tab}: query returned 0 rows — leaving the tab untouched")
        return
    values = [cols] + [[clean(v) for v in r] for r in rows]
    print(f"  {tab}: replace with {len(rows)} rows x {len(cols)} cols")
    if dry:
        return
    ensure_size(ws, len(values) + 1, len(cols))
    ws.batch_clear([f"A1:{gspread.utils.rowcol_to_a1(ws.row_count, ws.col_count)}"])
    write_block(ws, 1, values)


def do_append(ws, tab, cols, rows, key_cols, dry):
    header = ws.row_values(1)
    check_headers(tab, header, cols)

    # Map each sheet column to a Metabase column. Repeated headers (Raw_Dump has
    # placement_process twice) take name, then name_2 / name_3 ... if Metabase has them.
    seen, src = {}, []
    for h in header:
        n = seen.get(h, 0) + 1
        seen[h] = n
        cand = h if n == 1 else f"{h}_{n}"
        src.append(cols.index(cand) if cand in cols else None)

    for k in key_cols:
        if k not in header or k not in cols:
            sys.exit(f"{tab}: key column '{k}' must be in both the sheet header and the Metabase result")

    existing = ws.get(
        f"A2:{gspread.utils.rowcol_to_a1(ws.row_count, len(header))}",
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="SERIAL_NUMBER",
    )
    kidx_sheet = [header.index(k) for k in key_cols]
    have = set()
    last_filled = 1
    for i, r in enumerate(existing, start=2):
        if any(c not in ("", None) for c in r):
            last_filled = i
            have.add(tuple(key_part(serial_to_date(r[j]) if j < len(r) else "") for j in kidx_sheet))

    kidx_mb = [cols.index(k) for k in key_cols]
    new_rows = []
    for r in rows:
        key = tuple(key_part(r[j]) for j in kidx_mb)
        if key in have:
            continue
        have.add(key)
        new_rows.append([clean(r[j]) if j is not None else "" for j in src])

    print(f"  {tab}: {len(rows)} rows in Metabase, {len(new_rows)} new to append after row {last_filled}")
    if not new_rows or dry:
        return
    ensure_size(ws, last_filled + len(new_rows) + 1, len(header))
    write_block(ws, last_filled + 1, new_rows)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    tabs = {t: c for t, c in cfg["tabs"].items() if c and c.get("card_id")}
    if not tabs:
        sys.exit("No card_id filled in config.yaml — nothing to sync.")

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(cfg["spreadsheet_id"])
    mb = Metabase()

    # Fetch everything first, so a failed query never leaves the sheet half-updated.
    fetched = {}
    for tab, c in tabs.items():
        print(f"Fetching card {c['card_id']} for {tab} ...")
        cols, rows = mb.card_rows(c["card_id"])
        ren = (cfg.get("rename") or {}).get(tab) or {}
        cols = [ren.get(x, x) for x in cols]
        cap = CAPACITY.get(tab)
        if cap and len(rows) > cap and c.get("mode") != "append":
            print(f"  ! {tab}: {len(rows)} rows but the sheet only reads {cap} — narrow the query's date range")
        fetched[tab] = (cols, rows)

    failures = []
    for tab, c in tabs.items():
        try:
            ws = sh.worksheet(tab)
            cols, rows = fetched[tab]
            if c.get("mode", "replace") == "append":
                do_append(ws, tab, cols, rows, c.get("key_columns") or [], args.dry_run)
            else:
                do_replace(ws, tab, cols, rows, args.dry_run)
        except Exception as e:  # keep going with the other tabs, fail the run at the end
            failures.append(tab)
            print(f"  X {tab}: {e}")

    if failures:
        sys.exit(f"Failed tabs: {failures}")
    print("Done." + (" (dry run, nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
