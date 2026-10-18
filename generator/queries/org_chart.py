SCHEMA_ID = "org_chart"

QUERIES = [
    ("List every employee who has a manager, together with that manager's name.",
     """SELECT e."Name" AS employee, m."Name" AS manager
FROM "Employee" e
JOIN "Employee" m ON m."Id" = e."ManagerId\"""",
     ["self_join", "generic_names"]),

    ("List all employees and their manager's name; employees with no manager should still appear with an empty manager.",
     """SELECT e."Name", m."Name"
FROM "Employee" e
LEFT JOIN "Employee" m ON m."Id" = e."ManagerId\"""",
     ["self_join", "outer_join", "nullable"]),

    ("Which employees earn a higher salary than their direct manager?",
     """SELECT e."Name", e."Salary", m."Salary" AS manager_salary
FROM "Employee" e
JOIN "Employee" m ON m."Id" = e."ManagerId"
WHERE e."Salary" > m."Salary\"""",
     ["self_join"]),

    ("How many direct reports does each manager have? Show manager name and count, most reports first.",
     """SELECT m."Name", COUNT(*) AS direct_reports
FROM "Employee" e
JOIN "Employee" m ON m."Id" = e."ManagerId"
GROUP BY m."Id", m."Name"
ORDER BY direct_reports DESC""",
     ["self_join"]),

    ("Which employees do not manage anyone?",
     """SELECT e."Id", e."Name"
FROM "Employee" e
WHERE NOT EXISTS (SELECT 1 FROM "Employee" r WHERE r."ManagerId" = e."Id")""",
     ["self_join", "anti_join", "nullable"]),

    ("Give the IDs and names of everyone who reports, directly or indirectly, to Dana Whitfield.",
     """WITH RECURSIVE reports AS (
    SELECT e."Id", e."Name"
    FROM "Employee" e
    JOIN "Employee" boss ON boss."Id" = e."ManagerId"
    WHERE boss."Name" = 'Dana Whitfield'
    UNION ALL
    SELECT e."Id", e."Name"
    FROM "Employee" e
    JOIN reports r ON e."ManagerId" = r."Id"
)
SELECT "Id", "Name" FROM reports""",
     ["self_join"]),

    ("For each department, show its name and the name of its parent department (blank for top-level departments).",
     """SELECT d."Name", p."Name" AS parent
FROM "Department" d
LEFT JOIN "Department" p ON p."Id" = d."ParentDepartmentId\"""",
     ["self_join", "outer_join", "generic_names"]),

    ("What is the average salary of active employees in each department? Order by average salary, highest first.",
     """SELECT d."Name", AVG(e."Salary") AS avg_salary
FROM "Employee" e
JOIN "Department" d ON d."Id" = e."DepartmentId"
WHERE e."Status" = 'Active'
GROUP BY d."Id", d."Name"
ORDER BY avg_salary DESC""",
     ["flag_enum", "generic_names"]),

    ("How many active employees work remotely?",
     """SELECT COUNT(*) FROM "Employee"
WHERE "IsRemote" = 'Yes' AND "Status" = 'Active'""",
     ["flag_enum"]),

    ("Which employees have no bonus percentage set?",
     """SELECT "Name" FROM "Employee" WHERE "BonusPct" IS NULL""",
     ["nullable"]),

    ("Compute each employee's total target compensation: salary plus salary times bonus percent (treat a missing bonus as zero).",
     """SELECT "Name", "Salary" * (1 + COALESCE("BonusPct", 0) / 100) AS total_comp
FROM "Employee\"""",
     ["nullable"]),

    ("Which mentors currently have more than two ongoing mentorships? Give their names.",
     """SELECT e."Name"
FROM "Mentorship" ms
JOIN "Employee" e ON e."Id" = ms."MentorId"
WHERE ms."EndDate" IS NULL
GROUP BY e."Id", e."Name"
HAVING COUNT(*) > 2""",
     ["having", "nullable"]),

    ("Who is the highest-paid employee in each department? Return department ID, employee name and salary; if several tie, return one of them with the lowest employee ID.",
     """SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name", "Salary"
FROM "Employee"
ORDER BY "DepartmentId", "Salary" DESC, "Id\"""",
     ["distinct_on"]),

    ("For each employee, show the salary from their most recent salary-history entry.",
     """SELECT e."Name", sh."Salary"
FROM "Employee" e
JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id"
WHERE sh."EffectiveDate" = (SELECT MAX(s2."EffectiveDate") FROM "SalaryHistory" s2
                            WHERE s2."EmployeeId" = e."Id")""",
     ["correlated_subquery", "generic_names"]),

    ("Which employees were hired in the same calendar year as their manager?",
     """SELECT e."Name"
FROM "Employee" e
JOIN "Employee" m ON m."Id" = e."ManagerId"
WHERE EXTRACT(YEAR FROM e."HireDate") = EXTRACT(YEAR FROM m."HireDate")""",
     ["self_join", "dates"]),

    ("How many employees work in each country, based on their department's location? Departments without a location are ignored.",
     """SELECT l."Country", COUNT(*) AS headcount
FROM "Employee" e
JOIN "Department" d ON d."Id" = e."DepartmentId"
JOIN "Location" l ON l."Id" = d."LocationId"
GROUP BY l."Country\"""",
     []),

    ("For every employee, compute their depth in the reporting hierarchy, where the CEO (no manager) has depth 0.",
     """WITH RECURSIVE chain AS (
    SELECT "Id", "Name", 0 AS depth FROM "Employee" WHERE "ManagerId" IS NULL
    UNION ALL
    SELECT e."Id", e."Name", c.depth + 1
    FROM "Employee" e JOIN chain c ON e."ManagerId" = c."Id"
)
SELECT "Name", depth FROM chain""",
     ["self_join", "nullable"]),

    ("List employees who have been both a mentor and a mentee.",
     """SELECT "Name" FROM "Employee"
WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship")
  AND "Id" IN (SELECT "MenteeId" FROM "Mentorship")""",
     ["m2m", "self_join"]),

    ("How many employees have each employment status? Return status and count.",
     """SELECT "Status", COUNT(*) FROM "Employee" GROUP BY "Status\"""",
     ["flag_enum"]),

    ("List the 5 most recently hired terminated employees with their hire and termination dates.",
     """SELECT "Name", "HireDate", "TerminationDate"
FROM "Employee"
WHERE "Status" = 'Terminated'
ORDER BY "HireDate" DESC
LIMIT 5""",
     ["flag_enum", "top_k"]),

    ("Which office locations have no department assigned to them?",
     """SELECT l."Name"
FROM "Location" l
WHERE NOT EXISTS (SELECT 1 FROM "Department" d WHERE d."LocationId" = l."Id")""",
     ["anti_join", "nullable"]),

    ("List the ids of employees who take part in any mentorship, as mentor or as mentee.",
     """SELECT "MentorId" FROM "Mentorship"
UNION
SELECT "MenteeId" FROM "Mentorship\"""",
     ["self_join"]),

    ("Show each department id with its headcount, followed by one company-wide total row whose department is NULL.",
     """SELECT "DepartmentId", COUNT(*) AS headcount FROM "Employee" GROUP BY "DepartmentId"
UNION ALL
SELECT NULL, COUNT(*) FROM "Employee\"""",
     []),

    ("For every salary-history entry, show the employee id, effective date, salary, and the change from that employee's previous entry (NULL for their first entry).",
     '''SELECT "EmployeeId", "EffectiveDate", "Salary",
       "Salary" - LAG("Salary") OVER (PARTITION BY "EmployeeId" ORDER BY "EffectiveDate") AS "Change"
FROM "SalaryHistory"''',
     ['window']),

    ("Among active employees, how many work remotely and how many on site? Label the groups 'remote' and 'onsite'.",
     """SELECT CASE WHEN "IsRemote" = 'Yes' THEN 'remote' ELSE 'onsite' END AS arrangement, COUNT(*)
FROM "Employee"
WHERE "Status" = 'Active'
GROUP BY 1""",
     ['case', 'flag_enum']),
]
