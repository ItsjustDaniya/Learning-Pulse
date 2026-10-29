# Learning Pulse Live — daily sync

A GitHub Actions cron that pulls Metabase questions into the dump tabs of
[Learning Pulse Live](https://docs.google.com/spreadsheets/d/1kvCswOpip6dAqyIdr_l6vUdi6JC6k2vrQiSpzH6XYg0/edit)
once a day at **06:47 IST**. Every other tab (Dashboard, MBR View, Slack Report, …) recalculates on its own.

| Tab | Mode | What happens |
|---|---|---|
| Raw_Dump (NPS) | `append` | Adds only rows not already there (matched on `form_fill_date` + `user_id`). Older months loaded by hand stay as they are. |
| Lecture_Dump, Lecture_Feedback_Dump, TA_Dump, TA_Looker_Dump, Mentor_Sessions_Dump, Mentor_CSAT_Dump | `replace` | Cleared and rewritten (header + rows) every run. |

All 7 dump tabs are automated — nothing needs pasting by hand any more.

| Tab | Source |
|---|---|
| Raw_Dump | card 9452 "New-NPS-Query" (Altius) |
| Lecture_Dump | card 11642 "Instructor-Rating - student-level-Lecture-level" (last 35 days) |
| Lecture_Feedback_Dump | card 5037 "Subjective Feedback - Student:Instructor Mapping" (last 35 days) |
| TA_Dump | card 6135 "Data_Science_Mentor_help_doubt_session" (last 70 days) |
| TA_Looker_Dump | `sql/ta_looker_dump.sql` run directly on Newton School (db 4) — replaces the Looker TA Sessions export (last 70 days) |
| Mentor_Sessions_Dump | card 6161 "DS - Mentor one -one Student Overlapping" (last 70 days) |
| Mentor_CSAT_Dump | card 6167 "Mentor CSAT _DS" (last 70 days) |

A tab can come from a saved question (`card_id:`) or from a SQL file in this repo
(`sql_file:` + `database_id:`; `{{since}}` in the SQL is filled from `since: {days: N}`).
`value_map:` rewrites values after fetching — used to turn the mentor card's
`Student_did_not_join` / `Mentor_did_not_join` into the statuses the sheet's status map knows.

## Setup

1. **Repo** — copy these files in (keep `.github/workflows/` as is).
2. **config.yaml** — put the Metabase question ID next to each tab (`/question/1234-…` → `1234`). Leave `null` to skip a tab.
3. **Google service account** — in Google Cloud, enable the *Google Sheets API*, create a service account and a JSON key.
   **Share the sheet with the service account email as Editor.**
4. **GitHub secrets** (Settings → Secrets and variables → Actions):
   - `METABASE_URL` — e.g. `https://metabase.yourdomain.com` (no trailing slash)
   - `METABASE_API_KEY` — *or* `METABASE_USERNAME` + `METABASE_PASSWORD`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the whole JSON key
5. **First run** — Actions → *Learning Pulse sync* → *Run workflow*, tick **Dry run** first.
   The log shows row counts and any header mismatches without writing anything. Then run it for real.

## Things to keep in mind

- **Column names must match the sheet's headers** (the sheet reads columns by header name). If a query's alias differs,
  fix the alias in SQL or use `rename:` in `config.yaml`. The log prints `! … expects columns not in the Metabase result` when something's off.
- **Raw_Dump has `placement_process` twice.** The second one is filled from a Metabase column called `placement_process_2` if present, otherwise left blank.
- **Row limits** — Lecture_Dump reads 16,000 rows (about a month); keep that query to roughly the last 5 weeks. The log warns if a query goes over.
  Raw_Clean reads 5,000 NPS rows (enough for 2026).
- **Safety** — all queries are fetched before anything is written. A query that returns 0 rows leaves its tab untouched. If one tab fails, the rest still update and the run is marked failed (GitHub emails you).
- **Slack posting** still goes through the Apps Script trigger on the *Slack Setup* tab. Since this sync runs every morning, Monday's message will use fresh data.
- To change the time, edit the `cron:` line (it's in UTC; IST = UTC + 5:30).
