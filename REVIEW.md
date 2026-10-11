# REVIEW.md - defects in the starter `app/main.py`

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | punch-in date | `date` taken from the server's local naive time, not IST (R1); overnight shifts ignored | `attendance_date()` converts to IST and applies the overnight rule |
| 2 | `compute_late_minutes` | Floors to minutes *before* comparing to 10, so 09:40:30 counts as on time; shift start built from the punch's own (naive) timezone | Compare whole **seconds** `> 600`, then floor; shift start built in IST |
| 3 | `compute_overtime` | No 30-minute threshold; naive/aware datetime mix raises TypeError; overnight end not moved to next day | `compute_overtime()` with threshold and `shift_bounds()` |
| 4 | `compute_work_hours` | Python `round()` is banker's/float rounding, not half-up (R4) | `Decimal` + `ROUND_HALF_UP` |
| 5 | half day | `half_day` is never computed (R5) | Derived from the *rounded* work_hours `< 4.50` |
| 6 | timestamps | `fromtimestamp(ms/1000)` gives local naive time, fractional seconds kept, seconds/floats/strings accepted, `0` treated as "missing" | Strict epoch-ms model (`StrictInt`, range check), UTC-aware, truncated to seconds, `is not None` checks |
| 7 | `create_employee` | Check-then-insert race, no unique index, no validation (emp_code pattern, shifts equal, date), `created_at` is a local datetime | Unique index + `DuplicateKeyError` -> 409, validated model, `created_at` in epoch ms |
| 8 | `punch_in` | Unknown employee -> `None` -> unhandled 500 (should be 404); any status accepted (ABSENT/LEAVE) | 404, `PresenceStatus` only |
| 9 | `punch_in` | Check-then-insert lets parallel requests create duplicates | Unique `(emp_code, date)` index, 409 on duplicate key |
| 10 | `list_employees` | `skip = page * page_size` skips the first page; `total` counts the whole collection; no sort; `page=0`/huge `page_size` accepted | `(page-1)*size`, filtered count, sort by `emp_code`, `ge=1`/`le=100` |
| 11 | `list_attendance` | Loads the whole collection and sorts in Python (memory, slow at 100k); no `emp_code` tie-break; `date_from > date_to` not rejected; negative slices for `page<1` | Query + index-backed sort/skip/limit, 422 on bad range |
| 12 | responses | `_id`/`id` leaked, datetimes returned instead of epoch ms, legacy fields missing | `record_out()` / `employee_out()` serialisers |
| 13 | `/health` | Always "ok", never touches MongoDB | Pings Mongo, 503 on failure |
| 14 | startup | No indexes at all; client returns naive datetimes (`tz_aware` off) | `ensure_indexes()`; `tz_aware=True` |
| 15 | punch-out / PATCH / analytics | Missing entirely | Implemented (Part B) |

Checked and fine: `load_dotenv()` does not override real environment variables.
