# Employee Attendance & Analytics API

FastAPI + MongoDB 6.0+. All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or use a local .env
python sample_seed.py            # optional: loads the 9 sample documents
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `REVIEW.md` lists the defects fixed in the starter; `DECISIONS.md` has the design answers.

## Status of this submission
Everything in `openapi.yaml` is implemented. **Not tested against a live MongoDB**: the authoring sandbox could not reach PyPI or a MongoDB server. Only the rule helpers (R1-R5, rounding, input validation) were run, against the contract's own examples. Please run the app once against your MongoDB and check the aggregation endpoints (`$densify`, `$setWindowFields`, `$unionWith` + `$documents` in the trend pipeline need 6.0+).

Design notes: working days are counted arithmetically in Python (at most 31 steps); logs dated before an employee's `joined_on` still count toward their stats; a concurrent PATCH loser gets 409.
