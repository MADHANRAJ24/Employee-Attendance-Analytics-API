# DECISIONS.md

1. **Indexes.**
   On `employees`, unique `emp_code` guarantees employee identity; compound `(department, joined_on)` and `joined_on` power department filtering and headcount lookups. On `attendance_logs`, unique `(emp_code, date)` guarantees one record per day and prevents race conditions; `(date, -1), (emp_code, 1)` and `(status, 1), (date, -1), (emp_code, 1)` support sorted pagination; and `(date, 1), (late_minutes, 1)` backs leaderboard aggregations. I rejected indexing `history.at` because audit records are append-only and never queried directly.

2. **Punch-in race.**
   Both requests compute the attendance date independently from the timestamp and shift rules. Instead of check-then-insert, both issue an atomic `insert_one`. The database engine executes one insert first, returning 201. The concurrent insert violates the unique `(emp_code, date)` index, triggering PyMongo's `DuplicateKeyError`, which the API catches to return 409 Conflict.

3. **Ties.**
   The aggregation pipeline uses `$setWindowFields` with `$rank` to apply standard competition ranking (e.g., 1, 2, 2, 4). The `{ rank: { $lte: limit } }` filter applies after ranking, so all employees tied at or below the cutoff are returned, sorted by `total_late_minutes` descending then `emp_code` ascending.

4. **Headcount.**
   The pipeline starts from `employees` (filtering `joined_on <= month_end`) and `$lookup`s `attendance_logs`. Grouping by department counts every employee document, ensuring employees with zero logs are included in headcount.

5. **One thing you would change for 100x the data.**
   I would replace real-time cross-collection aggregations with pre-aggregated daily summaries maintained asynchronously via MongoDB Change Streams.
