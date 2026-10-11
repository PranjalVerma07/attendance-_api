# DECISIONS.md

**Concurrency.** Races are closed in the database, not in Python. `(emp_code, date)` is a unique index, so simultaneous punch-ins yield one 201 and 409s. Punch-out uses `find_one_and_update` with `punch_out: null` in the filter (compare-and-set). PATCH reads, validates, then updates only if status/punch times/history length are unchanged, retrying up to 5 times before 409, so no history entry is lost.

**Derived fields are stored.** Late/overtime/work hours/half day are computed once at write time (rules in one function, `derive_fields`) and analytics read the stored values, as the contract requires.

**Exact rounding.** Mongo's `$round` is half-even, so I avoid it for reported numbers. Averages are summed as integer cents and rounded half-up in Decimal; trend rates are integer units of 1e-4 using `floor((2N+D)/(2D))`. The moving average averages the *rounded* daily rates (my reading of "attendance_rate values").

**Trend pipeline.** Gap filling uses `$densify`; headcount is a running `$setWindowFields` sum of "joined" events, so no Python loop over days. Two `$unionWith` marker rows guarantee both range ends exist even with zero logs.

**Indexes.** `(emp_code,date)` unique; `(date -1,emp_code)` and `(status,date -1,emp_code)` serve the sorted list and month scans; `(emp_code,punch_in -1)` serves punch-out; employees get `(department,joined_on)`.

**Unverified.** I could not run MongoDB where this was written; pipelines should be checked with `/admin/explain` on a real dataset.
