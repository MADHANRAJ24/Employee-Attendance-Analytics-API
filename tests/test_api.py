import concurrent.futures
from datetime import datetime, timezone
import pytest
from starlette.testclient import TestClient

from app.main import app, db, client, init_indexes

test_client = TestClient(app)


def setup_module():
    init_indexes()


def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud_and_validation():
    db.employees.delete_many({"emp_code": {"$in": ["EMP8881", "EMP8882"]}})

    # Validation failure: shift_start == shift_end
    bad_emp = {
        "emp_code": "EMP8881",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-05-01",
    }
    r = test_client.post("/employees", json=bad_emp)
    assert r.status_code == 422

    # Validation failure: bad emp_code pattern
    bad_code = dict(bad_emp, shift_end="18:00", emp_code="USER001")
    r = test_client.post("/employees", json=bad_code)
    assert r.status_code == 422

    # Valid creation
    valid_emp = {
        "emp_code": "EMP8881",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
    }
    r = test_client.post("/employees", json=valid_emp)
    assert r.status_code == 201
    created = r.json()
    assert created["emp_code"] == "EMP8881"
    assert "created_at" in created
    assert isinstance(created["created_at"], int)  # epoch millis
    assert "_id" not in created

    # Duplicate creation -> 409
    r = test_client.post("/employees", json=valid_emp)
    assert r.status_code == 409

    # List employees with filter and pagination
    r = test_client.get("/employees?department=QA&page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert data["items"][0]["emp_code"] == "EMP8881"


def test_punch_in_and_rules():
    db.employees.delete_many({"emp_code": "EMP8882"})
    db.attendance_logs.delete_many({"emp_code": "EMP8882"})

    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP8882",
            "name": "Punch Tester",
            "email": "punch@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01",
        },
    )

    # Unknown employee -> 404
    r = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r.status_code == 404

    # Bad timestamp (seconds instead of ms) -> 422
    r = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8882", "punched_at": 1783312500},
    )
    assert r.status_code == 422

    # Punch in on 2026-07-06 at 09:40:00 IST (within 10m grace period)
    # 09:40:00 IST = 04:10:00 UTC
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ts_grace = int(dt_grace.timestamp() * 1000)

    r = test_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP8882",
            "punched_at": ts_grace,
            "status": "PRESENT",
        },
    )
    assert r.status_code == 201
    rec = r.json()
    assert rec["date"] == "2026-07-06"
    assert rec["late_minutes"] == 0  # exactly 10 min, grace applies!

    # Duplicate punch in for same day -> 409
    r = test_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP8882",
            "punched_at": ts_grace,
        },
    )
    assert r.status_code == 409

    # Punch in on 2026-07-07 at 09:40:01 IST (strictly > 10m grace period)
    # 09:40:01 IST = 04:10:01 UTC
    dt_late = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    ts_late = int(dt_late.timestamp() * 1000)
    r = test_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP8882",
            "punched_at": ts_late,
            "status": "PRESENT",
        },
    )
    assert r.status_code == 201
    rec = r.json()
    assert rec["date"] == "2026-07-07"
    assert rec["late_minutes"] == 10  # whole minutes floor since shift start!


def test_punch_out_and_rules():
    # Punch out for 2026-07-06
    # In at 04:10:00 UTC (09:40:00 IST)
    # Out at 13:20:00 UTC (18:50:00 IST) -> worked 9 hours 10 minutes = 9.17 hours
    # Shift end is 18:30 IST -> left at 18:50 IST -> 20 min overtime (< 30m threshold, so overtime_minutes = 0)
    dt_out = datetime(2026, 7, 6, 13, 20, 0, tzinfo=timezone.utc)
    ts_out = int(dt_out.timestamp() * 1000)

    r = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8882", "punched_at": ts_out},
    )
    assert r.status_code == 200
    rec = r.json()
    assert rec["date"] == "2026-07-06"
    assert rec["work_hours"] == 9.17
    assert rec["half_day"] is False
    assert rec["overtime_minutes"] == 0

    # Second punch out for the same record -> 409
    r = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8882", "punched_at": ts_out},
    )
    assert r.status_code == 409

    # Punch out for 2026-07-07 with half-day and overtime
    # In at 04:10:01 UTC (09:40:01 IST)
    # Out at 07:40:01 UTC (13:10:01 IST) -> 3.50 hours worked (< 4.50 -> half_day = True)
    dt_out2 = datetime(2026, 7, 7, 7, 40, 1, tzinfo=timezone.utc)
    r = test_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP8882",
            "punched_at": int(dt_out2.timestamp() * 1000),
        },
    )
    assert r.status_code == 200
    rec2 = r.json()
    assert rec2["work_hours"] == 3.50
    assert rec2["half_day"] is True


def test_overnight_shift_and_rules():
    db.employees.delete_many({"emp_code": "EMP8883"})
    db.attendance_logs.delete_many({"emp_code": "EMP8883"})

    # Overnight shift: 22:00 to 06:00
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP8883",
            "name": "Night Worker",
            "email": "night@example.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-07-01",
        },
    )

    # Punch in at 02:00 IST on 2026-07-10 (earlier than shift_end 06:00)
    # 02:00 IST on 2026-07-10 = 20:30 UTC on 2026-07-09
    dt_in = datetime(2026, 7, 9, 20, 30, 0, tzinfo=timezone.utc)
    r = test_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP8883",
            "punched_at": int(dt_in.timestamp() * 1000),
            "status": "PRESENT",
        },
    )
    assert r.status_code == 201
    rec = r.json()
    # Belonging to the shift that started the previous day (2026-07-09)
    assert rec["date"] == "2026-07-09"

    # Punch out at 06:45 IST on 2026-07-10 (01:15 UTC on 2026-07-10)
    # Shift end was 06:00 on July 10. Punch out at 06:45 -> 45 minutes overtime (>= 30m)
    dt_out = datetime(2026, 7, 10, 1, 15, 0, tzinfo=timezone.utc)
    r = test_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP8883",
            "punched_at": int(dt_out.timestamp() * 1000),
        },
    )
    assert r.status_code == 200
    rec = r.json()
    assert rec["date"] == "2026-07-09"
    assert rec["overtime_minutes"] == 45
    assert rec["work_hours"] == 4.75
    assert rec["half_day"] is False


def test_regularize_and_audit():
    # Regularize EMP8882 on 2026-07-06
    # Change status to WFH
    r = test_client.patch(
        "/attendance/EMP8882/2026-07-06",
        json={
            "status": "WFH",
            "reason": "Approved work from home request",
            "regularized_by": "manager.jane",
        },
    )
    assert r.status_code == 200
    rec = r.json()
    assert rec["status"] == "WFH"
    assert len(rec["history"]) == 1
    h = rec["history"][0]
    assert h["by"] == "manager.jane"
    assert h["reason"] == "Approved work from home request"
    assert "status" in h["changes"]
    assert h["changes"]["status"]["from"] == "PRESENT"
    assert h["changes"]["status"]["to"] == "WFH"

    # No change attempt -> 422
    r = test_client.patch(
        "/attendance/EMP8882/2026-07-06",
        json={
            "status": "WFH",
            "reason": "Duplicate regularization",
            "regularized_by": "manager.jane",
        },
    )
    assert r.status_code == 422


def test_explain_endpoints():
    endpoints = [
        ("attendance_list", "/admin/explain/attendance_list?emp_code=EMP0001"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07&limit=5"),
        ("department_trend", "/admin/explain/department_trend?department=Sales&from=2026-07-01&to=2026-07-10"),
    ]
    for name, url in endpoints:
        r = test_client.get(url)
        assert r.status_code == 200, f"Explain failed for {name}: {r.text}"
        data = r.json()
        assert data["endpoint"] == name
        assert "collection" in data
        assert "explain" in data
        exp = data["explain"]
        assert "queryPlanner" in exp or "stages" in exp


def test_concurrency_race():
    db.employees.delete_many({"emp_code": "EMP8884"})
    db.attendance_logs.delete_many({"emp_code": "EMP8884"})

    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP8884",
            "name": "Race Tester",
            "email": "race@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )

    dt = datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc)
    ts = int(dt.timestamp() * 1000)

    def do_punch_in():
        from starlette.testclient import TestClient
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": "EMP8884", "punched_at": ts})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        results = [f.result() for f in futures]

    status_codes = [r.status_code for r in results]
    assert status_codes.count(201) == 1, f"Expected exactly one 201, got {status_codes}"
    assert status_codes.count(409) == 4, f"Expected four 409s, got {status_codes}"
