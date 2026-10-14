# REVIEW.md

Defects found in the starter `app/main.py`, and what I did about each.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses `int(seconds/60)` before comparing with 10, so the grace test is on whole minutes. 09:40:30 gives 10, which is not > 10, so it is not late. R2 says late is strictly more than 10:00. | Shift 09:30, punch 09:40:01 must give 10; the starter gives 0. | Compare seconds against 600, then floor to minutes. |
| 2 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=..)` on the UTC value, so it is not IST. It also ignores overnight shifts, where the start is on the previous day. | Punch 09:45 IST is 04:15 UTC, so it is compared with 09:30 UTC and comes out hugely negative or wrong. | The start is built from the attendance `date` plus `shift_start` in IST. |
| 3 | `compute_work_hours` | `round()` is banker's rounding on a float, so 0.125 becomes 0.12 and not 0.13. Seconds are not truncated first. | 18 seconds gives 0.005 h, which must be 0.01. | `Decimal` with `ROUND_HALF_UP`; instants are truncated to whole seconds. |
| 4 | `compute_overtime` | There is no 30-minute minimum (R3). It also uses `date_str` as the shift end day, which is wrong for overnight shifts, and `int()` truncates instead of flooring. | Punch-out 18:45 gives 15 min but must give 0. Overnight punch-out at 06:40 the next day gives a negative number clamped to 0. | The 30-minute threshold, plus next-day end for overnight shifts. |
| 5 | `create_employee` | Check-then-insert is a race: two concurrent creates both pass the check. | Parallel POSTs give 2x 201. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 6 | `create_employee` | `datetime.now()` is a naive local time, not UTC. The response returns the raw datetime and not epoch ms. Nothing is validated: the emp_code pattern, email, shift format, bad dates, `shift_start == shift_end`. | POST with `joined_on: "2026-02-30"` or `shift_start: "25:00"` returns 201. | Pydantic validation (422), UTC `created_at`, ms in the response. |
| 7 | `list_employees` | `skip = page * page_size` skips the first page; it should be `(page-1)`. | Page 1 returns the second page of rows. | Corrected. |
| 8 | `list_employees` | `total` is `count_documents({})`, not the filtered count. There is no sort, so the order is undefined. | Filter by department: `total` is the whole collection. | Count with the filter, sort by `emp_code`. |
| 9 | list endpoints | `page` and `page_size` are unvalidated: `page=0` or `page_size=100000` is accepted. | `?page_size=1000` returns 1000 rows. | `Query(ge=1, le=100)` gives 422 (R10). |
| 10 | `punch_in` | A missing employee leaves `emp=None`, so `emp["shift_start"]` raises and the client gets a 500 instead of 404. | Punch in with an unknown `emp_code`. | Explicit 404. |
| 11 | `punch_in` | The timestamp is converted with `fromtimestamp` / `now()`, which uses the server's local time and gives a naive value. The date is therefore not IST (R1). Seconds are not truncated. Overnight rule not applied. | Punch 00:30 IST gives the previous day's date on a UTC server. | UTC-aware datetimes, IST date, overnight rule, truncation to seconds. |
| 12 | `punch_in` | `if body.punched_at` treats 0 as "not given". `status` is any string, so `"ABSENT"` or `"x"` is accepted. | `status: "foo"` returns 201. | `Optional[int]` is checked with `is not None`; strict epoch-ms type with range (422 for seconds, floats, strings); `status` is a Literal of the three presence values. |
| 13 | `punch_in` | Check-then-insert is a race (two simultaneous punch-ins both get 201). Nothing in the DB enforces one record per day. | Fire 10 parallel punch-ins. | Unique index (`emp_code`, `date`); `DuplicateKeyError` becomes 409. |
| 14 | `punch_in` | The response includes `id` (the stringified `_id`) and raw datetimes. The contract forbids `_id`, and instants must be epoch ms. | Look at the response body. | `attendance_out` serializer. |
| 15 | `list_attendance` | Loads the whole collection into memory, sorts in Python, and slices. | About 100k records: slow and memory-hungry. | Query sorted, skipped and limited in MongoDB, with matching indexes. |
| 16 | `list_attendance` | The sort is only by `date`, with no `emp_code` tie-break, so paging is unstable. `date_from > date_to`, malformed dates and bad `status` are not rejected. Also leaks `_id` as `id`. | Page through records of one date. | Sort (`date` desc, `emp_code` asc); 422 on invalid input. |
| 17 | `/health` | Always returns ok, even when MongoDB is down. | Stop MongoDB, `/health` still returns 200. | Pings MongoDB, else 503. |
| 18 | module level | No indexes at all, and `MongoClient` is not `tz_aware`, so datetimes come back naive. | `explain` shows COLLSCAN. | Indexes created at startup (idempotent); `tz_aware=True` UTC client. |

## Examined and judged not a defect
* `load_dotenv()` does not override real environment variables by default, which is what the contract asks for.
* `doc.pop("_id")` after `insert_one` is fine in itself (insert adds `_id` to the dict). I now insert a copy so the response never holds it.
* `late_minutes` stored as an int and `history: []` on insert match DATA_MODEL.
