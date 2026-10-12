"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded; real environment variables win)

Layout of this file
  1. configuration + MongoDB client      5. serialisation (Mongo doc -> API shape)
  2. indexes (created at startup)        6. request models
  3. time / rule helpers (R1 - R8)       7. query + pipeline builders (shared with /admin/explain)
  4. validation helpers                  8. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time as Time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False -> real environment variables take priority over .env

log = logging.getLogger("attendance")

# tz_aware=True: every datetime read from MongoDB is timezone-aware (UTC), never naive.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),  # localhost default is for local dev only
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")          # R6
PRESENCE_L = list(PRESENCE)
GRACE_SECONDS = 600                               # R2: strictly more than 10:00 is late
MIN_OVERTIME_MINUTES = 30                         # R3
HALF_DAY_HOURS = 4.5                              # R5
MAX_SHIFT = timedelta(hours=24)
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
MAX_TREND_DAYS = 92

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

# --------------------------------------------------------------------------- #
# 2. Indexes (idempotent, run at startup)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")           # identity + 409 guard
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_dept_emp")      # list filter+sort
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined")  # trend headcount
    emp.create_index([("joined_on", ASCENDING)], name="ix_joined_on")                      # dept summary (R9)
    # attendance_logs
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")   # race-safe punch-in, monthly, PATCH, $lookup
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp")               # list sort, month-wide scans
    att.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_status_date")
    att.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_punchin")        # punch-out lookup
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up so /health can answer 503
        log.error("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 3. Time and business-rule helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch ms. Naive values are treated as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return round(value.timestamp() * 1000)


def round_half_up(value: Any, places: int = 2) -> float:
    """R4 / R8: half-up on the decimal representation (Python's round() is banker's)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _hm(text: str) -> tuple[int, int]:
    hour, minute = text.split(":")
    return int(hour), int(minute)


def shift_bounds(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """IST start/end of the shift that starts on `day`. Overnight shifts end the next day (R1)."""
    sh, sm = _hm(shift_start)
    eh, em = _hm(shift_end)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    return start, end


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST date of the punch-in; early-morning punch-ins of an overnight shift belong to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start:
        eh, em = _hm(shift_end)
        if local.time() < Time(eh, em):
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, day: Date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes counted from shift start."""
    start, _ = shift_bounds(day, shift_start, shift_end)
    seconds = int((punch_in.replace(microsecond=0) - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, day: Date) -> int:
    """R3: whole minutes after shift end, only if >= 30."""
    _, end = shift_bounds(day, shift_start, shift_end)
    minutes = int((punch_out.replace(microsecond=0) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp: dict, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: Date) -> dict:
    """All derived fields in one place, used by punch-in, punch-out and PATCH (R2 - R5)."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return derived
    derived["late_minutes"] = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"], day)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = hours
        derived["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], day)
        derived["half_day"] = hours < HALF_DAY_HOURS
    return derived


def count_weekdays(start: Date, end: Date) -> int:
    """Mon - Fri days in [start, end] (R7). Calendar arithmetic, no documents involved."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    total = full_weeks * 5
    first_leftover = start + timedelta(days=full_weeks * 7)
    total += sum(1 for i in range(rest) if (first_leftover + timedelta(days=i)).weekday() < 5)
    return total


# --------------------------------------------------------------------------- #
# 4. Validation helpers (raise FastAPI-style 422 bodies)
# --------------------------------------------------------------------------- #
def unprocessable(loc: tuple, msg: str):
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def need_date(value: str, loc: tuple) -> Date:
    if not DATE_RE.fullmatch(value):
        unprocessable(loc, "date must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a real calendar date")


def need_month(value: str, loc: tuple) -> tuple[Date, Date]:
    if not re.fullmatch(MONTH_PATTERN, value):
        unprocessable(loc, "month must be YYYY-MM")
    year, month = int(value[:4]), int(value[5:7])
    try:
        return Date(year, month, 1), Date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        unprocessable(loc, "month out of range")


def need_trend_range(from_s: str, to_s: str) -> tuple[Date, Date]:
    d_from = need_date(from_s, ("query", "from"))
    d_to = need_date(to_s, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


def require(value: Any, name: str, where: str = "query"):
    if value is None:
        unprocessable((where, name), "field required")
    return value


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Serialisation: never leak _id, instants are epoch ms, defaults for legacy docs
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(out.get("created_at"))
    return out


def _history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 6. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]  # StrictInt: no floats / strings / bools
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        if not DATE_RE.fullmatch(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(value)  # ValueError -> 422 for 2026-02-31
        return value

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Not Optional on purpose: omitted -> default None (unchanged), explicit null -> 422.
    status: Status = Field(default=None)
    punch_in: EpochMs = Field(default=None)
    punch_out: EpochMs = Field(default=None)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 7. Query / pipeline builders. The endpoints AND /admin/explain both call these,
#    so the explained query is exactly the one that runs.
# --------------------------------------------------------------------------- #
def attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        flt["date"] = {}
        if date_from is not None:
            flt["date"]["$gte"] = date_from
        if date_to is not None:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    return {"filter": flt, "sort": ATT_SORT, "skip": (page - 1) * page_size, "limit": page_size}


def check_attendance_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    d_from = need_date(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = need_date(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")


# ---- small expression builders; `p` is the field prefix: "$" | "$$l." | "$logs." ----
def weekend_dates(first: Date, last: Date) -> list:
    """ISO strings of the Saturdays and Sundays in [first, last].
    Checking a log's date string against this short list is much cheaper than parsing every log's date inside MongoDB."""
    days = (first + timedelta(i) for i in range((last - first).days + 1))
    return [d.isoformat() for d in days if d.weekday() >= 5]


def _is_weekday(date_ref: str, weekends: list) -> dict:
    return {"$eq": [{"$in": [date_ref, weekends]}, False]}   # not a Saturday/Sunday of the month


def _presence_weight(p: str) -> dict:
    """1 for a present record, 0.5 for a present half day, 0 otherwise (R5, R6) - any day of the week."""
    half = {"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}
    return {"$cond": [{"$in": [f"{p}status", PRESENCE_L]}, {"$cond": [half, 0.5, 1]}, 0]}


def _weekday_presence_weight(p: str, weekends: list) -> dict:
    """Same, but only Mon - Fri counts as a present day (R7)."""
    return {"$cond": [_is_weekday(f"{p}date", weekends), _presence_weight(p), 0]}


def _late_minutes(p: str) -> dict:
    return {"$ifNull": [f"{p}late_minutes", 0]}


def _late_flag(p: str) -> dict:
    return {"$cond": [{"$gt": [_late_minutes(p), 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    weekends = weekend_dates(first, last)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _weekday_presence_weight("$", weekends)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _late_flag("$")},
            "total_late_minutes": {"$sum": _late_minutes("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def _has_hours(p: str) -> dict:
    """True for a present record that has a numeric work_hours (these feed avg_work_hours)."""
    return {"$and": [{"$in": [f"{p}status", PRESENCE_L]}, {"$isNumber": f"{p}work_hours"}]}


def department_summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    """
    Two sources merged by department (R9):
      logs side       per-employee subtotals of the month's logs, then joined to the employee for its department.
                      Joining AFTER grouping means one lookup per employee, not one per log, and no per-employee query.
      employees side  headcount of everyone who has joined by month end, including people with no logs.
    Cost grows with the logs in the month, not with the number of employees.
    """
    first_s, last_s = first.isoformat(), last.isoformat()
    weekends = weekend_dates(first, last)
    emp_match: dict = {"joined_on": {"$lte": last_s}}
    if department is not None:
        emp_match["department"] = department
    totals = ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count", "hours_sum", "hours_cnt")
    return [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": _weekday_presence_weight("$", weekends)},
            "late_count": {"$sum": _late_flag("$")},
            "total_late_minutes": {"$sum": _late_minutes("$")},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "hours_sum": {"$sum": {"$cond": [_has_hours("$"), "$work_hours", 0]}},   # average over records,
            "hours_cnt": {"$sum": {"$cond": [_has_hours("$"), 1, 0]}},               # not average of averages
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                                      # logs of unknown employees disappear
        {"$match": {f"emp.{k}": v for k, v in emp_match.items()}},  # joined by month end (+ department filter)
        {"$group": {"_id": "$emp.department", "headcount": {"$sum": 0}, **{t: {"$sum": f"${t}"} for t in totals}}},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{t: {"$sum": 0} for t in totals}}},
        ]}},
        {"$group": {"_id": "$_id", "headcount": {"$sum": "$headcount"}, **{t: {"$sum": f"${t}"} for t in totals}}},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # plain equality lookup (fast path). Inner join: logs of unknown employees disappear BEFORE ranking
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # filter BEFORE ranking
    pipeline += [
        # standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},                       # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def trend_pipeline(department: str, d_from: Date, d_to: Date) -> list:
    """
    One row per calendar day. Driven from the department's employees:
      real rows  = per-day sums of the department's logs
      seed rows  = one zero row per calendar day, generated in the database ($range), so empty days exist
      merged, then per-day headcount ($lookup), rate, and a 7-row moving average ($setWindowFields).
    """
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": _presence_weight("$logs.")},   # any weekday or weekend
            "late_count": {"$sum": _late_flag("$logs.")},
        }},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "offset": {"$range": [0, n_days]}}},
            {"$unwind": "$offset"},
            {"$project": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                    "$dateAdd": {"startDate": start, "unit": "day", "amount": "$offset"}}}},
                "present_count": {"$literal": 0},
                "late_count": {"$literal": 0},
            }},
        ]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$_id"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}},
        }},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$sort": {"_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


# --------------------------------------------------------------------------- #
# 8. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:  # startup could not reach MongoDB; try again now that it answers
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.error("index creation failed: %s", exc)
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index on emp_code is the guarantee, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {} if department is None else {"department": department}
    total = db.employees.count_documents(flt)
    cursor = db.employees.find(flt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, ts, None, day),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_at = rec["punch_in"]
    if ts <= punch_in_at:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if ts - punch_in_at > MAX_SHIFT:
        unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")

    derived = derive(emp, rec["status"], punch_in_at, ts, Date.fromisoformat(rec["date"]))
    # Atomic compare-and-set: only the request that still sees punch_out == null can win.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
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
    check_attendance_dates(date_from, date_to)
    q = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q["filter"])
    cursor = db.attendance_logs.find(q["filter"]).sort(q["sort"]).skip(q["skip"]).limit(q["limit"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = need_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    status = body.status or old["status"]
    new_in, new_out = old["punch_in"], old["punch_out"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(("body",), "ABSENT/LEAVE clears the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        if body.punch_in is not None:
            new_in = from_ms(body.punch_in)
        if body.punch_out is not None:
            new_out = from_ms(body.punch_out)
        if new_in is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            unprocessable(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derive(emp, status, new_in, new_out, day)}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        unprocessable(("body",), "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: only apply if history is exactly what we read. A concurrent edit makes this
    # match nothing -> 409 for the loser, and no history entry is ever overwritten or lost.
    if "history" in rec and rec["history"] is not None:
        guard = {"history": {"$size": len(rec["history"])}}
    else:
        guard = {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], **guard},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = need_month(month, ("query", "month"))
    emp = get_employee_or_404(emp_code)
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last))), {})

    start = max(first, Date.fromisoformat(emp["joined_on"]))      # R7: count from joined_on
    working_days = count_weekdays(start, last)
    present = Decimal(str(agg.get("present_days", 0)))
    pct = None if working_days == 0 else round_half_up(present / Decimal(working_days) * 100, 2)
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
    first, last = need_month(month, ("query", "month"))
    items = []
    for row in db.attendance_logs.aggregate(department_summary_pipeline(first, last, department)):
        count = int(row["hours_cnt"])
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": round_half_up(Decimal(str(row["hours_sum"])) / Decimal(count), 2) if count else None,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = need_month(month, ("query", "month"))
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = need_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = []
    for row in db.employees.aggregate(trend_pipeline(department, d_from, d_to)):
        rate, moving = row.get("attendance_rate"), row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": round_half_up(row["present_count"], 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
        })
    return {"department": department, "items": items}


# ---- admin / explain ------------------------------------------------------ #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
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
        check_attendance_dates(date_from, date_to)
        q = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        inner = {"find": collection, "filter": q["filter"], "sort": dict(q["sort"]), "skip": q["skip"], "limit": q["limit"]}
    else:
        if endpoint == "employee_monthly":
            require(emp_code, "emp_code")
            first, last = need_month(require(month, "month"), ("query", "month"))
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            first, last = need_month(require(month, "month"), ("query", "month"))
            collection, pipeline = "attendance_logs", department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = need_month(require(month, "month"), ("query", "month"))
            collection, pipeline = "attendance_logs", leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            require(department, "department")
            d_from, d_to = need_trend_range(require(from_, "from"), require(to, "to"))
            collection, pipeline = "employees", trend_pipeline(department, d_from, d_to)
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}