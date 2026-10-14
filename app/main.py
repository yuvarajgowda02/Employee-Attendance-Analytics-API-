"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this file: config -> time/rule helpers -> serialization -> request models ->
indexes -> employees -> attendance -> analytics pipelines -> explain.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False: real environment variables take priority

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
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
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    # one record per (employee, day): race-proof punch-in, employee monthly, $lookup, listing by employee
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    # listing sort (date desc, emp_code asc), month scans for summary / leaderboard
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # listing with a status filter, same sort order
    att.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # punch-out: latest record of an employee with punch_in <= t
    att.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # DB not reachable yet: /health reports 503 and retries index creation
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Errors / small helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: str, msg: str) -> HTTPException:
    """422 with the same shape as FastAPI's own validation errors."""
    return HTTPException(422, [{"loc": ["query", loc], "msg": msg, "type": "value_error"}])


def hu(value: Any, places: int) -> float:
    """Round half-up (not banker's, not binary-float rounding) to `places` decimals."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_num(v: Any) -> Any:
    """Mongo number (possibly Decimal128) -> Python number; None stays None."""
    if v is None:
        return None
    if isinstance(v, Decimal128):
        return float(v.to_decimal())
    return v


def to_decimal(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal128):
        return v.to_decimal()
    return Decimal(str(v))


def parse_date(value: str, name: str) -> date_cls:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise unprocessable(name, "must be a date in YYYY-MM-DD format")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise unprocessable(name, "not a valid calendar date")


def parse_month(value: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first day, last day) as YYYY-MM-DD strings."""
    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", value or "")
    if not m:
        raise unprocessable("month", "must be YYYY-MM")
    y, mo = int(m.group(1)), int(m.group(2))
    if y < 1:
        raise unprocessable("month", "must be YYYY-MM")
    last = calendar.monthrange(y, mo)[1]
    return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{last:02d}"


# --------------------------------------------------------------------------- #
# Time and business-rule helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: naive values are UTC by contract
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def shift_moment(day: str, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = date_cls.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts an early-morning
    punch-in (IST time-of-day < shift_end) belongs to the shift that began the day before."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, day: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    seconds = (punch_in - shift_moment(day, shift_start)).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, day: str) -> int:
    """R3: floor minutes after shift end, counted only when >= 30. Overnight shifts end the next day."""
    end = shift_moment(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up, computed in Decimal (no float rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return hu((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), 2)


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: str) -> dict:
    """All derived fields for a record, from its final state (used by punch-out and PATCH)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], day)
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_LIMIT
    return out


# --------------------------------------------------------------------------- #
# Serialization (Mongo document -> API shape). _id never leaves the service.
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _history_value(key: str, v: Any) -> Any:
    return dt_to_ms(v) if key in ("punch_in", "punch_out") and isinstance(v, datetime) else v


def attendance_out(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _history_value(k, c.get("from")), "to": _history_value(k, c.get("to"))}
                for k, c in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
# strict=True: floats, strings and booleans are rejected (422); the range rejects values in seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # rejects 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply not modelled -> ignored


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(dict(doc))  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(dict(doc))  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    derived = derive(rec["status"], pin, ts, emp, rec["date"])
    # the filter on punch_out: null makes the close atomic - of two racers only one matches
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **derived}}
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update({"punch_out": ts, **derived})
    return attendance_out(rec)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date(date_from, "date_from") if date_from is not None else None
    d_to = parse_date(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from", "date_from must not be after date_to")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


StatusLit = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLit] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    status = body.status or old_status
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if status in PRESENCE:
        final_in = new_in if new_in is not None else old_in
        final_out = new_out if new_out is not None else old_out
        if final_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if new_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if final_out is not None and (new_in is not None or new_out is not None):
            if final_out <= final_in or final_out - final_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if new_in is not None or new_out is not None:
            raise HTTPException(422, "punch times cannot be supplied with status ABSENT or LEAVE")
        final_in = final_out = None

    derived = derive(status, final_in, final_out, emp, date)
    old_vals = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new_vals = {"status": status, "punch_in": final_in, "punch_out": final_out, **derived}
    changes: dict = {}
    for k, nv in new_vals.items():
        ov = old_vals[k]
        same = (dt_to_ms(ov) == dt_to_ms(nv)) if isinstance(ov, datetime) or isinstance(nv, datetime) else ov == nv
        if not same:
            changes[k] = {"from": ov, "to": nv}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_history = len(rec.get("history") or [])
    # Optimistic concurrency: only apply if the record is exactly as we read it (same inputs, same
    # history length). A concurrent edit makes this match nothing -> 409 instead of a lost history entry.
    res = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_history]},
        },
        {"$set": new_vals, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    rec.update(new_vals)
    rec["history"] = list(rec.get("history") or []) + [entry]
    return attendance_out(rec)


# --------------------------------------------------------------------------- #
# Analytics: expressions shared by the pipelines
# --------------------------------------------------------------------------- #
DATE_FMT = "%Y-%m-%d"
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": DATE_FMT}}}, 5]}
PRESENT_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # missing half_day -> false
PRESENT_DAYS_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_VALUE, 0]}
PRESENT_DAYS_ANY = {"$cond": [IS_PRESENT, PRESENT_VALUE, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
OT_MIN = {"$ifNull": ["$overtime_minutes", 0]}


def halfup4(expr: Any) -> dict:
    """Half-up to 4 decimals inside MongoDB ($round is half-to-even). Works on Decimal128, null-safe."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, 0.5]}}, 10000]}


def count_working_days(start: str, end: str, joined_on: str) -> int:
    """R7: Mon-Fri between max(start, joined_on) and end, inclusive (at most 31 iterations)."""
    d = date_cls.fromisoformat(max(start, joined_on))
    last = date_cls.fromisoformat(end)
    n = 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# ---- employee monthly ------------------------------------------------------ #
def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": OT_MIN},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end))), {})
    working = count_working_days(start, end, emp["joined_on"])
    present = float(agg.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": hu(Decimal(str(present)) * 100 / Decimal(working), 2) if working else None,
    }


# ---- department summary ---------------------------------------------------- #
def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from `employees` so people with zero logs are still counted (R9), then pulls each
    employee's month of logs with an indexed $lookup and folds everything per department."""
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        emp_match["department"] = department
    wh_ok = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_DAYS_WEEKDAY},
                    # decimal sums keep the later average exact
                    "wh_sum": {"$sum": {"$cond": [wh_ok, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_count": {"$sum": {"$cond": [wh_ok, 1, 0]}},
                    "late_count": {"$sum": IS_LATE},
                    "late_minutes": {"$sum": LATE_MIN},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$set": {"s": {"$ifNull": [{"$first": "$s"}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "wh_sum": {"$sum": "$s.wh_sum"},
            "wh_count": {"$sum": "$s.wh_count"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    start, end = parse_month(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(start, end, department)):
        n = int(r.get("wh_count", 0))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": hu(to_decimal(r.get("present_days")), 2),
            "avg_work_hours": hu(to_decimal(r.get("wh_sum")) / n, 2) if n else None,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    stages: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        stages.append({"$match": {"e.department": department}})  # ranking happens inside the department
    stages += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return stages


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": rows}


# ---- department trend ------------------------------------------------------ #
def trend_pipeline(department: str, d_from: date_cls, d_to: date_cls) -> list:
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    if not emps:
        raise HTTPException(404, "department not found")
    f, t = d_from.isoformat(), d_to.isoformat()
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    joined = [e["joined_on"] for e in emps]
    return [
        {"$match": {"emp_code": {"$in": [e["emp_code"] for e in emps]}, "date": {"$gte": f, "$lte": t}}},
        # one throw-away document guarantees $densify has input even when no log exists in the range
        {"$unionWith": {"pipeline": [{"$documents": [{"date": f, "status": "_seed"}]}]}},
        {"$group": {"_id": "$date", "present": {"$sum": PRESENT_DAYS_ANY}, "late": {"$sum": IS_LATE}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": DATE_FMT}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},  # one row per day
        {"$set": {
            "date": {"$dateToString": {"date": "$d", "format": DATE_FMT}},
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "is_wd": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
        }},
        {"$set": {
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "cond": {"$lte": ["$$this", "$date"]}}}},
        }},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            halfup4({"$divide": [{"$toDecimal": "$present"}, "$headcount"]}),
            None,
        ]}}},
        {"$sort": {"d": 1}},
        # window = current row + up to 6 previous rows of the requested range; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"avg7": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": "$is_wd",
            "headcount": 1,
            "present_count": {"$toDouble": "$present"},
            "late_count": "$late",
            "attendance_rate": "$rate",
            "moving_avg_7d": halfup4("$avg7"),
        }},
    ]


def validate_trend_range(frm: str, to: str) -> tuple[date_cls, date_cls]:
    d_from, d_to = parse_date(frm, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise unprocessable("to", "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("to", "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = validate_trend_range(from_, to)
    rows = db.attendance_logs.aggregate(trend_pipeline(department, d_from, d_to))
    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": r["present_count"],
        "late_count": r["late_count"],
        "attendance_rate": to_num(r.get("attendance_rate")),
        "moving_avg_7d": to_num(r.get("moving_avg_7d")),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
EndpointName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**values):
    for name, v in values.items():
        if v is None:
            raise unprocessable(name, "field required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EndpointName,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLit] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch builds the query with the very same functions the real endpoint uses.
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd: dict = {
            "find": coll,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            start, end = parse_month(month)
            coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, start, end)
        elif endpoint == "department_summary":
            _require(month=month)
            start, end = parse_month(month)
            coll, pipeline = "employees", summary_pipeline(start, end, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            start, end = parse_month(month)
            coll, pipeline = "attendance_logs", leaderboard_pipeline(start, end, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            d_from, d_to = validate_trend_range(from_, to)
            coll, pipeline = "attendance_logs", trend_pipeline(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
