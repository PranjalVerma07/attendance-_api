"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, real environment variables win)

Layout of this single file
  1. configuration, time helpers, business rules R1-R5, rounding (R8)
  2. request models and serialisers
  3. index creation
  4. query / pipeline builders (shared by the real endpoints AND /admin/explain)
  5. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time as Time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv() 
log = logging.getLogger("attendance")


client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]  # strict: no floats, strings, bools

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
GRACE_SECONDS = 600
MIN_OVERTIME_MINUTES = 30
MAX_SHIFT_SECONDS = 24 * 3600

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 1. Helpers: errors, time conversion, rounding, business rules
# --------------------------------------------------------------------------- #
def unprocessable(msg: str, loc: Tuple[str, ...] = ("body",)):
    """422 with the same body shape FastAPI uses for validation errors."""
    raise HTTPException(422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def parse_date(value: str, loc: Tuple[str, ...]) -> Date:
    """Strict YYYY-MM-DD (date.fromisoformat alone would also accept 20260105)."""
    if not DATE_RE.match(value or ""):
        unprocessable("date must be YYYY-MM-DD", loc)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable("not a real calendar date", loc)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    dt = EPOCH + timedelta(milliseconds=int(ms))
    return dt.replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int) -> float:
    """Half-up rounding (R8). Goes through Decimal(str(x)) so 0.125 is a true tie."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def hhmm_to_seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(emp: dict) -> bool:
    return hhmm_to_seconds(emp.get("shift_end", "18:30")) <= hhmm_to_seconds(emp.get("shift_start", "09:30"))


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning
    punch-in (earlier than shift_end) belongs to the previous day's shift."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < hhmm_to_seconds(emp.get("shift_end", "18:30")):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, emp: dict) -> Tuple[datetime, datetime]:
    """Absolute start and end instants of the shift that belongs to attendance day `date_str`."""
    day = Date.fromisoformat(date_str)
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST)
    start = midnight + timedelta(seconds=hhmm_to_seconds(emp.get("shift_start", "09:30")))
    end = midnight + timedelta(seconds=hhmm_to_seconds(emp.get("shift_end", "18:30")))
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor(minutes since shift start)."""
    secs = int((punch_in - shift_start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (exact Decimal arithmetic)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive_fields(emp: dict, status: str, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record in one place (used by punch-in, punch-out and PATCH)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or punch_in is None:
        return out
    start, end = shift_bounds(date_str, emp)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = out["work_hours"] < 4.50  # R5, on the rounded value
    return out


def month_range(month: str) -> Tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: Date, last: Date) -> int:
    """R7: Mon-Fri between two dates inclusive (at most 31 iterations, not data-dependent)."""
    n, d = 0, first
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 2. Models and serialisers
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{
        "emp_code": "EMP0001", "name": "Asha Verma", "email": "asha@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}]}}
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{"emp_code": "EMP0001", "status": "PRESENT"}]}}
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{"emp_code": "EMP0001"}]}}
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{
        "status": "WFH", "reason": "Approved work from home", "regularized_by": "HR01"}]}}
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


def employee_out(doc: dict) -> dict:
    created = doc.get("created_at")
    if created is None and doc.get("_id") is not None:
        created = doc["_id"].generation_time  # legacy rows without created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created),
    }


def history_out(entries: Optional[list]) -> list:
    out = []
    for e in entries or []:
        changes = {}
        for field, ch in (e.get("changes") or {}).items():
            conv = dt_to_ms if field in ("punch_in", "punch_out") else (lambda v: v)
            changes[field] = {"from": conv(ch.get("from")), "to": conv(ch.get("to"))}
        out.append({"at": dt_to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def record_out(doc: dict) -> dict:
    """Stored document -> API shape. No _id; missing legacy fields get their defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# 3. Indexes (idempotent, run at startup)
# --------------------------------------------------------------------------- #

def _create_index(coll, keys, **kwargs):
    coll.create_index(keys, **kwargs)


def ensure_indexes() -> None:
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    _create_index(emp, [("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    _create_index(emp, [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    _create_index(emp, [("joined_on", ASCENDING)], name="joined")
    # one record per (employee, day): punch-in race guard, monthly report, PATCH lookup
    _create_index(logs, [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    # GET /attendance with no emp_code (sorted date desc, emp_code asc), month reports, trend
    _create_index(logs, [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    _create_index(logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # punch-out: "latest record of this employee with punch_in <= t"
    _create_index(logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # Mongo not reachable yet: /health will retry
        log.error("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 4. Query and pipeline builders (used by endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions over one attendance document.
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None}}}  # 1 = Sunday
X_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
X_IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
X_HALF_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
X_PRESENT_ANYDAY = {"$cond": [X_IS_PRESENT, X_HALF_WEIGHT, 0]}                      # trend (R6, half day = 0.5)
X_PRESENT_WEEKDAY = {"$cond": [{"$and": [X_IS_PRESENT, X_WEEKDAY]}, X_HALF_WEIGHT, 0]}  # monthly / summary (R7)
X_LATE = {"$ifNull": ["$late_minutes", 0]}
X_IS_LATE = {"$cond": [{"$gt": [X_LATE, 0]}, 1, 0]}
X_HAS_HOURS = {"$and": [X_IS_PRESENT, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
X_HOURS_CENTS = {"$cond": [X_HAS_HOURS, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}


def attendance_find_spec(emp_code, date_from, date_to, status, page, page_size):
    q: Dict[str, Any] = {}
    d_from = parse_date(date_from, ("query", "date_from")) if date_from else None
    d_to = parse_date(date_to, ("query", "date_to")) if date_to else None
    if d_from and d_to and d_from > d_to:
        unprocessable("date_from must not be after date_to", ("query", "date_from"))
    if emp_code:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": X_PRESENT_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": X_IS_LATE},
            "total_late_minutes": {"$sum": X_LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def dept_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Logs for the month (index scan) -> per-employee totals -> join employees for the
    department -> per-department totals; headcount comes from the employees collection
    through $unionWith so employees with zero logs still count (R9)."""
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end}}
    lookup_match: List[dict] = []
    if department:
        emp_match["department"] = department
        lookup_match = [{"$match": {"department": department}}]
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": X_PRESENT_WEEKDAY},
            "late_count": {"$sum": X_IS_LATE},
            "total_late_minutes": {"$sum": X_LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "hours_cents": {"$sum": X_HOURS_CENTS},
            "hours_n": {"$sum": {"$cond": [X_HAS_HOURS, 1, 0]}},
        }},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code",
            "pipeline": lookup_match + [{"$project": {"_id": 0, "department": 1}}], "as": "e",
        }},
        {"$unwind": "$e"},  # logs of unknown employees (or other departments) drop out here
        {"$group": {
            "_id": "$e.department",
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_cents": {"$sum": "$hours_cents"},
            "hours_n": {"$sum": "$hours_n"},
        }},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]}},
        {"$group": {  # merge the "log totals" row and the "headcount" row of each department
            "_id": "$_id",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_cents": {"$sum": "$hours_cents"},
            "hours_n": {"$sum": "$hours_n"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    lookup_match: List[dict] = [{"$match": {"department": department}}] if department else []
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code",
            "pipeline": lookup_match + [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "e",
        }},
        {"$unwind": "$e"},
        # $rank = standard competition ranking (1, 2, 2, 4): equal totals share a rank, next rank skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]


def trend_range(date_from: str, date_to: str) -> Tuple[Date, Date]:
    d_from = parse_date(date_from, ("query", "from"))
    d_to = parse_date(date_to, ("query", "to"))
    if d_to < d_from:
        unprocessable("'to' must not be before 'from'", ("query", "to"))
    if (d_to - d_from).days + 1 > 92:
        unprocessable("range must not exceed 92 days", ("query", "to"))
    return d_from, d_to


def trend_pipeline(department: str, emp_codes: List[str], date_from: str, date_to: str) -> list:
    """One row per calendar day. Gap filling with $densify, headcount as a running sum of
    'joined' events, and the 7-day moving average with $setWindowFields. Rates are kept as
    integers in units of 1e-4 so that half-up rounding (R8) is exact, not float luck."""
    d_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
    d_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
    in_window = {"documents": [-6, "current"]}
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": X_PRESENT_ANYDAY}, "late_count": {"$sum": X_IS_LATE}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        # employees: one "joined" event per employee, clamped to the first day of the range
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department, "joined_on": {"$lte": date_to}}},
            {"$project": {"_id": 0, "joined": {"$literal": 1}, "day": {"$dateFromString": {"dateString": {
                "$cond": [{"$lt": ["$joined_on", date_from]}, date_from, "$joined_on"]}}}}},
        ]}},
        # two marker rows so the range always has both ends, even with no logs at all
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "bounds": {"$literal": [d_from, d_to]}}},
            {"$unwind": "$bounds"},
            {"$project": {"day": "$bounds"}},
        ]}},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"},
            "joined": {"$sum": "$joined"},
        }},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1, "joined": 1}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "joined": {"$ifNull": ["$joined", 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
        }},
        # rate * 10000, rounded half-up:  floor((2*10000*present + h) / (2h))
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "rate_sum": {"$sum": "$rate_int", "window": in_window},
                "rate_n": {"$sum": {"$cond": [{"$ne": [{"$ifNull": ["$rate_int", None]}, None]}, 1, 0]}, "window": in_window},
            },
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$ne": [{"$ifNull": ["$rate_int", None]}, None]},
                                          {"$divide": ["$rate_int", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$rate_n", 0]},
                                        {"$divide": [{"$floor": {"$divide": [
                                            {"$add": [{"$multiply": ["$rate_sum", 2]}, "$rate_n"]},
                                            {"$multiply": [2, "$rate_n"]}]}}, 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# 5. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, body.status, date_str, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one parallel request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    p_in = rec["punch_in"]
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    seconds = (ts - p_in).total_seconds()
    if seconds <= 0:
        unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if seconds > MAX_SHIFT_SECONDS:
        unprocessable("punch-out is more than 24 hours after punch-in", ("body", "punched_at"))
    derived = derive_fields(emp, rec["status"], rec["date"], p_in, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": p_in},  # compare-and-set: one winner
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort(sort).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> Tuple[dict, dict]:
    """Validate a correction against the stored record; return ($set fields, history changes)."""
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    status = body.status if body.status is not None else old["status"]
    p_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
    p_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(f"{status} clears punch times; do not send punch_in/punch_out with it")
        p_in = p_out = None
    else:
        if p_in is None:
            unprocessable("a presence status requires a punch_in", ("body", "punch_in"))
        if attendance_date(p_in, emp) != rec["date"]:
            unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if p_out is not None:
            secs = (p_out - p_in).total_seconds()
            if secs <= 0:
                unprocessable("punch_out must be after punch_in", ("body", "punch_out"))
            if secs > MAX_SHIFT_SECONDS:
                unprocessable("punch_out must be within 24 hours of punch_in", ("body", "punch_out"))

    new = {"status": status, "punch_in": p_in, "punch_out": p_out,
           **derive_fields(emp, status, rec["date"], p_in, p_out)}

    def same(a, b):
        return dt_to_ms(a) == dt_to_ms(b) if isinstance(a, datetime) or isinstance(b, datetime) else a == b

    if all(same(old[f], new[f]) for f in ("status", "punch_in", "punch_out")):
        unprocessable("the request changes nothing")
    changes = {f: {"from": old[f], "to": new[f]} for f in old if not same(old[f], new[f])}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    for _ in range(5):  # optimistic concurrency: re-read and retry if somebody else wrote first
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(emp, rec, body)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history = rec.get("history")
        flt = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "history": {"$size": len(history)} if isinstance(history, list) else {"$exists": False},
        }
        updated = db.attendance_logs.find_one_and_update(
            flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# ---- analytics ------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    start, end = month_range(month)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)))
    agg = rows[0] if rows else {}
    first = max(Date.fromisoformat(start), Date.fromisoformat(emp["joined_on"]))
    last = Date.fromisoformat(end)
    working_days = count_working_days(first, last) if first <= last else 0
    present = agg.get("present_days", 0)
    pct = None
    if working_days:
        pct = float(
    (Decimal(str(present)) * 100 / Decimal(working_days))
    .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    start, end = month_range(month)
    items = []
    for r in db.attendance_logs.aggregate(dept_summary_pipeline(start, end, department)):
        n, cents = int(r.get("hours_n", 0)), int(r.get("hours_cents", 0))
        avg = None
        if n:
            avg = float((Decimal(cents) / Decimal(100 * n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r.get("present_days", 0), 2),
            "avg_work_hours": avg,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_range(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    trend_range(date_from, date_to)
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(trend_pipeline(department, codes, date_from, date_to)))
    for row in items:
        row["present_count"] = round_half_up(row["present_count"], 2)
    return {"department": department, "items": items}


# ---- admin: explain ---------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(value, name: str):
    if value is None or value == "":
        unprocessable(f"{name} is required", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        cmd = {"find": "attendance_logs", "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
        collection = "attendance_logs"
    else:
        collection = "attendance_logs"
        if endpoint == "employee_monthly":
            start, end = month_range(_need(month, "month"))
            pipeline = monthly_pipeline(_need(emp_code, "emp_code"), start, end)
        elif endpoint == "department_summary":
            start, end = month_range(_need(month, "month"))
            pipeline = dept_summary_pipeline(start, end, department)
        elif endpoint == "late_leaderboard":
            start, end = month_range(_need(month, "month"))
            pipeline = leaderboard_pipeline(start, end, limit, department)
        else:  # department_trend
            dept = _need(department, "department")
            trend_range(_need(from_, "from"), _need(to, "to"))
            codes = db.employees.distinct("emp_code", {"department": dept})
            pipeline = trend_pipeline(dept, codes, from_, to)
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    try:
        raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(500, f"explain failed: {exc}")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}