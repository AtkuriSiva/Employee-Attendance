# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:



Here is the **list of defects** that are associated with the starter `app/main.py`:



|**#**|**Where (function / line)**|**What is wrong**|**How you'd notice it (test, input, or symptom)**|**How you fixed it**|
|-|-|-|-|-|
|1|Module setup, L22|`MongoClient` is created without `tz\\\_aware=True`, so every datetime read back from MongoDB is naive (no time zone).|Subtracting a stored `punch\\\_out` from an aware IST datetime raises `TypeError: can't subtract offset-naive and offset-aware datetimes` (reproduced, see row 10).|`MongoClient(..., tz\\\_aware=True)`. All instants are handled as aware UTC and converted to IST only where the rules need it.|
|2|Module setup, L22-23 (minor)|The connection falls back to a hard-coded `localhost` / `attendance\\\_db`.|If the environment variable is missing the app silently talks to the wrong database (by reading).|Kept only as a local-development default; real environment variables always win over `.env`, and the README says to set them.|
|3|App setup, L27 (nothing after it)|No indexes are created anywhere, and uniqueness is only "checked" with `find\\\_one` before inserting.|Any `explain` on these collections shows `COLLSCAN`, and duplicates are possible under concurrency (rows 16 and 26). By reading.|A startup `lifespan` handler creates the indexes idempotently: unique `emp\\\_code`, unique `(emp\\\_code, date)` and the query indexes.|
|4|`compute\\\_late\\\_minutes`, L35|`punch\\\_in.replace(hour=..., minute=...)` uses the punch-in's own time zone and calendar day instead of the IST shift start.|A punch-in at 10:05 IST stored as UTC (04:35Z) returns 0 late minutes; the correct answer is 35 (reproduced).|Convert to IST and measure from the shift start of the attendance date (`shift\\\_bounds`).|
|5|`compute\\\_late\\\_minutes`, L36-37|The minutes are floored first and then compared with `> 10`, so the grace period boundary (rule R2: late only if strictly more than 10:00) is wrong.|Punch-in at 09:40:01 for a 09:30 shift returns 0; it must return 10 (reproduced).|Compare whole seconds (`> 600`), then floor to minutes.|
|6|`compute\\\_late\\\_minutes` (whole function)|No truncation to whole seconds (R1) and no overnight-shift handling: an early-morning punch-in of a 22:00-06:00 shift is measured against the wrong day's shift start.|22:00-06:00 shift, punch-in 02:00: it belongs to the previous day's shift and should be 240 minutes late (by reading).|Times are truncated to whole seconds when read; `attendance\\\_date()` picks the shift day and `shift\\\_bounds()` builds the correct start.|
|7|`compute\\\_work\\\_hours`, L41|Python `round()` on a float is banker's rounding; rule R4 needs half-up.|9 h 07 min 30 s = 9.125 h returns 9.12; the contract expects 9.13 (reproduced).|Whole seconds divided by `Decimal(3600)` and quantized with `ROUND\\\_HALF\\\_UP`.|
|8|`compute\\\_overtime`, L47|No 30-minute minimum (R3).|Punch-out 18:35 with shift end 18:30 returns 5; it must return 0 (reproduced).|Return the minutes only when they are at least 30, otherwise 0.|
|9|`compute\\\_overtime`, L46|The shift end is always built on the record's calendar date, so overnight shifts get the wrong end time.|Shift 22:00-06:00, punch-out 06:40 the next day returns 1480; the correct answer is 40 (reproduced).|`shift\\\_bounds()` adds one day to the end when `shift\\\_end <= shift\\\_start`.|
|10|`compute\\\_overtime`, L47|It subtracts an aware IST datetime from the naive datetime that comes back from MongoDB.|`TypeError` on any stored record, which becomes an HTTP 500 (reproduced).|Same fix as row 1: every datetime is timezone-aware.|
|11|Missing helpers; `punch\\\_in`, L114-115|There is no half-day rule (R5: under 4.5 hours) and no attendance-date rule (R1); `half\\\_day` is hard-coded to `False` and the date is just `ts.date()`.|A 3.5 hour day is never marked as a half day (by reading).|One `derive()` function computes `work\\\_hours`, `late\\\_minutes`, `overtime\\\_minutes` and `half\\\_day`; punch-in, punch-out and PATCH all use it.|
|12|`EmployeeIn`, L53-60|No validation at all.|`POST /employees` with `emp\\\_code "x"`, `email "nope"`, `shift\\\_start "99:99"`, `joined\\\_on "not-a-date"` and an empty name returns 201; the contract says 422 (reproduced).|Field patterns (`EMP` + 4-6 digits, `HH:MM`, email shape), length limits, a real-calendar-date check on `joined\\\_on`, and `shift\\\_start != shift\\\_end`.|
|13|`PunchInIn.punched\\\_at`, L65|`Optional\\\[int]` is lenient: Pydantic accepts `1783312500000.0`, numeric strings and `True`, and a seconds-sized timestamp passes.|A float timestamp and a seconds-style timestamp (`1783312500`) both return 201; the contract says 422 (reproduced).|`StrictInt` with bounds 100,000,000,000 to 4,102,444,800,000, so floats, strings, bools and seconds-style values are 422.|
|14|`PunchInIn.status`, L66|`status: str` accepts any text, including `ABSENT`, `LEAVE` and nonsense.|`status "banana"` is stored and returns 201 (reproduced).|`Literal\\\["PRESENT", "WFH", "ON\\\_DUTY"]`.|
|15|`health`, L72-74|It always returns `ok` and never touches MongoDB.|With MongoDB unreachable `/health` still returns 200; the contract says 503 (reproduced).|`client.admin.command("ping")`; any `PyMongoError` returns 503.|
|16|`create\\\_employee`, L79-83|Check-then-insert race with no unique index: two simultaneous requests can both pass `find\\\_one`.|Same mechanism as row 26, which I reproduced for punch-in; not separately reproduced here.|Unique index on `emp\\\_code`; `insert\\\_one` is attempted directly and `DuplicateKeyError` becomes 409.|
|17|`create\\\_employee`, L82-85 (and `list\\\_employees` items)|`datetime.now()` is naive local time, and `created\\\_at` is returned as an ISO string instead of epoch milliseconds.|The response contained `created\\\_at: '2026-10-11T16:54:07.290425'` (reproduced).|Stored as aware UTC; every response goes through a serializer that returns epoch milliseconds.|
|18|`list\\\_employees`, L93|`skip = page \\\* page\\\_size` is off by one page.|With `page=1\\\&page\\\_size=5` the first item returned was `EMP0005`, not `EMP0001`; with `page\\\_size=100` page 1 returned no items (reproduced).|`skip = (page - 1) \\\* page\\\_size`.|
|19|`list\\\_employees`, L94|`total` counts the whole collection and ignores the `department` filter (rule R10).|With `department=Ops`, `total` equalled the size of the entire collection, not the number of matches (reproduced).|`count\\\_documents` uses the same filter as the query.|
|20|`list\\\_employees`, L95|No sort, so page order is not guaranteed. The contract orders by `emp\\\_code`.|Pages can overlap or skip people (by reading).|`.sort("emp\\\_code", 1)`; it is served by the `(department, emp\\\_code)` and `emp\\\_code` indexes.|
|21|`list\\\_employees` L89, `list\\\_attendance` L130-131|`page` and `page\\\_size` are not bounded.|`page=0` returns 200 (an empty page on `/attendance`); `page\\\_size=0` returns everyone (MongoDB treats `limit(0)` as "no limit"); `page\\\_size=100000` is accepted; all should be 422 (reproduced).|`Query(1, ge=1)` for page and `Query(20, ge=1, le=100)` for page\_size.|
|22|`punch\\\_in`, L101 and L113|An unknown employee makes `emp` equal `None`, and `emp\\\["shift\\\_start"]` raises `TypeError`.|`POST /attendance/punch-in` with an unknown `emp\\\_code` returns 500; the contract says 404 (reproduced).|Explicit lookup that returns 404 first.|
|23|`punch\\\_in`, L102|`datetime.fromtimestamp(ms / 1000)` builds a naive server-local time and keeps the milliseconds (R1 wants whole seconds), and `if body.punched\\\_at` treats `0` as "not given".|`punched\\\_at: 0` was silently replaced by "now" and stored under today's date (reproduced).|`from\\\_ms()` uses integer `ms // 1000` and an aware UTC datetime; the check is `is not None`.|
|24|`punch\\\_in`, L103|The date comes from `ts.date()` in server-local time, not IST, and there is no overnight rule (R1).|On a UTC server a punch-in at 02:00 IST on 7 July was stored under `2026-07-06` (reproduced).|`attendance\\\_date()` converts to IST and moves early-morning punch-ins of overnight shifts to the previous day.|
|25|`punch\\\_in`, L104-105 and L118|The duplicate check (`find\\\_one`, L104) and the insert (L118) are two separate steps, so there is a gap between them (check-then-insert race).|20 simultaneous identical punch-ins all returned 201 and 20 records were stored for one employee and day (reproduced, with a short delay between check and insert to imitate network latency).|Unique index on `(emp\\\_code, date)`; `insert\\\_one` directly, and `DuplicateKeyError` becomes 409, so exactly one request wins.|
|26|`punch\\\_in`, L119-121|The response leaks the internal id as `id`, and `punch\\\_in` comes back as an ISO string, not epoch milliseconds.|Response contained an `id` field and `punch\\\_in: '2026-07-06T20:30:00'` (reproduced).|A `record\\\_out()` serializer builds the documented shape (no id, epoch milliseconds, defaults for missing fields).|
|27|`list\\\_attendance`, L144-147|It loads every matching document into memory, sorts in Python and slices; `total` is `len(docs)`.|On 100,000 records each page request reads the whole matching set and cannot use an index for the sort (by reading).|`find(q).sort(...).skip(...).limit(...)` in MongoDB, `count\\\_documents(q)` for `total`, and indexes for the sort and filters.|
|28|`list\\\_attendance`, L145|It sorts only by `date`, so records on the same day come back in insertion order. The contract says date descending, then `emp\\\_code` ascending.|Three same-day records inserted as EMP0030, EMP0010, EMP0020 came back in that order (reproduced).|Sort `\\\[("date", -1), ("emp\\\_code", 1)]`, backed by the `(date desc, emp\\\_code)` index.|
|29|`list\\\_attendance`, L148-150|It leaks `id`, returns datetimes as strings, and does not default fields that older documents lack (`history`, `half\\\_day`, ...).|The response contained `id` (reproduced); legacy documents would be missing fields (by reading).|The same `record\\\_out()` serializer, with defaults (`history = \\\[]`, `half\\\_day = false`, late/overtime `0`).|
|30|`list\\\_attendance`, L125-143|No validation of query parameters.|`date\\\_from=junk`, `status=NOPE`, `page=0` and `page\\\_size=100000` all return 200; `date\\\_from` after `date\\\_to` is not rejected (reproduced).|Dates must be real `YYYY-MM-DD`, `status` is a `Literal`, `date\\\_from <= date\\\_to`, page limits as in row 21.|





Also note anything you looked at and decided was **not** a defect, and why.



**Looked at and decided was not a defect**



* `load\\\_dotenv()` (L20): by default it does not override variables that already exist in the environment, so values injected by the grader win over `.env`. This is what we want.
* Plain `def` endpoints with the synchronous PyMongo client: FastAPI runs them in a thread pool and `MongoClient` is thread-safe, so they do not block the event loop. No need to switch to async.
* `doc.pop("\\\_id", None)` after `insert\\\_one` in `create\\\_employee` (L84): `insert\\\_one` adds `\\\_id` to the dict, so removing it is correct. The problem in that function is the `created\\\_at` format (row 17), not a leaked id.
* `IST = timezone(timedelta(hours=5, minutes=30))` (L25): India has no daylight-saving time, so a fixed offset is right.
* `status\\\_code=201` on the two create endpoints matches the contract.
* Negative late minutes (punching in early): the `> 10` guard already returns 0, so there is no negative value to clean up.
* `requirements.txt` (`pymongo\\\[srv]`) already covers Atlas connection strings.









