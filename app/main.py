"""
Employee Attendance & Analytics API

FastAPI application providing attendance tracking and analytics with MongoDB.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env file
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required database indexes idempotently at startup."""
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8 Rounding: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC BSON/Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to UTC datetime, truncating to whole seconds (R1)."""
    if ms is None:
        return None
    truncated_ms = ms - (ms % 1000)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(end_h, end_m)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2 Late.
    Grace period 10 minutes. A punch-in is late only if strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) elapsed since shift_start, not since grace ended.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    start_dt = datetime.strptime(attendance_date, "%Y-%m-%d").replace(
        hour=start_h, minute=start_m, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    diff_sec = Decimal((punch_out_utc - punch_in_utc).total_seconds())
    hours = (diff_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(
    punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str
) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    Overnight shifts: shift end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    end_h, end_m = map(int, shift_end.split(":"))
    end_date_dt = datetime.strptime(attendance_date, "%Y-%m-%d")
    if shift_end <= shift_start:
        end_date_dt += timedelta(days=1)
    end_dt = end_date_dt.replace(
        hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7 Working days.
    Monday - Friday, no holiday calendar. For mid-month joiners, counted from joined_on.
    """
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = date(y, m, 1)
    month_end = date(y, m, num_days)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > month_end:
        return 0
    start_date = max(month_start, joined_date)

    cur = start_date
    working_days = 0
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def serialize_employee(doc: dict) -> dict:
    """Format employee document according to OpenAPI Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    """Format attendance document according to OpenAPI AttendanceRecord schema."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_history_entry(h: dict) -> dict:
    """Format history entry according to OpenAPI HistoryEntry schema."""
    changes_api = {}
    for k, v in h.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            changes_api[k] = {
                "from": dt_to_epoch_ms(v.get("from")),
                "to": dt_to_epoch_ms(v.get("to")),
            }
        else:
            changes_api[k] = {
                "from": v.get("from"),
                "to": v.get("to"),
            }
    return {
        "at": dt_to_epoch_ms(h["at"]),
        "by": h["by"],
        "reason": h["reason"],
        "changes": changes_api,
    }


def json_safe(doc: Any) -> Any:
    """Convert BSON types in MongoDB results to JSON-safe primitives."""
    return json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. 409 on duplicate emp_code enforced by unique index."""
    now_dt = datetime.now(timezone.utc)
    # Truncate instant to whole seconds (R1)
    now_sec = now_dt.replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_sec

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees: filter by department, paginated, sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record punch-in: creates attendance record (R1, R2); race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_dt(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_in_utc = now_dt.replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record punch-out: closes most recent open record; computes R3-R5; race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_dt(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_out_utc = now_dt.replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_utc = candidate["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_end"], emp["shift_start"], candidate["date"])

    # Atomic update to avoid race conditions between simultaneous punch-outs
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_attendance(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records: filter, paginated, sorted by date desc then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Regularize (manually correct) one day's record: recompute derived fields, append to history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Setting status ABSENT or LEAVE clears both punch times; supplying times with it is 422
    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_status = body.status
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    elif body.status is None and record["status"] in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied for ABSENT or LEAVE record",
            )
        target_status = record["status"]
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Target status is a presence status (PRESENT, WFH, ON_DUTY)
        target_status = body.status if body.status is not None else record["status"]
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        computed_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date}",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], emp["shift_start"], date
            )
        else:
            target_work_hours = None
            target_half_day = False
            target_overtime_minutes = 0

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)

    # Detect what actually changed
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes_db: Dict[str, Dict[str, Any]] = {}
    if record.get("status") != target_status:
        changes_db["status"] = {"from": record.get("status"), "to": target_status}
    if old_punch_in != target_punch_in:
        changes_db["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        changes_db["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if record.get("work_hours") != target_work_hours:
        changes_db["work_hours"] = {"from": record.get("work_hours"), "to": target_work_hours}
    if record.get("late_minutes", 0) != target_late_minutes:
        changes_db["late_minutes"] = {"from": record.get("late_minutes", 0), "to": target_late_minutes}
    if record.get("overtime_minutes", 0) != target_overtime_minutes:
        changes_db["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": target_overtime_minutes,
        }
    if record.get("half_day", False) != target_half_day:
        changes_db["half_day"] = {"from": record.get("half_day", False), "to": target_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="No fields changed")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic locking: ensure history length hasn't changed concurrently
    curr_hist_len = len(record.get("history", []))
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, curr_hist_len]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance(updated_doc)


def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],  # Mon=2, Fri=6
                    ]
                },
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_weekday",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee: backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = round_half_up(agg.get("present_days", 0.0), 2)
        leave_days = agg.get("leave_days", 0)
        late_count = agg.get("late_count", 0)
        total_late = agg.get("total_late_minutes", 0)
        total_ot = agg.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    match_filter: Dict[str, Any] = {"joined_on": {"$lte": f"{month}-{num_days:02d}"}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-{num_days:02d}"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_work_hours_count", 0]},
                        {"$divide": ["$total_work_hours", "$total_work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month: cross-collection aggregation including 0-log employees."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    for item in results:
        item["present_days"] = round_half_up(item.get("present_days"), 2)
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"), 2)

    return {"month": month, "items": results}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard: ranking with ties (competition ranking), limit applied after ranking."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, from_date_str: str, to_date_str: str
) -> List[Dict[str, Any]]:
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date_str, "%Y-%m-%d")
    to_dt_plus_one = to_dt + timedelta(days=1)

    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date_str, "$lte": to_date_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_doc",
            }
        },
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc_doc.hc"}, 0]}}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        "$moving_avg_raw",
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department: gap-filling and 7-day moving average in MongoDB."""
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_)
    d_to = date.fromisoformat(to)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_, to)
    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        item["present_count"] = round_half_up(item.get("present_count"), 2)
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Raw MongoDB explain output with executionStats for an endpoint's main query or aggregation."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        explain_doc = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": "attendance_list",
            "collection": "attendance_logs",
            "explain": json_safe(explain_doc),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_doc = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": "employee_monthly",
            "collection": "attendance_logs",
            "explain": json_safe(explain_doc),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": "department_summary",
            "collection": "employees",
            "explain": json_safe(explain_doc),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_doc = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": "late_leaderboard",
            "collection": "attendance_logs",
            "explain": json_safe(explain_doc),
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        d_from = date.fromisoformat(from_)
        d_to = date.fromisoformat(to)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to cannot be earlier than from")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_, to)
        explain_doc = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": "department_trend",
            "collection": "attendance_logs",
            "explain": json_safe(explain_doc),
        }
