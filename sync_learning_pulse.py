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

    def card_parameters(self, card_id, spec):
        """Build the date filter using the question's own parameter definition (id, type, target)."""
        if not spec:
            return []
        r = self.s.get(f"{self.base}/api/card/{card_id}", timeout=120)
        r.raise_for_status()
        card = r.json()
        tag = spec["tag"]
        target = ["dimension", ["template-tag", tag]]
        p = next((x for x in card.get("parameters") or []
                  if x.get("target") == target or x.get("slug") == tag.lower() or x.get("name") == tag), None)
        if p is None:  # fall back to the template tag itself
            native = (card.get("dataset_query") or {}).get("native") or {}
            t = (native.get("template-tags") or {}).get(tag)
            if not t:
                tags = list((native.get("template-tags") or {}).keys())
                raise RuntimeError(f"Card {card_id}: no filter named '{tag}'. Filters on this question: {tags}")
            p = {"id": t.get("id"), "type": t.get("widget-type") or "date/all-options", "target": target}
        value = f"{cutoff_date(spec)}~{dt.date.today().isoformat()}"
        return [{"id": p["id"], "type": p.get("type") or "date/all-options",
                 "target": p.get("target") or target, "value": value}]

    def native_rows(self, database_id, sql):
        """Run a SQL file straight through Metabase (no saved question needed). Export endpoint, so no 2,000-row cap."""
        q = {"database": int(database_id), "type": "native", "native": {"query": sql, "template-tags": {}}}
        url = f"{self.base}/api/dataset/json"
        r = self.s.post(url, data={"query": json.dumps(q), "format_rows": "false"}, timeout=900)
        if r.status_code >= 400:
            r = self.s.post(url, json={"query": q, "format_rows": False}, timeout=900)
        return self._rows(r, f"SQL on database {database_id}")

    def _rows(self, r, label):
        if r.status_code >= 400:
            raise RuntimeError(f"Metabase {label} -> HTTP {r.status_code}: {r.text[:800]}")
        data = r.json()
        if isinstance(data, dict) and ("error" in data or "message" in data):
            raise RuntimeError(f"Metabase {label} failed: {data.get('error') or data.get('message')}")
        if not data:
            return [], []
        cols = list(data[0].keys())
        return cols, [[rec.get(c) for c in cols] for rec in data]

    def card_rows(self, card_id, date_spec=None):
        """All rows of a saved question as (columns, rows). Uses the export endpoint (no 2,000-row cap)."""
        url = f"{self.base}/api/card/{card_id}/query/json"
        params = self.card_parameters(card_id, date_spec)
        r = self.s.post(url, data={"parameters": json.dumps(params), "format_rows": "false"}, timeout=900)
        if r.status_code >= 400:  # newer Metabase versions also accept a JSON body
            r = self.s.post(url, json={"parameters": params, "format_rows": False}, timeout=900)
        return self._rows(r, f"card {card_id}")


def load_sql(path, since_spec):
    """Read a .sql file and fill {{since}} with the cutoff date."""
    full = path if os.path.isabs(path) else os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    sql = open(full, encoding="utf-8").read()
    if "{{since}}" in sql:
        if not since_spec:
            sys.exit(f"{path} uses {{{{since}}}} but no since: is set in config.yaml")
        sql = sql.replace("{{since}}", cutoff_date(since_spec))
    return sql


def apply_value_map(cols, rows, vmap, tab):
    """{'session_status': {'old': 'new'}} -> rewrite those values in place."""
    for col, mapping in (vmap or {}).items():
        if col not in cols:
            print(f"  ! {tab}: value_map column '{col}' not in the result")
            continue
        i = cols.index(col)
        for r in rows:
            if r[i] in mapping:
                r[i] = mapping[r[i]]
    return rows


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


def cutoff_date(spec):
    """{'days': 35} -> date 35 days ago; {'date': '2026-01-01'} -> that date. Returns 'YYYY-MM-DD'."""
    if spec.get("date"):
        return str(spec["date"])
    return (dt.date.today() - dt.timedelta(days=int(spec["days"]))).isoformat()


def keep_since(cols, rows, spec, tab):
    """Drop rows older than the cutoff, judged on one date column."""
    if not spec:
        return rows
    if spec["column"] not in cols:
        sys.exit(f"{tab}: keep_since column '{spec['column']}' not in the Metabase result")
    i, cut = cols.index(spec["column"]), cutoff_date(spec)
    return [r for r in rows if key_part(r[i])[:10] >= cut]


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
TEXT_DATE = re.compile(
    r"^\s*(\d{1,2})[\s\-/]+([A-Za-z]{3,9})\.?[\s\-/,]*(\d{4})?(?:[,\s]+\d{1,2}:\d{2}(?::\d{2})?\s*([AaPp][Mm])?)?\s*$")


def text_date(v, year):
    """'1 Sept ' / '12-Sep' / '3 September 2026' -> '2026-09-01'. Anything else is returned unchanged."""
    if not isinstance(v, str):
        return v
    m = TEXT_DATE.match(v)
    if not m or m.group(2)[:3].lower() not in MONTHS:
        return v
    y = int(m.group(3) or year)
    try:
        return dt.date(y, MONTHS[m.group(2)[:3].lower()], int(m.group(1))).isoformat()
    except ValueError:
        return v


def split_key(k):
    """'month:form_fill_date' -> ('form_fill_date', True)"""
    return (k[6:], True) if k.startswith("month:") else (k, False)


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


def do_append(ws, tab, cols, rows, key_cols, dry, text_year=None):
    header = ws.row_values(1)
    check_headers(tab, header, cols)
    text_year = text_year or dt.date.today().year

    # Map each sheet column to a Metabase column. Repeated headers (Raw_Dump has
    # placement_process twice) take name, then name_2 / name_3 ... if Metabase has them.
    seen, src = {}, []
    for h in header:
        n = seen.get(h, 0) + 1
        seen[h] = n
        cand = h if n == 1 else f"{h}_{n}"
        src.append(cols.index(cand) if cand in cols else None)

    keys = [split_key(k) for k in key_cols]
    for k, _ in keys:
        if k not in header or k not in cols:
            sys.exit(f"{tab}: key column '{k}' must be in both the sheet header and the Metabase result")

    existing = ws.get(
        f"A2:{gspread.utils.rowcol_to_a1(ws.row_count, len(header))}",
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="SERIAL_NUMBER",
    )
    kidx_sheet = [(header.index(k), m) for k, m in keys]

    def fmt(v, month):
        v = key_part(text_date(serial_to_date(v), text_year))
        return v[:7] if month else v

    # Existing rows: remember keys, and mark later repeats of the same key as duplicates.
    have, dupes, last_filled = set(), [], 1
    for i, r in enumerate(existing, start=2):
        if not any(c not in ("", None) for c in r):
            continue
        last_filled = i
        key = tuple(fmt(r[j] if j < len(r) else "", m) for j, m in kidx_sheet)
        if key in have:
            dupes.append(i)
        have.add(key)

    if dupes:
        print(f"  {tab}: removing {len(dupes)} duplicate rows already in the tab (same {key_cols}), keeping the first")
        if not dry:
            # group into contiguous blocks and delete bottom-up so row numbers stay valid
            blocks, start, prev = [], dupes[0], dupes[0]
            for d in dupes[1:]:
                if d == prev + 1:
                    prev = d
                    continue
                blocks.append((start, prev))
                start = prev = d
            blocks.append((start, prev))
            reqs = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                                   "startIndex": a - 1, "endIndex": b}}}
                    for a, b in sorted(blocks, reverse=True)]
            ws.spreadsheet.batch_update({"requests": reqs})
        last_filled -= len(dupes)

    kidx_mb = [(cols.index(k), m) for k, m in keys]
    new_rows = []
    for r in rows:
        key = tuple(fmt(r[j], m) for j, m in kidx_mb)
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
    tabs = {t: c for t, c in cfg["tabs"].items() if c and (c.get("card_id") or c.get("sql_file"))}
    if not tabs:
        sys.exit("No card_id / sql_file filled in config.yaml — nothing to sync.")
    skipped = [t for t in cfg["tabs"] if t not in tabs]
    if skipped:
        print(f"Not automated (no card_id / sql_file): {skipped}")

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(cfg["spreadsheet_id"])
    mb = Metabase()

    # Fetch everything first, so a failed query never leaves a tab half-updated.
    # A tab whose query fails is skipped; the other tabs still update.
    fetched, failures = {}, []
    for tab, c in tabs.items():
        src = f"card {c['card_id']}" if c.get("card_id") else c["sql_file"]
        print(f"Fetching {src} for {tab} ...", flush=True)
        try:
            if c.get("card_id"):
                cols, rows = mb.card_rows(c["card_id"], c.get("date_param"))
            else:
                cols, rows = mb.native_rows(c["database_id"], load_sql(c["sql_file"], c.get("since")))
        except Exception as e:
            failures.append(tab)
            print(f"  X {tab}: {str(e)[:500]}", flush=True)
            continue
        rows = keep_since(cols, rows, c.get("keep_since"), tab)
        rows = apply_value_map(cols, rows, c.get("value_map"), tab)
        ren = (cfg.get("rename") or {}).get(tab) or {}
        cols = [ren.get(x, x) for x in cols]
        cap = CAPACITY.get(tab)
        if cap and len(rows) > cap and c.get("mode") != "append":
            print(f"  ! {tab}: {len(rows)} rows but the sheet only reads {cap} — narrow the query's date range")
        fetched[tab] = (cols, rows)

    for tab, c in tabs.items():
        if tab not in fetched:
            continue
        try:
            ws = sh.worksheet(tab)
            cols, rows = fetched[tab]
            if c.get("mode", "replace") == "append":
                do_append(ws, tab, cols, rows, c.get("key_columns") or [], args.dry_run, c.get("text_date_year"))
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
