#### DECISIONS.md  (about 150-200 words in total - plain language, your own words)



1. **Indexes:**
* **Employees:** unique - `emp\_code`(identity and the 409), list filter -(department, emp\_code), headcount - ((department, joined\_on) and (joined\_on))
* **Attendance:** unique - (emp\_code, date); duplicate guard, monthly report, PATCH - (date desc, emp\_code) ; list sort, month scans - (status, date, emp\_code); status filter - (emp\_code, punch\_in desc); punch-out lookup.
* A separate late\_minutes index (the date index already narrows to the month).



2\. **Punch-in race:**  The request is validated, the employee is looked up, the IST date is computed, and insert\_one runs. The unique (emp\_code, date) index accepts the first request, and the second raises DuplicateKeyError, which becomes 409. The point to make is that there’s no separate “check” step for another request to slip between.



**3. Ties:** $rank gives 1, 2, 2, 4, and the limit is applied after ranking. So everyone tied at the cutoff is returned, which can be more than limit rows.

Cutting between equals would be arbitrary. 



**4. Headcount:** The headcount comes from the employees collection (joined by month end), separate from the logs, which only add sums. So an employee with zero logs is still counted.



**5. One thing you would change** if this had to serve 100x the data: 

* Pre-computed daily totals instead of aggregating raw logs.
* A covering index for the month-wide analytics.
* Keyset pagination instead of skip. 





