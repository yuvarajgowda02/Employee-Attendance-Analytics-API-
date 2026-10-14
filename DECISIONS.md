# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate guarantee, lookups), `(department, emp_code)` (filtered listing in sort order), `joined_on` (headcount scan in the department summary). `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly and `$lookup`), `(date desc, emp_code)` (listing order, month scans for summary and leaderboard), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (punch-out lookup). I considered a covering index including `late_minutes` for the leaderboard and rejected it: more write cost for a small gain.

2. **Punch-in race.** Both requests compute the same date and try `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which I turn into 409. There is no read-then-write, so there is no window.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4), then keep every row with `rank <= limit`. Two people tied at the cutoff both appear, so the list can be longer than `limit`. Order is total minutes descending, then `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and left-joins each person's logs with `$lookup`. Someone with no logs still produces a row, with zero stats.

5. **100x data.** Stop recomputing months on read: keep pre-aggregated monthly rollups per employee, updated on each write, and shard attendance by `emp_code`.
