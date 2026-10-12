# Candidate kit

Read in this order:

1. `PROBLEM\_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA\_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample\_data/` holds the nine sample documents as MongoDB Extended JSON; `sample\_seed.py` loads them. The grader's dataset is much larger and contains cases they do not show.



## Local setup to run application

```bash
python -m venv .venv \&\& source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO\_URI / MONGO\_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample\_seed.py
uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## 

### What is implemented

All endpoints in `openapi.yaml`: employees, punch-in, punch-out, attendance list, PATCH regularization with history, the monthly employee report, department summary, late leaderboard, department trend, and `GET /admin/explain/{endpoint}`.
The analytics are MongoDB aggregation pipelines; nothing is computed by looping over fetched documents. The explain endpoint builds the query with the same functions the real endpoint uses.



### Choices worth knowing about

* **Races:** uniqueness is enforced by unique indexes (`emp\\\_code`, and `(emp\\\_code, date)`), not by "check then insert".
Punch-out is an atomic update that only matches while `punch\\\_out` is still null. PATCH only writes if the history is exactly what it read, so a concurrent edit returns 409 instead of overwriting history.
* **Leaderboard ties:** `limit` is applied after ranking, so everyone tied at the cutoff is returned and the response can have more than `limit` rows.
* **Moving average (trend):** it is calculated from the unrounded daily rates and rounded to 4 decimals at the end.
* **PATCH "changes nothing":** a request is rejected with 422 when none of `status`, `punch\\\_in` or `punch\\\_out` actually changes.
* **Punch-out:** it closes the employee's most recent record whose punch-in is at or before `punched\\\_at`; if that record is already closed the answer is 409.



### How it was checked

* The business rules (late, overtime, work hours, half day, overnight shifts, rounding) were tested against every example in the contract and the sample data.
* The endpoints were exercised for validation errors (422), missing records (404), duplicates and concurrent requests (409).
* The analytics results were compared with values calculated separately in plain Python for the sample data and for extra cases: an employee with no logs, weekend records, a tie, and a mid-month joiner.
* On a generated set of about 98,000 attendance records every explain plan used index scans and none used a collection scan.



