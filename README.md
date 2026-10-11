# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of `openapi.yaml` (rules R1-R10 in its description).

    pip install -r requirements.txt
    cp .env.example .env            # or export MONGO_URI / MONGO_DB (real env vars win)
    uvicorn app.main:app --port 8000

Needs MongoDB 6.0+ (`$densify`, `$setWindowFields`, `$rank`, `$unionWith`).
Indexes are created idempotently at startup (and retried from `/health` if Mongo was down at boot).
Interactive docs: http://localhost:8000/docs

See `REVIEW.md` (defects found in the starter) and `DECISIONS.md` (design choices).
