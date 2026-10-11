# DECISIONS.md

**1. Indexes:** I created a unique `emp_code` index to prevent duplicate employees and a unique `(emp_code, date)` index to guarantee one attendance record per employee per day. The `(date, emp_code)` index supports sorted attendance queries, while `(status, date, emp_code)` supports status filtering. The `(emp_code, punch_in)` index helps find punch-out records. Employee indexes on `(department, joined_on)` and `joined_on` support department and joining-date queries. I avoided a separate `status` index because the compound index already starts with that field.

**2. Punch-in race:** Two identical requests can both pass the employee lookup, but the unique `(emp_code, date)` index allows only one attendance insertion. Our concurrency test returned one `201 Created` and one `409 Conflict`.

**3. Ties:** The leaderboard uses `$rank`, so employees with equal late minutes share the same rank. The limit is applied after ranking, so employees tied at the cutoff are included.

**4. Headcount:** The department summary uses `$unionWith` to combine attendance totals with employee counts. Employees with no attendance logs are therefore still included in department headcount.

**5. Scaling:** For 100 times more data, I would inspect execution statistics, benchmark aggregation pipelines, and consider precomputed summaries for expensive analytics.
