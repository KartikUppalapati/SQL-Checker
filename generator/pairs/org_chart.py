"""Hand-written labeled predictions for org_chart queries. See campus.py for the format."""
E, N = True, False

PAIRS = {
    1: [
        (E, '''SELECT e."Name", (SELECT m."Name" FROM "Employee" m WHERE m."Id" = e."ManagerId") FROM "Employee" e WHERE e."ManagerId" IS NOT NULL''',
         "scalar subquery: FK guarantees the manager exists"),
        (N, '''SELECT e."Name", m."Name" FROM "Employee" e LEFT JOIN "Employee" m ON m."Id" = e."ManagerId"''',
         "LEFT JOIN keeps the CEO with a NULL manager"),
        (N, '''SELECT e."Name", m."Name" FROM "Employee" e JOIN "Employee" m ON e."Id" = m."ManagerId"''',
         "self-join direction reversed"),
        (N, "SELECT e.Name, m.Name FROM Employee e JOIN Employee m ON m.Id = e.ManagerId",
         "INVALID: unquoted identifiers fold to lowercase; relation employee does not exist"),
    ],
    2: [
        (E, '''SELECT e."Name", m."Name" FROM "Employee" m RIGHT JOIN "Employee" e ON e."ManagerId" = m."Id"''',
         "RIGHT JOIN with tables swapped"),
        (N, '''SELECT e."Name", m."Name" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId"''',
         "inner join drops employees without a manager"),
        (N, '''SELECT e."Name", COALESCE(m."Name", '') FROM "Employee" e LEFT JOIN "Employee" m ON m."Id" = e."ManagerId"''',
         "empty string instead of NULL"),
        (N, '''SELECT e."name", m."name" FROM "Employee" e LEFT JOIN "Employee" m ON m."Id" = e."ManagerId"''',
         'INVALID: quoted "name" is case-sensitive; the column is "Name"'),
    ],
    3: [
        (E, '''SELECT e."Name", e."Salary", m."Salary" FROM "Employee" e, "Employee" m WHERE e."ManagerId" = m."Id" AND m."Salary" < e."Salary"''',
         "implicit join, comparison mirrored"),
        (N, '''SELECT e."Name", e."Salary", m."Salary" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" WHERE e."Salary" >= m."Salary"''',
         "includes equal salaries"),
        (N, '''SELECT e."Name", e."Salary" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" WHERE e."Salary" > m."Salary"''',
         "missing manager salary column"),
        (N, '''SELECT e."Name", e."Salary", m."Salary" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" WHERE e."Salary" >= m."Salary"''',
         ">= includes employees paid the same as their manager"),
    ],
    4: [
        (E, '''SELECT m."Name", r.cnt FROM "Employee" m JOIN (SELECT "ManagerId", COUNT(*) AS cnt FROM "Employee" WHERE "ManagerId" IS NOT NULL GROUP BY "ManagerId") r ON r."ManagerId" = m."Id" ORDER BY r.cnt DESC''',
         "pre-aggregated subquery"),
        (N, '''SELECT m."Name", COUNT(*) FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" GROUP BY m."Name" ORDER BY 2 DESC''',
         "managers with the same name are merged"),
        (N, '''SELECT m."Name", COUNT(e."Id") FROM "Employee" m LEFT JOIN "Employee" e ON e."ManagerId" = m."Id" GROUP BY m."Id", m."Name" ORDER BY 2 DESC''',
         "includes every employee with 0 reports"),
    ],
    5: [
        (E, '''SELECT "Id", "Name" FROM "Employee" WHERE "Id" NOT IN (SELECT "ManagerId" FROM "Employee" WHERE "ManagerId" IS NOT NULL)''',
         "NOT IN with NULLs filtered out"),
        (E, '''SELECT e."Id", e."Name" FROM "Employee" e LEFT JOIN "Employee" r ON r."ManagerId" = e."Id" WHERE r."Id" IS NULL''',
         "LEFT JOIN anti-join"),
        (N, '''SELECT "Id", "Name" FROM "Employee" WHERE "Id" NOT IN (SELECT "ManagerId" FROM "Employee")''',
         "NOT IN over a nullable column: the CEO's NULL ManagerId makes the predicate never true"),
    ],
    6: [
        (E, '''WITH RECURSIVE r AS (
    SELECT "Id", "Name" FROM "Employee"
    WHERE "ManagerId" IN (SELECT "Id" FROM "Employee" WHERE "Name" = 'Dana Whitfield')
    UNION ALL
    SELECT c."Id", c."Name" FROM "Employee" c JOIN r ON c."ManagerId" = r."Id"
)
SELECT * FROM r''',
         "semi-join anchor; each employee has one manager so IN and JOIN give the same rows"),
        (N, '''SELECT e."Id", e."Name" FROM "Employee" e JOIN "Employee" b ON b."Id" = e."ManagerId" WHERE b."Name" = 'Dana Whitfield\'''',
         "direct reports only"),
        (N, '''WITH RECURSIVE r AS (
    SELECT "Id", "Name" FROM "Employee" WHERE "Name" = 'Dana Whitfield'
    UNION ALL
    SELECT e."Id", e."Name" FROM "Employee" e JOIN r ON e."ManagerId" = r."Id"
)
SELECT "Id", "Name" FROM r''',
         "includes Dana herself"),
        (N, '''WITH RECURSIVE reports AS (SELECT e."Id", e."Name" FROM "Employee" e JOIN "Employee" boss ON boss."Id" = e."ManagerId" WHERE boss."Name" = 'Dana Whitfield' UNION SELECT e."Id", e."Name" FROM "Employee" e JOIN reports r ON e."ManagerId" = r."Id") SELECT "Id", "Name" FROM reports''',
         "Name is not unique: if one Dana Whitfield reports to another, UNION ALL lists the lower one's reports twice and UNION once"),
    ],
    7: [
        (E, '''SELECT d."Name", (SELECT p."Name" FROM "Department" p WHERE p."Id" = d."ParentDepartmentId") FROM "Department" d''',
         "scalar subquery yields NULL for top-level departments"),
        (N, '''SELECT d."Name", p."Name" FROM "Department" d JOIN "Department" p ON p."Id" = d."ParentDepartmentId"''',
         "drops top-level departments"),
        (N, '''SELECT d."Name", c."Name" FROM "Department" d LEFT JOIN "Department" c ON c."ParentDepartmentId" = d."Id"''',
         "child departments instead of parent"),
    ],
    8: [
        (E, '''SELECT d."Name", AVG(e."Salary") FROM "Department" d JOIN "Employee" e ON e."DepartmentId" = d."Id" WHERE e."Status" = 'Active' GROUP BY d."Id" ORDER BY 2 DESC''',
         "group by primary key; Name is functionally dependent"),
        (N, '''SELECT d."Name", AVG(e."Salary") AS a FROM "Employee" e JOIN "Department" d ON d."Id" = e."DepartmentId" WHERE e."Status" = 'active' GROUP BY d."Id", d."Name" ORDER BY a DESC''',
         "enum labels are case-sensitive: 'active' is not a valid EmploymentStatus value (runtime error)"),
        (N, '''SELECT d."Name", AVG(e."Salary") AS a FROM "Employee" e JOIN "Department" d ON d."Id" = e."DepartmentId" GROUP BY d."Id", d."Name" ORDER BY a DESC''',
         "missing active filter"),
    ],
    9: [
        (E, '''SELECT COUNT("Id") FROM "Employee" WHERE "Status" = 'Active' AND "IsRemote" = 'Yes\'''',
         "COUNT of the primary key equals COUNT(*)"),
        (E, '''SELECT COUNT(*) FROM "Employee" WHERE "IsRemote" <> 'No' AND "Status" = 'Active\'''',
         "RemoteFlag has only Yes/No and the column is NOT NULL"),
        (N, '''SELECT COUNT(*) FROM "Employee" WHERE "IsRemote" = 'Yes' AND "Status" <> 'Terminated\'''',
         "includes employees on leave"),
        (N, '''SELECT COUNT(*) FROM "Employee" WHERE "IsRemote" = 'yes' AND "Status" = 'Active\'''',
         "'yes' is not a valid RemoteFlag label (runtime error)"),
        (E, '''SELECT COUNT(*) FROM "Employee" WHERE "IsRemote" = 'Yes' AND "Status" = 'Active' LIMIT 1''',
         "an aggregate without GROUP BY returns exactly one row, so LIMIT 1 changes nothing"),
    ],
    10: [
        (E, '''SELECT "Name" FROM "Employee" WHERE "BonusPct" IS NOT DISTINCT FROM NULL''',
         "IS NOT DISTINCT FROM NULL is IS NULL"),
        (N, '''SELECT "Name" FROM "Employee" WHERE "BonusPct" = 0''',
         "zero bonus is not a missing bonus"),
        (N, '''SELECT "Name" FROM "Employee" WHERE COALESCE("BonusPct", 0) = 0''',
         "also matches explicit zero bonuses"),
        (N, '''SELECT "Name" FROM "Employee" WHERE COALESCE("BonusPct", 0) = 0''',
         "also returns employees whose bonus is exactly 0"),
    ],
    11: [
        (E, '''SELECT "Name", "Salary" * (COALESCE("BonusPct", 0) / 100 + 1) FROM "Employee"''',
         "addition reordered"),
        (N, '''SELECT "Name", "Salary" * (1 + "BonusPct" / 100) FROM "Employee"''',
         "NULL bonus yields NULL compensation"),
        (N, '''SELECT "Name", "Salary" * COALESCE("BonusPct", 0) / 100 FROM "Employee"''',
         "bonus amount only, salary not added"),
    ],
    12: [
        (E, '''SELECT e."Name" FROM "Employee" e WHERE (SELECT COUNT(*) FROM "Mentorship" m WHERE m."MentorId" = e."Id" AND m."EndDate" IS NULL) > 2''',
         "correlated count"),
        (N, '''SELECT e."Name" FROM "Mentorship" ms JOIN "Employee" e ON e."Id" = ms."MentorId" GROUP BY e."Id", e."Name" HAVING COUNT(*) > 2''',
         "counts ended mentorships too"),
        (N, '''SELECT e."Name" FROM "Mentorship" ms JOIN "Employee" e ON e."Id" = ms."MentorId" WHERE ms."EndDate" IS NULL GROUP BY e."Id", e."Name" HAVING COUNT(*) >= 2''',
         "two or more instead of more than two"),
    ],
    13: [
        (E, '''SELECT "DepartmentId", "Name", "Salary" FROM (SELECT "DepartmentId", "Name", "Salary", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn = 1 ORDER BY "DepartmentId\"''',
         "ROW_NUMBER with the same tie-break, ordered like the target"),
        (N, '''SELECT "DepartmentId", "Name", "Salary" FROM (SELECT "DepartmentId", "Name", "Salary", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn = 1''',
         "target's outer ORDER BY makes row order significant; prediction is unordered"),
        (N, '''SELECT "DepartmentId", "Name", "Salary" FROM (SELECT "DepartmentId", "Name", "Salary", RANK() OVER (PARTITION BY "DepartmentId" ORDER BY "Salary" DESC) AS rk FROM "Employee") t WHERE rk = 1 ORDER BY "DepartmentId\"''',
         "RANK returns every tied top earner"),
    ],
    14: [
        (E, '''SELECT DISTINCT ON (e."Id") e."Name", sh."Salary" FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" ORDER BY e."Id", sh."EffectiveDate" DESC''',
         "UNIQUE(EmployeeId, EffectiveDate) makes the latest entry unique; extra ordering is harmless"),
        (N, '''SELECT e."Name", MAX(sh."Salary") FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" GROUP BY e."Id", e."Name"''',
         "highest historical salary, not the latest"),
        (N, '''SELECT e."Name", e."Salary" FROM "Employee" e WHERE EXISTS (SELECT 1 FROM "SalaryHistory" sh WHERE sh."EmployeeId" = e."Id")''',
         "generic column: reads Employee.Salary instead of SalaryHistory.Salary"),
    ],
    15: [
        (E, '''SELECT e."Name" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" WHERE date_part('year', e."HireDate") = date_part('year', m."HireDate")''',
         "date_part is EXTRACT"),
        (E, '''SELECT e."Name" FROM "Employee" e JOIN "Employee" m ON e."ManagerId" = m."Id" WHERE date_trunc('year', e."HireDate") = date_trunc('year', m."HireDate")''',
         "truncating to the year compares the same thing"),
        (N, '''SELECT e."Name" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId" WHERE e."HireDate" = m."HireDate"''',
         "same day, not same year"),
    ],
    16: [
        (E, '''SELECT l."Country", COUNT(e."Id") FROM "Location" l JOIN "Department" d ON d."LocationId" = l."Id" JOIN "Employee" e ON e."DepartmentId" = d."Id" GROUP BY l."Country"''',
         "join order reversed"),
        (N, '''SELECT l."Country", COUNT(*) FROM "Employee" e JOIN "Department" d ON d."Id" = e."DepartmentId" LEFT JOIN "Location" l ON l."Id" = d."LocationId" GROUP BY l."Country"''',
         "adds a NULL-country group for departments without a location"),
        (N, '''SELECT l."Country", COUNT(DISTINCT d."Id") FROM "Employee" e JOIN "Department" d ON d."Id" = e."DepartmentId" JOIN "Location" l ON l."Id" = d."LocationId" GROUP BY l."Country"''',
         "counts departments, not employees"),
    ],
    17: [
        (E, '''WITH RECURSIVE h(emp_id, emp_name, lvl) AS (
    SELECT "Id", "Name", 0 FROM "Employee" WHERE "ManagerId" IS NULL
    UNION ALL
    SELECT e."Id", e."Name", h.lvl + 1 FROM "Employee" e JOIN h ON e."ManagerId" = h.emp_id
)
SELECT emp_name, lvl FROM h''',
         "CTE column list renamed"),
        (N, '''WITH RECURSIVE h AS (
    SELECT "Id", "Name", 1 AS depth FROM "Employee" WHERE "ManagerId" IS NULL
    UNION ALL
    SELECT e."Id", e."Name", h.depth + 1 FROM "Employee" e JOIN h ON e."ManagerId" = h."Id"
)
SELECT "Name", depth FROM h''',
         "depth starts at 1 instead of 0"),
    ],
    18: [
        (E, '''SELECT e."Name" FROM "Employee" e WHERE EXISTS (SELECT 1 FROM "Mentorship" a WHERE a."MentorId" = e."Id") AND EXISTS (SELECT 1 FROM "Mentorship" b WHERE b."MenteeId" = e."Id")''',
         "EXISTS form"),
        (E, '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship" INTERSECT SELECT "MenteeId" FROM "Mentorship")''',
         "INTERSECT on ids inside IN"),
        (N, '''SELECT DISTINCT e."Name" FROM "Employee" e JOIN "Mentorship" a ON a."MentorId" = e."Id" JOIN "Mentorship" b ON b."MenteeId" = e."Id"''',
         "DISTINCT on name merges different employees with the same name"),
        (N, '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship") OR "Id" IN (SELECT "MenteeId" FROM "Mentorship")''',
         "OR instead of AND"),
    ],
    19: [
        (E, '''SELECT "Status", COUNT("Id") FROM "Employee" GROUP BY 1''',
         "ordinal GROUP BY; COUNT of PK"),
        (N, '''SELECT COUNT(*), "Status" FROM "Employee" GROUP BY "Status"''',
         "columns swapped"),
        (N, '''SELECT "Status", COUNT(*) FROM "Employee" WHERE "TerminationDate" IS NULL GROUP BY "Status"''',
         "extra filter"),
    ],
    20: [
        (E, '''SELECT "Name", "HireDate", "TerminationDate" FROM "Employee" WHERE "Status" = 'Terminated' ORDER BY "HireDate" DESC FETCH FIRST 5 ROWS ONLY''',
         "FETCH FIRST"),
        (N, '''SELECT "Name", "HireDate", "TerminationDate" FROM "Employee" WHERE "Status" = 'Terminated' ORDER BY "TerminationDate" DESC LIMIT 5''',
         "orders by termination date"),
        (N, '''SELECT "Name", "HireDate", "TerminationDate" FROM "Employee" WHERE "TerminationDate" IS NOT NULL ORDER BY "HireDate" DESC LIMIT 5''',
         "TerminationDate is not constrained to agree with Status"),
        (N, '''SELECT "Name", "HireDate", "TerminationDate" FROM "Employee" WHERE "Status" = 'Terminated' LIMIT 5''',
         "LIMIT without ORDER BY returns 5 arbitrary terminated employees"),
    ],
    21: [
        (E, '''SELECT "Name" FROM "Location" WHERE "Id" NOT IN (SELECT "LocationId" FROM "Department" WHERE "LocationId" IS NOT NULL)''',
         "NOT IN with NULLs filtered out"),
        (E, '''SELECT l."Name" FROM "Location" l LEFT JOIN "Department" d ON d."LocationId" = l."Id" WHERE d."Id" IS NULL''',
         "LEFT JOIN anti-join on the department primary key"),
        (E, '''SELECT "Name" FROM "Location" WHERE "Id" <> ALL (SELECT "LocationId" FROM "Department" WHERE "LocationId" IS NOT NULL)''',
         "<> ALL is NOT IN; NULLs filtered out"),
        (N, '''SELECT "Name" FROM "Location" WHERE "Id" NOT IN (SELECT "LocationId" FROM "Department")''',
         "NOT IN over a nullable column: one department without a location makes the result empty"),
        (N, '''SELECT "Name" FROM "Location" WHERE "Id" <> ALL (SELECT "LocationId" FROM "Department")''',
         "<> ALL has the same NULL trap as NOT IN"),
        (N, "SELECT l.Name FROM Location l WHERE NOT EXISTS (SELECT 1 FROM Department d WHERE d.LocationId = l.Id)",
         "INVALID: unquoted identifiers fold to lowercase; relation location does not exist"),
    ],
    22: [
        (N, '''SELECT "MentorId" FROM "Mentorship" UNION ALL SELECT "MenteeId" FROM "Mentorship"''',
         "UNION ALL repeats employees with several mentorships or with both roles"),
        (E, '''SELECT e."Id" FROM "Employee" e WHERE e."Id" IN (SELECT "MentorId" FROM "Mentorship") OR e."Id" IN (SELECT "MenteeId" FROM "Mentorship")''',
         "both columns are NOT NULL foreign keys to the Employee primary key"),
        (E, '''SELECT DISTINCT x FROM (SELECT "MentorId" AS x FROM "Mentorship" UNION ALL SELECT "MenteeId" FROM "Mentorship") t''',
         "DISTINCT over UNION ALL is UNION"),
        (N, '''SELECT DISTINCT "MentorId" FROM "Mentorship"''',
         "mentors only"),
        (N, '''SELECT "MentorId" FROM "Mentorship" WHERE "EndDate" IS NULL UNION SELECT "MenteeId" FROM "Mentorship" WHERE "EndDate" IS NULL''',
         "ongoing mentorships only"),
    ],
    23: [
        (E, '''SELECT "DepartmentId", COUNT(*) FROM "Employee" GROUP BY ROLLUP ("DepartmentId")''',
         "ROLLUP adds the same grand-total row (also on an empty table)"),
        (E, '''SELECT "DepartmentId", COUNT(*) FROM "Employee" GROUP BY "DepartmentId" UNION SELECT NULL, COUNT(*) FROM "Employee"''',
         "rows are already distinct: one per department, and DepartmentId is NOT NULL so the total row cannot collide"),
        (N, '''SELECT "DepartmentId", COUNT(*) FROM "Employee" GROUP BY "DepartmentId" UNION ALL SELECT NULL, COUNT("ManagerId") FROM "Employee"''',
         "COUNT(ManagerId) skips the CEO"),
        (N, '''SELECT "DepartmentId", COUNT(*) FROM "Employee" GROUP BY "DepartmentId"''',
         "missing the total row"),
        (N, '''SELECT "DepartmentId", COUNT(*) FROM "Employee" GROUP BY "DepartmentId" UNION ALL SELECT NULL, COUNT(DISTINCT "DepartmentId") FROM "Employee"''',
         "total row counts departments, not employees"),
    ],
}
