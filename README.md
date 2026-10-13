# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, manual regularization with audit trails, and reporting analytics.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local Docker container or MongoDB Atlas)

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Configuration**:
   The service reads `MONGO_URI` and `MONGO_DB` from environment variables (or an optional `.env` file). Default values are:
   - `MONGO_URI`: `mongodb://localhost:27017`
   - `MONGO_DB`: `attendance_db`

3. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

5. **API Documentation**:
   Once started, interactive API documentation is available at `http://localhost:8000/docs`.

## Database Indexes
All necessary indexes (unique constraints and compound query indexes) are automatically and idempotently created on application startup via the FastAPI lifespan context manager:
- `employees`: `emp_code` (unique), `(department, joined_on)`, `joined_on`
- `attendance_logs`: `(emp_code, date)` (unique), `(emp_code, punch_in)`, `(date, emp_code)`, `(status, date, emp_code)`, `(date, late_minutes)`

## Key Features & Highlights
- **Business Rules R1 - R10**: Handles overnight shifts, 10-minute grace periods for late calculations, minimum 30-minute overtime thresholds, half-day detection (<4.50 hrs), working days with mid-month joiners, and half-up rounding.
- **Race Safety**: Atomic inserts with database unique constraints prevent duplicate punch-ins; atomic `find_one_and_update` operations prevent concurrent punch-out race conditions; and optimistic locking guards against lost history entries during manual regularization.
- **MongoDB Aggregation Pipelines**: Analytics endpoints (`monthly`, `summary`, `leaderboard`, `trend`) run purely in the database using aggregation pipelines, `$setWindowFields` for competition ranking, and `$densify` for gap filling.
- **Explain Endpoint**: `GET /admin/explain/{endpoint}` exposes raw execution plans with `executionStats` confirming index scans (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

## Deliverables Included
- `app/main.py`: Complete application codebase.
- `requirements.txt`: Project dependencies.
- `REVIEW.md`: Detailed audit and defect fixes from starter code.
- `DECISIONS.md`: Architectural choices and index rationale.
- `README.md`: Setup and run instructions.
