"""Boundary tests for each canonicalization rule: one pair where the rule's precondition holds
(must match) next to a near-identical pair where it does not (must not match).

Run:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path

from sqleval.equivalence import compare

SCHEMAS = Path(__file__).resolve().parent.parent / "dataset" / "schemas"
CAMPUS = (SCHEMAS / "campus.sql").read_text()
ORG = (SCHEMAS / "org_chart.sql").read_text()
SOCIAL = (SCHEMAS / "social_graph.sql").read_text()
SNOW = (SCHEMAS / "sales_snowflake.sql").read_text()
PROJECT = (SCHEMAS / "project_tracker.sql").read_text()

# (schema, ground truth, prediction, expected match, what it checks)
CASES = [
    # --- validity ------------------------------------------------------------------------------
    (CAMPUS, "SELECT name FROM students", None, False, "null prediction"),
    (CAMPUS, "SELECT name FROM students", "", False, "empty prediction"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students; SELECT 1", False, "two statements"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students;", True, "one statement + semicolon"),
    (CAMPUS, "SELECT name FROM students", "DELETE FROM students", False, "not a query"),
    (CAMPUS, "SELECT name FROM students", "SELECT nme FROM students", False, "unknown column"),
    (CAMPUS, "SELECT name FROM students", "SELECT NAME FROM STUDENTS", True, "unquoted identifiers fold"),
    (CAMPUS, "SELECT name FROM students", 'SELECT "NAME" FROM students', False, "quoted identifiers keep case"),
    (ORG, 'SELECT "Name" FROM "Employee"', "SELECT Name FROM Employee", False, "unquoted PascalCase folds to lowercase"),
    (SNOW, "SELECT Name FROM DimRegion", "select name from dimregion", True, "unquoted PascalCase schema"),
    (SNOW, "SELECT Name FROM DimRegion", 'SELECT "Name" FROM "DimRegion"', False, "quoting an unquoted schema"),
    (ORG, '''SELECT COUNT(*) FROM "Employee" WHERE "Status" = 'Active\'''',
     '''SELECT COUNT(*) FROM "Employee" WHERE "Status" = 'active\'''', False, "invalid enum label"),
    (CAMPUS, "SELECT c.name, COUNT(*) FROM courses c GROUP BY c.id",
     "SELECT c.name, COUNT(*) FROM courses c GROUP BY c.code", False, "UNIQUE column does not license ungrouped columns"),
    (CAMPUS, "SELECT name FROM students", "SELECT DISTINCT name FROM students ORDER BY id", False,
     "SELECT DISTINCT with ORDER BY outside the select list"),
    (CAMPUS, "SELECT COALESCE(email, 'none') FROM students", "SELECT IFNULL(email, 'none') FROM students", False,
     "IFNULL is not a PostgreSQL function"),
    (CAMPUS, "SELECT COALESCE(email, 'none') FROM students", "SELECT NVL(email, 'none') FROM students", False,
     "NVL is not a PostgreSQL function"),
    (CAMPUS, "SELECT EXTRACT(YEAR FROM hire_date) FROM instructors",
     "select extract(year from hire_date) from instructors", True, "EXTRACT(YEAR ...) is not the MySQL YEAR()"),

    # --- result shape ------------------------------------------------------------------------------
    (CAMPUS, "SELECT id, name FROM students", "SELECT s.id AS x, s.name AS y FROM students s", True, "aliases ignored"),
    (CAMPUS, "SELECT id, name FROM students", "SELECT name, id FROM students", False, "column order matters"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students ORDER BY name", True, "unordered target"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students", False, "ordered target"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students ORDER BY 1", True, "ordinal"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students ORDER BY name DESC", False, "direction"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students ORDER BY name LIMIT 5", False, "limit"),
    (CAMPUS, "SELECT name FROM students ORDER BY name LIMIT 5",
     "SELECT name FROM students ORDER BY name FETCH FIRST 5 ROWS ONLY", True, "FETCH FIRST is LIMIT"),
    (CAMPUS, "SELECT name FROM students ORDER BY name LIMIT 5",
     "SELECT name FROM students ORDER BY name FETCH FIRST 5 ROWS WITH TIES", False, "WITH TIES can return more rows"),
    (CAMPUS, "SELECT name FROM students ORDER BY name FETCH FIRST 5 ROWS WITH TIES",
     "SELECT name FROM students ORDER BY 1 FETCH NEXT 5 ROWS WITH TIES", True, "WITH TIES on both sides"),
    (CAMPUS, "SELECT name FROM students ORDER BY name LIMIT 1",
     "SELECT name FROM students ORDER BY name FETCH FIRST ROW ONLY", True, "FETCH FIRST ROW ONLY is LIMIT 1"),
    (CAMPUS, "SELECT name FROM students ORDER BY name",
     "SELECT name FROM students ORDER BY name FETCH FIRST ROW ONLY", False, "FETCH FIRST ROW ONLY still limits"),
    (CAMPUS, "SELECT name FROM instructors ORDER BY salary DESC",
     "SELECT name FROM instructors ORDER BY salary DESC NULLS FIRST", True, "DESC sorts NULLs first by default"),
    (CAMPUS, "SELECT name FROM instructors ORDER BY salary DESC",
     "SELECT name FROM instructors ORDER BY salary DESC NULLS LAST", False, "explicit NULLS LAST differs"),

    # --- arithmetic and literals ---------------------------------------------------------------------
    (CAMPUS, "SELECT 100.0 * SUM(is_international) / COUNT(*) FROM students",
     "SELECT SUM(is_international) * 100.0 / COUNT(*) FROM students", True, "commutative multiplication"),
    (CAMPUS, "SELECT 100.0 * SUM(is_international) / COUNT(*) FROM students",
     "SELECT 100 * SUM(is_international) / COUNT(*) FROM students", False, "integer division"),
    (CAMPUS, "SELECT id FROM courses WHERE credits > 3", "SELECT id FROM courses WHERE credits >= 4", True,
     "integer bounds"),
    (CAMPUS, "SELECT id FROM departments WHERE budget > 3", "SELECT id FROM departments WHERE budget >= 4", False,
     "numeric is not discrete"),
    (CAMPUS, "SELECT id FROM instructors WHERE hire_date <= '2020-12-31'",
     "SELECT id FROM instructors WHERE hire_date < DATE '2021-01-01'", True, "date bounds"),
    (CAMPUS, "SELECT id FROM instructors WHERE EXTRACT(YEAR FROM hire_date) = 2020",
     "SELECT id FROM instructors WHERE hire_date >= '2020-01-01' AND hire_date < '2021-01-01'", True, "year range"),
    (CAMPUS, "SELECT id FROM instructors WHERE EXTRACT(YEAR FROM hire_date) = 2020",
     "SELECT id FROM instructors WHERE hire_date >= '2020-01-01' AND hire_date <= '2021-01-01'", False,
     "off-by-one year range"),
    (CAMPUS, "SELECT id FROM courses WHERE credits BETWEEN 2 AND 4",
     "SELECT id FROM courses WHERE credits >= 2 AND credits <= 4", True, "BETWEEN"),
    (CAMPUS, "SELECT id FROM students WHERE email IS NULL", "SELECT id FROM students WHERE email = NULL", False,
     "= NULL"),

    # --- enums ---------------------------------------------------------------------------------------
    (ORG, '''SELECT "Id" FROM "Employee" WHERE "IsRemote" = 'Yes\'''',
     '''SELECT "Id" FROM "Employee" WHERE "IsRemote" <> 'No\'''', True, "two-label enum"),
    (ORG, '''SELECT "Id" FROM "Employee" WHERE "Status" = 'Terminated\'''',
     '''SELECT "Id" FROM "Employee" WHERE "Status" <> 'Active\'''', False, "three-label enum"),
    (ORG, '''SELECT "Id" FROM "Employee" WHERE "Status" IN ('Active', 'OnLeave')''',
     '''SELECT "Id" FROM "Employee" WHERE "Status" <> 'Terminated\'''', True, "enum complement"),

    # --- NULL-sensitive subqueries -------------------------------------------------------------------
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT name FROM departments WHERE id NOT IN (SELECT department_id FROM students)", False,
     "NOT IN over a nullable column"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT name FROM departments WHERE id NOT IN (SELECT department_id FROM students WHERE department_id IS NOT NULL)",
     True, "NOT IN with NULLs filtered"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT d.name FROM departments d LEFT JOIN students s ON s.department_id = d.id WHERE s.id IS NULL", True,
     "LEFT JOIN anti-join on a NOT NULL column"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT d.name FROM departments d LEFT JOIN students s ON s.department_id = d.id WHERE s.email IS NULL", False,
     "LEFT JOIN anti-join on a nullable column"),

    # --- semi-joins ----------------------------------------------------------------------------------
    (CAMPUS, "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)", False,
     "join duplicates rows, EXISTS does not"),
    (CAMPUS, "SELECT DISTINCT s.name FROM students s JOIN enrollments e ON e.student_id = s.id",
     "SELECT DISTINCT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)",
     True, "under DISTINCT"),
    (CAMPUS, "SELECT c.name FROM courses c JOIN departments d ON d.id = c.department_id WHERE d.budget > 100",
     "SELECT c.name FROM courses c WHERE EXISTS (SELECT 1 FROM departments d WHERE d.id = c.department_id AND d.budget > 100)",
     True, "semi-join on a key"),

    # --- foreign keys --------------------------------------------------------------------------------
    (CAMPUS, "SELECT c.name, d.name FROM courses c JOIN departments d ON d.id = c.department_id",
     "SELECT c.name, d.name FROM courses c LEFT JOIN departments d ON d.id = c.department_id", True,
     "LEFT JOIN on a NOT NULL FK"),
    (CAMPUS, "SELECT s.name, d.name FROM students s JOIN departments d ON d.id = s.department_id",
     "SELECT s.name, d.name FROM students s LEFT JOIN departments d ON d.id = s.department_id", False,
     "LEFT JOIN on a nullable FK"),
    (CAMPUS, "SELECT s.id FROM students s JOIN departments d ON d.id = s.department_id",
     "SELECT s.id FROM students s WHERE s.department_id IS NOT NULL", True, "join to parent used only for its key"),
    (CAMPUS, "SELECT s.id FROM students s JOIN departments d ON d.id = s.department_id",
     "SELECT s.id FROM students s", False, "nullable FK: the join filters NULLs"),
    (CAMPUS, "SELECT s.id, s.department_id FROM students s",
     "SELECT s.id, d.id FROM students s LEFT JOIN departments d ON d.id = s.department_id", True,
     "LEFT JOIN to parent key"),
    (CAMPUS, "SELECT s.id, s.department_id FROM students s",
     "SELECT s.id, d.id FROM students s LEFT JOIN departments d ON d.id = s.department_id AND d.budget > 0", False,
     "extra ON condition can leave the parent NULL"),
    (CAMPUS, "SELECT c.name, d.name FROM courses c JOIN departments d ON d.id = c.department_id",
     "SELECT c.name, (SELECT d.name FROM departments d WHERE d.id = c.department_id) FROM courses c", True,
     "scalar subquery on a key"),

    # --- aggregation ---------------------------------------------------------------------------------
    (CAMPUS, "SELECT COUNT(*) FROM students", "SELECT COUNT(id) FROM students", True, "COUNT(pk)"),
    (CAMPUS, "SELECT COUNT(*) FROM students", "SELECT COUNT(email) FROM students", False, "COUNT(nullable)"),
    (CAMPUS, "SELECT s.id, COUNT(*) FROM students s LEFT JOIN enrollments e ON e.student_id = s.id GROUP BY s.id",
     "SELECT s.id, COUNT(e.course_id) FROM students s LEFT JOIN enrollments e ON e.student_id = s.id GROUP BY s.id",
     False, "COUNT(*) counts the NULL-extended row"),
    (CAMPUS, "SELECT c.code, COUNT(e.student_id) FROM courses c LEFT JOIN enrollments e ON e.course_id = c.id GROUP BY c.id, c.code",
     "SELECT c.code, (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id) FROM courses c", True,
     "correlated count"),
    (CAMPUS, "SELECT c.credits, COUNT(e.student_id) FROM courses c LEFT JOIN enrollments e ON e.course_id = c.id GROUP BY c.id, c.credits",
     "SELECT DISTINCT c.credits, (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id) FROM courses c", False,
     "correlated count under DISTINCT"),
    (CAMPUS, "SELECT c.name FROM courses c JOIN course_instructors ci ON ci.course_id = c.id GROUP BY c.id, c.name HAVING COUNT(*) > 1",
     "SELECT c.name FROM courses c WHERE (SELECT COUNT(*) FROM course_instructors ci WHERE ci.course_id = c.id) > 1",
     True, "correlated count in WHERE"),
    (CAMPUS, "SELECT c.name FROM courses c JOIN course_instructors ci ON ci.course_id = c.id GROUP BY c.id, c.name HAVING COUNT(*) >= 0",
     "SELECT c.name FROM courses c WHERE (SELECT COUNT(*) FROM course_instructors ci WHERE ci.course_id = c.id) >= 0",
     False, "count >= 0 keeps courses without instructors"),
    (CAMPUS, "SELECT d.name, COUNT(*) FROM departments d JOIN courses c ON c.department_id = d.id GROUP BY d.id, d.name",
     "SELECT d.name, COUNT(*) FROM departments d JOIN courses c ON c.department_id = d.id GROUP BY d.name", True,
     "group by a UNIQUE NOT NULL column"),
    (CAMPUS, "SELECT s.name, COUNT(*) FROM students s JOIN enrollments e ON e.student_id = s.id GROUP BY s.id, s.name",
     "SELECT s.name, COUNT(*) FROM students s JOIN enrollments e ON e.student_id = s.id GROUP BY s.name", False,
     "group by a non-unique column"),
    (CAMPUS, "SELECT id, name FROM students", "SELECT DISTINCT id, name FROM students", True, "DISTINCT over a key"),
    (CAMPUS, "SELECT name FROM students", "SELECT DISTINCT name FROM students", False, "DISTINCT over a non-key"),
    (CAMPUS, "SELECT AVG(salary) FROM instructors WHERE salary IS NOT NULL", "SELECT AVG(salary) FROM instructors",
     True, "AVG ignores NULLs"),
    (CAMPUS, "SELECT department_id, AVG(salary) FROM instructors WHERE salary IS NOT NULL GROUP BY department_id",
     "SELECT department_id, AVG(salary) FROM instructors GROUP BY department_id", False,
     "grouped: all-NULL groups would appear"),

    # --- self-joins and set operations ----------------------------------------------------------------
    (ORG, '''SELECT e."Name", m."Name" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId"''',
     '''SELECT a."Name", b."Name" FROM "Employee" b JOIN "Employee" a ON b."Id" = a."ManagerId"''', True,
     "self-join with swapped aliases"),
    (ORG, '''SELECT e."Name", m."Name" FROM "Employee" e JOIN "Employee" m ON m."Id" = e."ManagerId"''',
     '''SELECT e."Name", m."Name" FROM "Employee" e JOIN "Employee" m ON e."Id" = m."ManagerId"''', False,
     "self-join direction"),
    (CAMPUS, "SELECT id FROM students UNION SELECT id FROM instructors",
     "SELECT id FROM instructors UNION SELECT id FROM students", True, "UNION is commutative"),
    (CAMPUS, "SELECT id FROM students EXCEPT SELECT id FROM instructors",
     "SELECT id FROM instructors EXCEPT SELECT id FROM students", False, "EXCEPT is not"),
    (CAMPUS, "SELECT id FROM students UNION SELECT id FROM instructors",
     "SELECT id FROM students UNION ALL SELECT id FROM instructors", False, "UNION vs UNION ALL"),
    # --- adversarial: rules must not fire on look-alikes ------------------------------------------------
    (CAMPUS, "SELECT COUNT(*) OVER () FROM students", "SELECT DISTINCT COUNT(*) OVER () FROM students", False,
     "DISTINCT over a window function is not a single aggregate row"),
    (CAMPUS, "SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.id = s.department_id)",
     "SELECT s.name FROM students s WHERE s.department_id NOT IN (SELECT id FROM departments)", False,
     "NOT IN with a nullable outer column"),
    (CAMPUS, "SELECT s.name FROM students s WHERE s.department_id IS NULL",
     "SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.id = s.department_id)",
     True, "NOT EXISTS parent row via FK is FK IS NULL"),
    (CAMPUS, "SELECT s.name FROM students s WHERE s.department_id IS NULL",
     "SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.id = s.department_id AND d.budget > 0)",
     False, "extra condition breaks the FK shortcut"),
    (CAMPUS, "SELECT c.name FROM courses c JOIN course_instructors ci ON ci.course_id = c.id GROUP BY c.id, c.name HAVING COUNT(*) > 1",
     "SELECT c.name FROM courses c LEFT JOIN course_instructors ci ON ci.course_id = c.id GROUP BY c.id, c.name HAVING COUNT(ci.instructor_id) > 1 OR c.credits > 3",
     False, "HAVING disjunction keeps NULL-extended groups"),
    (CAMPUS, "SELECT id FROM students s WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.id = s.department_id) OR is_active = 1",
     "SELECT id FROM students WHERE department_id NOT IN (SELECT id FROM departments) OR is_active = 1",
     False, "NOT IN inside OR with a nullable column"),
    (CAMPUS, "SELECT enrollment_year / 2 FROM students", "SELECT enrollment_year / 2.0 FROM students", False,
     "integer vs numeric division"),
    (SOCIAL, '''SELECT u."userName" FROM "users" u WHERE u."privateProfile" = 'on\'''',
     '''SELECT u."userName" FROM "users" u WHERE u."privateProfile" = 'ON\'''', False, "case-sensitive literal"),

    # --- grouping sets -------------------------------------------------------------------------------
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)",
     "SELECT department_id, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id), ())", True,
     "ROLLUP is its prefix grouping sets"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)",
     "SELECT department_id, COUNT(*) FROM students GROUP BY department_id", False, "ROLLUP adds a total row"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id",
     "SELECT department_id, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id))", True,
     "one grouping set is a plain GROUP BY"),
    (CAMPUS, "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY CUBE (department_id, enrollment_year)",
     "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY GROUPING SETS "
     "((enrollment_year, department_id), (enrollment_year), (department_id), ())", True, "CUBE is every subset"),
    (CAMPUS, "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY ROLLUP (department_id, enrollment_year)",
     "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY ROLLUP (enrollment_year, department_id)",
     False, "ROLLUP column order picks the subtotals"),
    (CAMPUS, "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY department_id, ROLLUP (enrollment_year)",
     "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY GROUPING SETS "
     "((department_id, enrollment_year), (department_id))", True, "plain key crossed with ROLLUP"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id), ())",
     "SELECT department_id, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id), (), ())", False,
     "a repeated grouping set repeats its rows"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id), ())",
     "SELECT department_id, COUNT(*) FROM students GROUP BY DISTINCT GROUPING SETS ((department_id), (), ())", True,
     "GROUP BY DISTINCT drops repeated sets"),
    (CAMPUS, "SELECT DISTINCT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)", False,
     "DISTINCT is not redundant under ROLLUP: the total row can equal a NULL-key group"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (1)", True, "ordinal inside ROLLUP"),
    (CAMPUS, "SELECT id, name FROM students GROUP BY id", "SELECT id, name FROM students GROUP BY ROLLUP (id)", False,
     "primary key only licenses ungrouped columns when grouped in every set"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM students",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)", True,
     "UNION ALL grand-total row is the () grouping set"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM students",
     "SELECT NULL, COUNT(*) FROM students UNION ALL SELECT department_id, COUNT(*) FROM students GROUP BY department_id",
     True, "grand-total branch first"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM students",
     "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION SELECT NULL, COUNT(*) FROM students",
     False, "UNION can merge the total row with a NULL-key group"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM students WHERE is_active = 1",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)", False,
     "total over a different filter is not the grand total"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(department_id) FROM students",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id)", False,
     "total with a different aggregate"),
    (CAMPUS, "SELECT department_id FROM students GROUP BY department_id UNION ALL SELECT NULL FROM students",
     "SELECT department_id FROM students GROUP BY ROLLUP (department_id)", False,
     "without an aggregate the second branch returns one row per student"),
    (CAMPUS, "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY department_id, enrollment_year "
     "UNION ALL SELECT department_id, NULL, COUNT(*) FROM students GROUP BY department_id",
     "SELECT department_id, enrollment_year, COUNT(*) FROM students GROUP BY GROUPING SETS ((department_id, enrollment_year), ())",
     False, "a subtotal is not the grand total"),
    (CAMPUS, "SELECT NULL AS d, COUNT(*) AS n FROM students UNION ALL "
     "SELECT department_id, COUNT(*) FROM students GROUP BY department_id ORDER BY 2 DESC LIMIT 2",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id) ORDER BY COUNT(*) DESC LIMIT 2",
     True, "the UNION's ORDER BY and LIMIT carry over to the grouping-sets query"),
    (CAMPUS, "SELECT department_id, COUNT(*) AS n FROM students GROUP BY department_id UNION ALL "
     "SELECT NULL, COUNT(*) FROM students ORDER BY n DESC LIMIT 2",
     "SELECT department_id, COUNT(*) FROM students GROUP BY ROLLUP (department_id) ORDER BY 2 DESC LIMIT 3",
     False, "a different LIMIT after the rewrite"),

    # --- UNION of provably distinct rows -------------------------------------------------------------
    (CAMPUS, "SELECT department_id, COUNT(*) FROM instructors GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM instructors",
     "SELECT department_id, COUNT(*) FROM instructors GROUP BY department_id UNION SELECT NULL, COUNT(*) FROM instructors",
     True, "NOT NULL group key: the NULL total row cannot equal a group row"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION ALL SELECT NULL, COUNT(*) FROM students",
     "SELECT department_id, COUNT(*) FROM students GROUP BY department_id UNION SELECT NULL, COUNT(*) FROM students",
     False, "nullable group key: the NULL group can equal the total row"),
    (CAMPUS, "SELECT department_id, COUNT(*) FROM students WHERE department_id IS NOT NULL GROUP BY department_id "
     "UNION ALL SELECT NULL, COUNT(*) FROM students",
     "SELECT department_id, COUNT(*) FROM students WHERE department_id IS NOT NULL GROUP BY department_id "
     "UNION SELECT NULL, COUNT(*) FROM students", True, "WHERE makes the group key non-NULL"),
    (CAMPUS, "SELECT code FROM courses WHERE is_online = 1 UNION SELECT code FROM courses WHERE is_online = 0 AND credits = 4",
     "SELECT code FROM courses WHERE is_online = 1 UNION ALL SELECT code FROM courses WHERE is_online = 0 AND credits = 4",
     True, "contradictory filters on one table, key in the output"),
    (CAMPUS, "SELECT code FROM courses WHERE is_online = 1 UNION SELECT code FROM courses WHERE credits = 4",
     "SELECT code FROM courses WHERE is_online = 1 UNION ALL SELECT code FROM courses WHERE credits = 4",
     False, "overlapping filters"),
    (CAMPUS, "SELECT name FROM courses WHERE is_online = 1 UNION SELECT name FROM courses WHERE is_online = 0",
     "SELECT name FROM courses WHERE is_online = 1 UNION ALL SELECT name FROM courses WHERE is_online = 0",
     False, "contradictory filters but a non-key output"),
    (CAMPUS, "SELECT 'student', id FROM students UNION SELECT 'instructor', id FROM instructors",
     "SELECT 'student', id FROM students UNION ALL SELECT 'instructor', id FROM instructors",
     True, "different constant tags with keys in the output"),
    (CAMPUS, "SELECT 'student', name FROM students UNION SELECT 'instructor', name FROM instructors",
     "SELECT 'student', name FROM students UNION ALL SELECT 'instructor', name FROM instructors",
     False, "different tags, but a branch can repeat a name"),
    (CAMPUS, "SELECT code FROM courses WHERE is_online = 1 UNION SELECT code FROM courses WHERE is_online = 1.0",
     "SELECT code FROM courses WHERE is_online = 1 UNION ALL SELECT code FROM courses WHERE is_online = 1.0",
     False, "1 and 1.0 are the same value"),

    # --- recursive CTEs over a single-parent hierarchy ----------------------------------------------
    (PROJECT, "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE parent_task_id = 100 UNION ALL "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE parent_task_id = 100 UNION "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     True, "one parent per row and a seed with one parent: every row is reached once"),
    (ORG, '''WITH RECURSIVE c AS (SELECT "Id", 0 AS depth FROM "Employee" WHERE "ManagerId" IS NULL UNION ALL '''
     '''SELECT e."Id", c.depth + 1 FROM "Employee" e JOIN c ON e."ManagerId" = c."Id") SELECT "Id", depth FROM c''',
     '''WITH RECURSIVE c AS (SELECT "Id", 0 AS depth FROM "Employee" WHERE "ManagerId" IS NULL UNION '''
     '''SELECT e."Id", c.depth + 1 FROM "Employee" e JOIN c ON e."ManagerId" = c."Id") SELECT "Id", depth FROM c''',
     True, "seeded with the roots"),
    (PROJECT, "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE priority = 1 UNION ALL "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE priority = 1 UNION "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     False, "seed is not closed: a seed task under another seed task is reached twice"),
    (PROJECT, "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE parent_task_id IN (100, 200) UNION ALL "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     "WITH RECURSIVE tree AS (SELECT id FROM tasks WHERE parent_task_id IN (100, 200) UNION "
     "SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id) SELECT id FROM tree",
     False, "two seed parents: one may descend from the other"),
    (ORG, '''WITH RECURSIVE r AS (SELECT e."Id" FROM "Employee" e JOIN "Employee" b ON b."Id" = e."ManagerId" '''
     '''WHERE b."Name" = 'Dana' UNION ALL SELECT e."Id" FROM "Employee" e JOIN r ON e."ManagerId" = r."Id") SELECT "Id" FROM r''',
     '''WITH RECURSIVE r AS (SELECT e."Id" FROM "Employee" e JOIN "Employee" b ON b."Id" = e."ManagerId" '''
     '''WHERE b."Name" = 'Dana' UNION SELECT e."Id" FROM "Employee" e JOIN r ON e."ManagerId" = r."Id") SELECT "Id" FROM r''',
     False, "seed parent picked by a non-unique name"),
    (PROJECT, "WITH RECURSIVE dep AS (SELECT depends_on_task_id AS id FROM task_dependencies WHERE task_id = 1 UNION ALL "
     "SELECT d.depends_on_task_id FROM task_dependencies d JOIN dep ON d.task_id = dep.id) SELECT id FROM dep",
     "WITH RECURSIVE dep AS (SELECT depends_on_task_id AS id FROM task_dependencies WHERE task_id = 1 UNION "
     "SELECT d.depends_on_task_id FROM task_dependencies d JOIN dep ON d.task_id = dep.id) SELECT id FROM dep",
     False, "a dependency graph has several paths to a task"),

    # --- no-op row limits ----------------------------------------------------------------------------
    (CAMPUS, "SELECT COUNT(*) FROM students", "SELECT COUNT(*) FROM students LIMIT 1", True,
     "LIMIT 1 on an ungrouped aggregate"),
    (CAMPUS, "SELECT COUNT(*) FROM students GROUP BY department_id",
     "SELECT COUNT(*) FROM students GROUP BY department_id LIMIT 1", False, "LIMIT on a grouped query"),
    (CAMPUS, "SELECT COUNT(*) FROM students", "SELECT COUNT(*) FROM students OFFSET 1", False, "OFFSET 1 drops the row"),
    (CAMPUS, "SELECT COUNT(*) FROM students", "SELECT COUNT(*) FROM students LIMIT 0", False, "LIMIT 0"),
    (CAMPUS, "SELECT id FROM students", "SELECT id FROM students OFFSET 0", True, "OFFSET 0"),

    # --- small identities --------------------------------------------------------------------------------
    (CAMPUS, "SELECT name FROM courses WHERE id IN (SELECT course_id FROM enrollments)",
     "SELECT name FROM courses WHERE id = ANY (SELECT course_id FROM enrollments)", True, "= ANY is IN"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT name FROM departments WHERE id <> ALL (SELECT department_id FROM students WHERE department_id IS NOT NULL)",
     True, "<> ALL over non-NULL values is NOT EXISTS"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT name FROM departments WHERE id <> ALL (SELECT department_id FROM students)",
     False, "<> ALL over a nullable column"),
    (CAMPUS, "SELECT id FROM courses WHERE credits = 3", "SELECT id FROM courses WHERE credits IN (3, NULL)", True,
     "NULL in an IN list in WHERE"),
    (CAMPUS, "SELECT id FROM courses WHERE NOT (credits IN (3))", "SELECT id FROM courses WHERE NOT (credits IN (3, NULL))",
     False, "NULL in an IN list under NOT"),
    (CAMPUS, "SELECT COALESCE(email, name) FROM students", "SELECT COALESCE(email, NULL, name) FROM students", True,
     "NULL argument to COALESCE"),
    (CAMPUS, "SELECT COALESCE(email, name) FROM students", "SELECT COALESCE(name, email) FROM students", False,
     "COALESCE argument order"),
    (ORG, '''SELECT "Name" FROM "Employee" WHERE EXTRACT(YEAR FROM "HireDate") = EXTRACT(YEAR FROM "TerminationDate")''',
     '''SELECT "Name" FROM "Employee" WHERE date_trunc('year', "HireDate") = date_trunc('year', "TerminationDate")''',
     True, "same year via date_trunc"),
    (ORG, '''SELECT "Name" FROM "Employee" WHERE EXTRACT(YEAR FROM "HireDate") = EXTRACT(YEAR FROM "TerminationDate")''',
     '''SELECT "Name" FROM "Employee" WHERE date_trunc('month', "HireDate") = date_trunc('month', "TerminationDate")''',
     False, "same month is stricter than same year"),
    (CAMPUS, "SELECT name FROM courses ORDER BY credits DESC", "SELECT name FROM courses ORDER BY credits DESC NULLS LAST",
     True, "NULLS LAST on a NOT NULL sort key"),
    (SOCIAL, '''SELECT p."id", u."userName" FROM "posts" p LEFT JOIN "posts" q ON q."id" = p."replyToPostId" '''
     '''LEFT JOIN "users" u ON u."id" = q."userId"''',
     '''SELECT p."id", (SELECT u."userName" FROM "posts" q JOIN "users" u ON u."id" = q."userId" '''
     '''WHERE q."id" = p."replyToPostId") FROM "posts" p''', True, "scalar subquery over a key chain"),
    (SOCIAL, '''SELECT p."id", q."body" FROM "posts" p LEFT JOIN "posts" q ON q."id" = p."replyToPostId"''',
     '''SELECT p."id", (SELECT q."body" FROM "posts" q JOIN "users" u ON u."id" = q."userId" '''
     '''WHERE q."id" = p."replyToPostId" AND u."privateProfile" = 'on') FROM "posts" p''',
     False, "scalar chain whose value is not from the last table"),
    (CAMPUS, "SELECT AVG(credits) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM courses",
     "SELECT AVG(credits) OVER (ORDER BY id ROWS 2 PRECEDING) FROM courses", True, "frame start alone ends at the current row"),
    (CAMPUS, "SELECT AVG(credits) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND 1 FOLLOWING) FROM courses",
     "SELECT AVG(credits) OVER (ORDER BY id ROWS 2 PRECEDING) FROM courses", False, "a different frame end"),

    # --- counting forms ------------------------------------------------------------------------------
    (CAMPUS, "SELECT COUNT(*) FROM students WHERE email IS NULL", "SELECT COUNT(*) - COUNT(email) FROM students", True,
     "rows minus non-NULL values"),
    (CAMPUS, "SELECT COUNT(email) FROM students", "SELECT COUNT(*) FILTER (WHERE email IS NOT NULL) FROM students", True,
     "COUNT(col) as a filtered COUNT(*)"),
    (CAMPUS, "SELECT COUNT(email) FROM students", "SELECT COUNT(*) FROM students WHERE email IS NOT NULL", True,
     "ungrouped: the filter can move to WHERE"),
    (CAMPUS, "SELECT department_id, COUNT(email) FROM students GROUP BY department_id",
     "SELECT department_id, COUNT(*) FROM students WHERE email IS NOT NULL GROUP BY department_id", False,
     "grouped: WHERE drops groups whose emails are all NULL"),
    (CAMPUS, "SELECT COUNT(email), MAX(enrollment_year) FROM students",
     "SELECT COUNT(*), MAX(enrollment_year) FROM students WHERE email IS NOT NULL", False,
     "the filter would also change the other aggregate"),

    # --- set operations, DISTINCT and semi-joins ------------------------------------------------------
    (CAMPUS, "SELECT name FROM students UNION SELECT name FROM instructors",
     "SELECT DISTINCT name FROM (SELECT name FROM students UNION ALL SELECT name FROM instructors) t", True,
     "DISTINCT over UNION ALL"),
    (CAMPUS, "SELECT name FROM students UNION SELECT name FROM instructors",
     "SELECT name FROM (SELECT name FROM students UNION ALL SELECT name FROM instructors) t", False,
     "no DISTINCT over UNION ALL"),
    (CAMPUS, "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id UNION SELECT name FROM instructors",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id) "
     "UNION SELECT name FROM instructors", True, "join and semi-join agree under UNION"),
    (CAMPUS, "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id UNION ALL SELECT name FROM instructors",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id) "
     "UNION ALL SELECT name FROM instructors", False, "join and semi-join differ under UNION ALL"),
    (ORG, '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship") AND "Id" IN (SELECT "MenteeId" FROM "Mentorship")''',
     '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship" INTERSECT SELECT "MenteeId" FROM "Mentorship")''',
     True, "IN over INTERSECT"),
    (ORG, '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship") OR "Id" IN (SELECT "MenteeId" FROM "Mentorship")''',
     '''SELECT "Name" FROM "Employee" WHERE "Id" IN (SELECT "MentorId" FROM "Mentorship" INTERSECT SELECT "MenteeId" FROM "Mentorship")''',
     False, "INTERSECT is not OR"),
    (CAMPUS, "SELECT course_id FROM enrollments EXCEPT SELECT course_id FROM course_prerequisites",
     "SELECT DISTINCT e.course_id FROM enrollments e WHERE NOT EXISTS "
     "(SELECT 1 FROM course_prerequisites cp WHERE cp.course_id = e.course_id)", True, "EXCEPT as DISTINCT + NOT EXISTS"),
    (CAMPUS, "SELECT course_id FROM enrollments EXCEPT SELECT course_id FROM course_prerequisites",
     "SELECT e.course_id FROM enrollments e WHERE NOT EXISTS "
     "(SELECT 1 FROM course_prerequisites cp WHERE cp.course_id = e.course_id)", False, "EXCEPT removes duplicates"),
    (CAMPUS, "SELECT course_id FROM enrollments EXCEPT ALL SELECT course_id FROM course_prerequisites",
     "SELECT DISTINCT e.course_id FROM enrollments e WHERE NOT EXISTS "
     "(SELECT 1 FROM course_prerequisites cp WHERE cp.course_id = e.course_id)", False, "EXCEPT ALL keeps multiplicities"),
    (CAMPUS, "SELECT department_id FROM students EXCEPT SELECT department_id FROM students WHERE is_active = 1",
     "SELECT DISTINCT s.department_id FROM students s WHERE NOT EXISTS "
     "(SELECT 1 FROM students s2 WHERE s2.is_active = 1 AND s2.department_id = s.department_id)", False,
     "EXCEPT matches NULL with NULL, = does not"),
    (SOCIAL, '''SELECT f."followeeId" FROM "follows" f JOIN "users" u ON u."id" = f."followerId" WHERE u."userName" = 'alice\'''',
     '''SELECT DISTINCT f."followeeId" FROM "follows" f JOIN "users" u ON u."id" = f."followerId" WHERE u."userName" = 'alice\'''',
     True, "DISTINCT is redundant: the output and a unique name fix both rows"),
    (SOCIAL, '''SELECT f."followeeId" FROM "follows" f JOIN "users" u ON u."id" = f."followerId" WHERE u."displayName" = 'Alice\'''',
     '''SELECT DISTINCT f."followeeId" FROM "follows" f JOIN "users" u ON u."id" = f."followerId" WHERE u."displayName" = 'Alice\'''',
     False, "a non-unique display name can match several followers"),
    (CAMPUS, "SELECT department_id FROM instructors UNION SELECT department_id FROM courses",
     "SELECT d.id FROM departments d WHERE EXISTS (SELECT 1 FROM instructors i WHERE i.department_id = d.id) "
     "OR d.id IN (SELECT department_id FROM courses)", True, "a UNION of foreign keys is a semi-join on the parent"),
    (CAMPUS, "SELECT department_id FROM instructors UNION ALL SELECT department_id FROM courses",
     "SELECT d.id FROM departments d WHERE EXISTS (SELECT 1 FROM instructors i WHERE i.department_id = d.id) "
     "OR EXISTS (SELECT 1 FROM courses c WHERE c.department_id = d.id)", False, "UNION ALL keeps duplicates"),
    (CAMPUS, "SELECT department_id FROM instructors UNION SELECT id FROM courses",
     "SELECT d.id FROM departments d WHERE EXISTS (SELECT 1 FROM instructors i WHERE i.department_id = d.id) "
     "OR EXISTS (SELECT 1 FROM courses c WHERE c.id = d.id)", False, "branches name rows of different tables"),
    (CAMPUS, "SELECT DISTINCT department_id FROM students",
     "SELECT d.id FROM departments d WHERE EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)", False,
     "DISTINCT keeps one NULL for undeclared students"),
    (CAMPUS, "SELECT DISTINCT department_id FROM students WHERE department_id IS NOT NULL",
     "SELECT d.id FROM departments d WHERE EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)", True,
     "distinct non-NULL foreign keys"),
    (CAMPUS, "SELECT COUNT(DISTINCT department_id) FROM students",
     "SELECT COUNT(*) FROM departments d WHERE EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)", True,
     "COUNT(DISTINCT fk) skips NULLs like the semi-join"),
    (PROJECT, "SELECT task_id FROM task_dependencies UNION SELECT depends_on_task_id FROM task_dependencies",
     "SELECT t.id FROM tasks t WHERE EXISTS (SELECT 1 FROM task_dependencies d WHERE d.task_id = t.id OR d.depends_on_task_id = t.id)",
     True, "EXISTS distributes over a correlated OR"),

    # --- grouped counts as semi-joins ----------------------------------------------------------------
    (CAMPUS, "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'CS101') AND EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'MATH201')",
     "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id JOIN courses c ON c.id = e.course_id "
     "WHERE c.code IN ('CS101', 'MATH201') GROUP BY s.id, s.name HAVING COUNT(DISTINCT c.code) = 2", True,
     "all n listed codes present"),
    (CAMPUS, "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'CS101') AND EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'MATH201')",
     "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id JOIN courses c ON c.id = e.course_id "
     "WHERE c.code IN ('CS101', 'MATH201', 'PHYS101') GROUP BY s.id, s.name HAVING COUNT(DISTINCT c.code) = 2", False,
     "two of three codes"),
    (CAMPUS, "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'CS101') AND EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id "
     "WHERE e.student_id = s.id AND c.code = 'MATH201')",
     "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id JOIN courses c ON c.id = e.course_id "
     "WHERE c.code IN ('CS101', 'MATH201') GROUP BY s.name HAVING COUNT(DISTINCT c.code) = 2", False,
     "grouping by name merges students"),
    (CAMPUS, "SELECT c.name FROM courses c JOIN course_instructors ci ON ci.course_id = c.id GROUP BY c.id, c.name HAVING COUNT(*) > 1",
     "SELECT c.name FROM courses c WHERE EXISTS (SELECT 1 FROM course_instructors x JOIN course_instructors y "
     "ON x.course_id = y.course_id AND x.instructor_id <> y.instructor_id WHERE x.course_id = c.id)", True,
     "more than one child row: two that differ in the rest of the key"),
    (CAMPUS, "SELECT s.name FROM students s JOIN enrollments e ON e.student_id = s.id GROUP BY s.id, s.name HAVING COUNT(*) > 1",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments x JOIN enrollments y "
     "ON x.student_id = y.student_id AND x.course_id <> y.course_id WHERE x.student_id = s.id)", False,
     "a three-column key: two enrollments can share the course"),

    # --- pre-aggregation -----------------------------------------------------------------------------
    (CAMPUS, "SELECT c.name, COUNT(*) FROM courses c JOIN instructors i ON i.department_id = c.department_id GROUP BY c.id, c.name",
     "SELECT c.name, t.n FROM courses c JOIN (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) t "
     "ON t.department_id = c.department_id", True, "pre-aggregated per department, joined to many courses"),
    (CAMPUS, "SELECT c.name, COUNT(*) FROM courses c JOIN instructors i ON i.department_id = c.department_id GROUP BY c.department_id, c.name",
     "SELECT c.name, t.n FROM courses c JOIN (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) t "
     "ON t.department_id = c.department_id", False, "grouping by department merges courses"),
    (CAMPUS, "SELECT d.name, COUNT(*) FROM departments d JOIN instructors i ON i.department_id = d.id GROUP BY d.id, d.name",
     "SELECT d.name, t.n FROM departments d LEFT JOIN (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) t "
     "ON t.department_id = d.id", False, "LEFT JOIN keeps departments without instructors"),
    (PROJECT, "SELECT tm.name, (SELECT COUNT(*) FROM projects p WHERE p.team_id = tm.id), "
     "(SELECT COUNT(*) FROM users u WHERE u.team_id = tm.id) FROM teams tm",
     "SELECT tm.name, COUNT(DISTINCT p.id), COUNT(DISTINCT u.id) FROM teams tm LEFT JOIN projects p ON p.team_id = tm.id "
     "LEFT JOIN users u ON u.team_id = tm.id GROUP BY tm.id, tm.name", True, "two correlated counts as distinct keys"),
    (PROJECT, "SELECT tm.name, (SELECT COUNT(*) FROM projects p WHERE p.team_id = tm.id), "
     "(SELECT COUNT(*) FROM users u WHERE u.team_id = tm.id) FROM teams tm",
     "SELECT tm.name, COUNT(p.id), COUNT(u.id) FROM teams tm LEFT JOIN projects p ON p.team_id = tm.id "
     "LEFT JOIN users u ON u.team_id = tm.id GROUP BY tm.id, tm.name", False, "the two joins multiply plain counts"),
    (CAMPUS, "SELECT a.department_id FROM (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) a "
     "JOIN (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) b ON b.n = a.n AND b.department_id <> a.department_id",
     "WITH t AS (SELECT department_id, COUNT(*) AS n FROM instructors GROUP BY department_id) "
     "SELECT a.department_id FROM t a JOIN t b ON b.n = a.n AND b.department_id <> a.department_id", True,
     "a deterministic CTE read twice"),
    (CAMPUS, "SELECT a.id FROM (SELECT id, random() AS r FROM students) a JOIN (SELECT id, random() AS r FROM students) b "
     "ON b.id = a.id WHERE b.r = a.r",
     "WITH t AS (SELECT id, random() AS r FROM students) SELECT a.id FROM t a JOIN t b ON b.id = a.id WHERE b.r = a.r",
     False, "a CTE calling random() is read once"),
    (CAMPUS, "SELECT t.name FROM (SELECT name FROM courses) t", "SELECT t.name FROM (SELECT name, credits FROM courses) t",
     True, "an unread derived column"),
    (CAMPUS, "SELECT t.name FROM (SELECT DISTINCT name FROM courses) t",
     "SELECT t.name FROM (SELECT DISTINCT name, credits FROM courses) t", False, "an unread column under DISTINCT"),

    # --- aggregate arithmetic ------------------------------------------------------------------------
    (CAMPUS, "SELECT SUM(credits + capacity) FROM courses WHERE capacity IS NOT NULL",
     "SELECT SUM(credits) + SUM(capacity) FROM courses WHERE capacity IS NOT NULL", True, "SUM is linear over non-NULL terms"),
    (CAMPUS, "SELECT SUM(credits + capacity) FROM courses", "SELECT SUM(credits) + SUM(capacity) FROM courses", False,
     "a NULL capacity drops the whole row from the left sum only"),
    (CAMPUS, "SELECT department_id, SUM(COALESCE(salary, 0)) FROM instructors GROUP BY department_id",
     "SELECT department_id, COALESCE(SUM(salary), 0) FROM instructors GROUP BY department_id", True,
     "groups are never empty"),
    (CAMPUS, "SELECT SUM(COALESCE(salary, 0)) FROM instructors", "SELECT COALESCE(SUM(salary), 0) FROM instructors", False,
     "on an empty table one is NULL and the other 0"),
    (CAMPUS, "SELECT department_id, SUM(CASE WHEN is_tenured = 1 THEN 1 ELSE 0 END) FROM instructors GROUP BY department_id",
     "SELECT department_id, COUNT(*) FILTER (WHERE is_tenured = 1) FROM instructors GROUP BY department_id", True,
     "CASE sum as a filtered count, grouped"),
    (CAMPUS, "SELECT SUM(CASE WHEN is_tenured = 1 THEN 1 ELSE 0 END) FROM instructors",
     "SELECT COUNT(*) FILTER (WHERE is_tenured = 1) FROM instructors", False, "ungrouped: NULL vs 0 on an empty table"),
    (CAMPUS, "SELECT SUM(salary) FILTER (WHERE is_tenured = 1) FROM instructors",
     "SELECT SUM(CASE WHEN is_tenured = 1 THEN salary END) FROM instructors", True, "CASE without ELSE is a FILTER"),
    (CAMPUS, "SELECT department_id, COALESCE(SUM(salary) FILTER (WHERE is_tenured = 1), 0) FROM instructors GROUP BY department_id",
     "SELECT department_id, SUM(CASE WHEN is_tenured = 1 THEN salary ELSE 0 END) FROM instructors GROUP BY department_id",
     False, "a group of tenured instructors without salaries: NULL vs 0"),
    (CAMPUS, "SELECT SUM(credits)::numeric / COUNT(DISTINCT department_id) FROM courses",
     "SELECT AVG(t) FROM (SELECT SUM(credits) AS t FROM courses GROUP BY department_id) x", True,
     "average of group totals"),
    (CAMPUS, "SELECT SUM(credits) / COUNT(DISTINCT department_id) FROM courses",
     "SELECT AVG(t) FROM (SELECT SUM(credits) AS t FROM courses GROUP BY department_id) x", False,
     "integer division vs AVG's numeric result"),
    (CAMPUS, "SELECT SUM(enrollment_year)::numeric / COUNT(DISTINCT department_id) FROM students",
     "SELECT AVG(t) FROM (SELECT SUM(enrollment_year) AS t FROM students GROUP BY department_id) x", False,
     "AVG counts the NULL-department group, COUNT(DISTINCT) does not"),
    (CAMPUS, "SELECT SUM(salary) / COUNT(DISTINCT department_id) FROM instructors",
     "SELECT AVG(t) FROM (SELECT SUM(salary) AS t FROM instructors GROUP BY department_id) x", False,
     "AVG skips a department whose salaries are all NULL"),
    (CAMPUS, "SELECT (SELECT COUNT(*) FROM students) / (SELECT COUNT(*) FROM instructors)",
     "SELECT COUNT(*) / (SELECT COUNT(*) FROM instructors) FROM students", True, "aggregate next to a scalar subquery"),
    (CAMPUS, "SELECT (SELECT COUNT(*) FROM students) / (SELECT COUNT(*) FROM instructors)",
     "SELECT COUNT(*) / (SELECT COUNT(*) FROM instructors) FROM students WHERE is_active = 1", False,
     "the aggregate's filter matters"),
    (CAMPUS, "SELECT (SELECT COUNT(*) FROM students) / (SELECT COUNT(*) FROM instructors)",
     "SELECT COUNT(*) / (SELECT COUNT(*) FROM instructors) FROM students GROUP BY department_id", False,
     "a grouped query returns a row per group"),
    (CAMPUS, "SELECT AVG(is_international) FROM students", "SELECT AVG(is_international::numeric) FROM students", True,
     "AVG of an integer column is already numeric"),

    # --- first row per group -------------------------------------------------------------------------
    (ORG, '''SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name" FROM "Employee" ORDER BY "DepartmentId", "Salary" DESC, "Id"''',
     '''SELECT "DepartmentId", "Name" FROM (SELECT "DepartmentId", "Name", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" '''
     '''ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn = 1 ORDER BY "DepartmentId"''', True,
     "ROW_NUMBER() = 1 is DISTINCT ON"),
    (ORG, '''SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name" FROM "Employee" ORDER BY "DepartmentId", "Salary" DESC, "Id"''',
     '''SELECT "DepartmentId", "Name" FROM (SELECT "DepartmentId", "Name", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" '''
     '''ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn = 1''', False,
     "the target is ordered, the prediction is not"),
    (ORG, '''SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name" FROM "Employee" ORDER BY "DepartmentId", "Salary" DESC, "Id"''',
     '''SELECT "DepartmentId", "Name" FROM (SELECT "DepartmentId", "Name", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" '''
     '''ORDER BY "Salary", "Id") AS rn FROM "Employee") t WHERE rn = 1 ORDER BY "DepartmentId"''', False,
     "lowest instead of highest salary"),
    (ORG, '''SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name" FROM "Employee" ORDER BY "DepartmentId", "Salary" DESC, "Id"''',
     '''SELECT "DepartmentId", "Name" FROM (SELECT "DepartmentId", "Name", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" '''
     '''ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn <= 2 ORDER BY "DepartmentId"''', False,
     "top two per group"),
    (ORG, '''SELECT DISTINCT ON ("DepartmentId") "DepartmentId", "Name" FROM "Employee" WHERE "Salary" > 100000 '''
     '''ORDER BY "DepartmentId", "Salary" DESC, "Id"''',
     '''SELECT t."DepartmentId", t."Name" FROM (SELECT "DepartmentId", "Name", "Salary", ROW_NUMBER() OVER (PARTITION BY "DepartmentId" '''
     '''ORDER BY "Salary" DESC, "Id") AS rn FROM "Employee") t WHERE rn = 1 AND t."Salary" > 100000 ORDER BY t."DepartmentId"''',
     False, "filtering after ranking is not filtering before"),
    (ORG, '''SELECT e."Name", sh."Salary" FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" '''
     '''WHERE sh."EffectiveDate" = (SELECT MAX(s2."EffectiveDate") FROM "SalaryHistory" s2 WHERE s2."EmployeeId" = e."Id")''',
     '''SELECT DISTINCT ON (e."Id") e."Name", sh."Salary" FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" '''
     '''ORDER BY e."Id", sh."EffectiveDate" DESC''', True, "latest row by a unique (employee, date) key"),
    (CAMPUS, "SELECT d.name, i.name FROM departments d JOIN instructors i ON i.department_id = d.id "
     "WHERE i.hire_date = (SELECT MAX(i2.hire_date) FROM instructors i2 WHERE i2.department_id = d.id)",
     "SELECT DISTINCT ON (d.id) d.name, i.name FROM departments d JOIN instructors i ON i.department_id = d.id "
     "ORDER BY d.id, i.hire_date DESC", False, "instructors hired the same day all tie for the latest"),
    (ORG, '''SELECT e."Name", sh."Salary" FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" '''
     '''WHERE sh."Salary" > 1000 AND sh."EffectiveDate" = (SELECT MAX(s2."EffectiveDate") FROM "SalaryHistory" s2 WHERE s2."EmployeeId" = e."Id")''',
     '''SELECT DISTINCT ON (e."Id") e."Name", sh."Salary" FROM "Employee" e JOIN "SalaryHistory" sh ON sh."EmployeeId" = e."Id" '''
     '''WHERE sh."Salary" > 1000 ORDER BY e."Id", sh."EffectiveDate" DESC''', False,
     "the latest row overall vs the latest row above 1000"),

    # --- windows -----------------------------------------------------------------------------------
    (CAMPUS, "SELECT id, RANK() OVER (ORDER BY credits) FROM courses",
     "SELECT id, RANK() OVER w FROM courses WINDOW w AS (ORDER BY credits)", True, "named window"),
    (CAMPUS, "SELECT id, RANK() OVER w FROM courses WINDOW w AS (ORDER BY credits)",
     "SELECT id, RANK() OVER w FROM courses WINDOW w AS (ORDER BY capacity)", False, "named windows that differ"),
    (CAMPUS, "SELECT id, LAG(credits) OVER (ORDER BY id) FROM courses",
     "SELECT id, LAG(credits, 1) OVER (ORDER BY id) FROM courses", True, "LAG default offset"),
    (CAMPUS, "SELECT id, LAG(credits) OVER (ORDER BY id) FROM courses",
     "SELECT id, LAG(credits, 1, 0) OVER (ORDER BY id) FROM courses", False, "LAG with a default value"),
    (CAMPUS, "SELECT id, RANK() OVER (ORDER BY credits) FROM courses",
     "SELECT id, RANK() OVER (ORDER BY credits ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM courses", True,
     "ranking ignores the frame"),
    (CAMPUS, "SELECT department_id, SUM(SUM(credits)) OVER (ORDER BY department_id) FROM courses GROUP BY department_id",
     "SELECT department_id, SUM(SUM(credits)) OVER (ORDER BY department_id ROWS UNBOUNDED PRECEDING) FROM courses GROUP BY department_id",
     True, "RANGE = ROWS when the order key is the grouping key"),
    (CAMPUS, "SELECT id, SUM(credits) OVER (ORDER BY department_id) FROM courses",
     "SELECT id, SUM(credits) OVER (ORDER BY department_id ROWS UNBOUNDED PRECEDING) FROM courses", False,
     "RANGE includes peers when the order key repeats"),
    (CAMPUS, "SELECT name, n FROM (SELECT name, credits AS n, ROW_NUMBER() OVER (ORDER BY credits DESC, id) AS rn FROM courses) t "
             "WHERE rn <= 3 ORDER BY n DESC", "SELECT name, credits FROM courses ORDER BY credits DESC, id LIMIT 3", False,
     "ROW_NUMBER filter: the outer order must be the window order"),
    (CAMPUS, "SELECT name, credits FROM courses ORDER BY credits DESC, id LIMIT 3",
     "SELECT name, n FROM (SELECT name, credits AS n, id, ROW_NUMBER() OVER (ORDER BY credits DESC, id) AS rn FROM courses) t "
     "WHERE rn <= 3 ORDER BY n DESC, id", True, "ROW_NUMBER <= n is ORDER BY ... LIMIT n"),
    (CAMPUS, "SELECT name, credits FROM courses ORDER BY credits DESC, id LIMIT 3",
     "SELECT name, n FROM (SELECT name, credits AS n, id, ROW_NUMBER() OVER (PARTITION BY department_id ORDER BY credits DESC, id) AS rn "
     "FROM courses) t WHERE rn <= 3 ORDER BY n DESC, id", False, "a partitioned ROW_NUMBER keeps 3 per partition"),
    (CAMPUS, "SELECT c.code FROM courses c WHERE c.credits > (SELECT AVG(credits) FROM courses)",
     "SELECT t.code FROM (SELECT code, credits, AVG(credits) OVER () AS a FROM courses) t WHERE t.credits > t.a", True,
     "AVG over the whole result is a scalar subquery"),
    (CAMPUS, "SELECT c.code FROM courses c WHERE c.credits > (SELECT AVG(credits) FROM courses)",
     "SELECT t.code FROM (SELECT code, credits, AVG(credits) OVER (PARTITION BY department_id) AS a FROM courses) t WHERE t.credits > t.a",
     False, "a partitioned average is per department"),

    # --- expression identities -------------------------------------------------------------------------
    (CAMPUS, "SELECT id FROM courses WHERE code IN ('A', 'B')", "SELECT id FROM courses WHERE code = ANY (ARRAY['A', 'B'])", True,
     "= ANY (ARRAY)"),
    (CAMPUS, "SELECT id FROM courses WHERE code = 'A'", "SELECT id FROM courses WHERE code = ANY (ARRAY['A', 'B'])", False,
     "= ANY with another element"),
    (CAMPUS, "SELECT id FROM courses WHERE code NOT IN ('A', 'B')", "SELECT id FROM courses WHERE code <> ALL (ARRAY['A', 'B'])", True,
     "<> ALL (ARRAY)"),
    (CAMPUS, "SELECT id FROM courses WHERE credits = 4 AND is_online = 1",
     "SELECT id FROM courses WHERE (credits, is_online) = (4, 1)", True, "row-value ="),
    (CAMPUS, "SELECT id FROM courses WHERE credits = 4", "SELECT id FROM courses WHERE (credits, is_online) = (4, 1)", False,
     "row-value = tests every pair"),
    (CAMPUS, "SELECT id FROM courses WHERE credits <> 4 OR is_online <> 1",
     "SELECT id FROM courses WHERE (credits, is_online) <> (4, 1)", True, "row-value <>"),
    (CAMPUS, "SELECT id FROM courses WHERE code <> 'X'", "SELECT id FROM courses WHERE code IS DISTINCT FROM 'X'", True,
     "IS DISTINCT FROM a never-NULL column"),
    (CAMPUS, "SELECT id FROM students WHERE email <> 'X'", "SELECT id FROM students WHERE email IS DISTINCT FROM 'X'", False,
     "IS DISTINCT FROM keeps NULL emails"),
    (CAMPUS, "SELECT id FROM departments WHERE budget IS NULL OR budget = 0",
     "SELECT id FROM departments WHERE COALESCE(budget, 0) = 0", True, "COALESCE(x, c) = c"),
    (CAMPUS, "SELECT id FROM departments WHERE budget = 0", "SELECT id FROM departments WHERE COALESCE(budget, 0) = 0", False,
     "COALESCE(x, c) = c also keeps NULLs"),
    (CAMPUS, "SELECT length(name) FROM students", "SELECT char_length(name) FROM students", True, "char_length"),
    (CAMPUS, "SELECT length(name) FROM students", "SELECT octet_length(name) FROM students", False, "octet_length counts bytes"),
    (CAMPUS, "SELECT id FROM courses WHERE name LIKE '%intro%'", "SELECT id FROM courses WHERE strpos(name, 'intro') > 0", True,
     "strpos > 0"),
    (CAMPUS, "SELECT id FROM courses WHERE name LIKE '%a_b%'", "SELECT id FROM courses WHERE strpos(name, 'a_b') > 0", False,
     "strpos takes _ literally, LIKE does not"),
    (CAMPUS, "SELECT CASE WHEN credits = 4 THEN 'x' END FROM courses", "SELECT CASE credits WHEN 4 THEN 'x' END FROM courses",
     True, "simple CASE"),
    (ORG, '''SELECT COUNT(*) FROM "Employee" WHERE "Status" = 'Active\'''',
     '''SELECT COUNT(*) FROM "Employee" WHERE "Status"::text ILIKE 'active\'''', True, "enum label by pattern"),
    (ORG, '''SELECT COUNT(*) FROM "Employee" WHERE "Status" = 'Active\'''',
     '''SELECT COUNT(*) FROM "Employee" WHERE "Status"::text LIKE '%e%\'''', False, "pattern matching several labels"),
    (CAMPUS, "SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)",
     "SELECT s.name FROM students s WHERE (SELECT COUNT(*) FROM enrollments e WHERE e.student_id = s.id) = 0", True,
     "correlated COUNT(*) = 0"),
    (CAMPUS, "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)",
     "SELECT s.name FROM students s WHERE (SELECT COUNT(*) FROM enrollments e WHERE e.student_id = s.id) = 1", False,
     "COUNT(*) = 1 is not EXISTS"),
    (CAMPUS, "SELECT d.id FROM departments d JOIN instructors i ON i.department_id = d.id GROUP BY d.id, d.budget "
             "HAVING SUM(i.salary) > d.budget",
     "SELECT d.id FROM departments d JOIN instructors i ON i.department_id = d.id GROUP BY d.id, d.budget "
     "HAVING SUM(i.salary) - d.budget > 0", True, "a - b > 0 on NUMERIC"),
    (CAMPUS, "SELECT id FROM departments WHERE budget > 10000", "SELECT id FROM departments WHERE budget > 1e4", True,
     "numeric literal spelling in a comparison"),
    (CAMPUS, "SELECT credits / 10000 FROM courses", "SELECT credits / 1e4 FROM courses", False,
     "1e4 is NUMERIC: no integer division"),
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date < DATE '2025-01-01'",
     "SELECT name FROM instructors WHERE hire_date < DATE '2024-01-01' + INTERVAL '1 year'", True, "date + interval"),
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date < DATE '2024-02-29'",
     "SELECT name FROM instructors WHERE hire_date < DATE '2024-01-31' + INTERVAL '1 month'", True,
     "date + 1 month clamps to the month's end"),
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date < DATE '2024-03-02'",
     "SELECT name FROM instructors WHERE hire_date < DATE '2024-01-31' + INTERVAL '1 month'", False, "not 30 days"),
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date <= DATE '2025-01-01'",
     "SELECT name FROM instructors WHERE hire_date <= TIMESTAMP '2025-01-01 12:00:00'", True,
     "a DATE <= noon is the same as <= that date"),
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date < DATE '2025-01-01'",
     "SELECT name FROM instructors WHERE hire_date < TIMESTAMP '2025-01-01 12:00:00'", False,
     "a DATE < noon includes that date"),
    (PROJECT, "SELECT id FROM tasks WHERE status IN ('todo', 'in_progress', 'blocked')",
     "SELECT id FROM tasks WHERE status < 'done'", True, "enum declaration order"),
    (PROJECT, "SELECT id FROM tasks WHERE status = 'todo'", "SELECT id FROM tasks WHERE status <= 'in_progress'", False,
     "<= includes the bound"),
    (CAMPUS, "SELECT department_id FROM courses GROUP BY department_id HAVING COUNT(*) > 3",
     "SELECT department_id FROM courses GROUP BY department_id HAVING COUNT(*) >= 4", True, "COUNT is an integer"),
    (CAMPUS, "SELECT department_id FROM courses GROUP BY department_id HAVING AVG(credits) > 3",
     "SELECT department_id FROM courses GROUP BY department_id HAVING AVG(credits) >= 4", False, "AVG is not"),

    # --- aggregates -----------------------------------------------------------------------------------
    (CAMPUS, "SELECT department_id, COUNT(*) AS n FROM courses GROUP BY department_id ORDER BY n DESC",
     "SELECT department_id, COUNT(*) AS n FROM courses GROUP BY department_id ORDER BY n DESC NULLS LAST", True,
     "a count is never NULL"),
    (CAMPUS, "SELECT department_id, SUM(salary) AS s FROM instructors GROUP BY department_id ORDER BY s DESC",
     "SELECT department_id, SUM(salary) AS s FROM instructors GROUP BY department_id ORDER BY s DESC NULLS LAST", False,
     "a SUM of a nullable column can be NULL"),
    (CAMPUS, "SELECT AVG(salary) FROM instructors", "SELECT SUM(salary) / COUNT(salary) FROM instructors", True,
     "AVG = SUM / COUNT (NUMERIC)"),
    (CAMPUS, "SELECT AVG(salary) FROM instructors", "SELECT SUM(salary) / COUNT(*) FROM instructors", False,
     "COUNT(*) also counts NULL salaries"),
    (CAMPUS, "SELECT AVG(credits) FROM courses", "SELECT SUM(credits)::numeric / COUNT(*) FROM courses", True,
     "AVG of integers = NUMERIC sum / count"),
    (CAMPUS, "SELECT AVG(credits) FROM courses", "SELECT SUM(credits) / COUNT(*) FROM courses", False, "integer division"),
    (CAMPUS, "SELECT 100.0 * SUM(is_international) / COUNT(*) FROM students",
     "SELECT 100.0 * SUM(is_international) / NULLIF(COUNT(*), 0) FROM students", True,
     "NULLIF(count, 0) under a SUM: no change"),
    (CAMPUS, "SELECT COUNT(email)::numeric / COUNT(*) FROM students",
     "SELECT COUNT(email)::numeric / NULLIF(COUNT(*), 0) FROM students", False,
     "on an empty table the first divides by zero"),
    (CAMPUS, "SELECT COUNT(*) FILTER (WHERE email IS NULL) - COUNT(*) FILTER (WHERE email IS NOT NULL) FROM students",
     "SELECT SUM(CASE WHEN email IS NULL THEN 1 ELSE -1 END) FROM students", False, "on an empty table SUM is NULL"),
    (CAMPUS, "SELECT department_id FROM students GROUP BY department_id "
             "HAVING COUNT(*) FILTER (WHERE email IS NULL) > COUNT(*) FILTER (WHERE email IS NOT NULL)",
     "SELECT department_id FROM students GROUP BY department_id HAVING SUM(CASE WHEN email IS NULL THEN 1 ELSE -1 END) > 0", True,
     "SUM of +1 / -1 per row in non-empty groups"),

    # --- truth contexts, grouping, joins -------------------------------------------------------------------
    (CAMPUS, "SELECT id FROM students WHERE email IS NOT NULL", "SELECT id FROM students WHERE email LIKE '%'", True,
     "LIKE '%' in WHERE"),
    (CAMPUS, "SELECT email IS NOT NULL FROM students", "SELECT email LIKE '%' FROM students", False,
     "LIKE '%' is NULL, not FALSE, for NULL"),
    (CAMPUS, "SELECT DISTINCT department_id FROM students", "SELECT department_id FROM students GROUP BY department_id", True,
     "GROUP BY of the outputs is DISTINCT"),
    (CAMPUS, "SELECT DISTINCT department_id FROM students", "SELECT department_id FROM students GROUP BY department_id, id", False,
     "GROUP BY an extra key keeps duplicates"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students GROUP BY id", True, "GROUP BY the primary key alone"),
    (CAMPUS, "SELECT c.name, d.name, e.term FROM courses c JOIN departments d ON d.id = c.department_id JOIN enrollments e ON e.course_id = c.id",
     "SELECT c.name, d.name, e.term FROM departments d RIGHT JOIN courses c ON d.id = c.department_id JOIN enrollments e ON e.course_id = c.id",
     True, "RIGHT JOIN followed by a join"),
    (CAMPUS, "SELECT s.name, d.name FROM students s JOIN departments d ON d.id = s.department_id JOIN enrollments e ON e.student_id = s.id",
     "SELECT s.name, d.name FROM departments d RIGHT JOIN students s ON d.id = s.department_id JOIN enrollments e ON e.student_id = s.id",
     False, "RIGHT JOIN keeps undeclared students"),

    # --- CASE ------------------------------------------------------------------------------------------
    (CAMPUS, "SELECT CASE WHEN capacity IS NULL THEN 'u' WHEN capacity < 20 THEN 's' ELSE 'l' END FROM courses",
     "SELECT CASE WHEN capacity >= 20 THEN 'l' WHEN capacity < 20 THEN 's' ELSE 'u' END FROM courses", True,
     "CASE branches over ranges reordered"),
    (CAMPUS, "SELECT CASE WHEN capacity IS NULL THEN 'u' WHEN capacity < 20 THEN 's' ELSE 'l' END FROM courses",
     "SELECT CASE WHEN capacity < 20 THEN 's' ELSE 'l' END FROM courses", False, "NULL capacity falls into ELSE"),
    (CAMPUS, "SELECT CASE WHEN capacity IS NULL THEN 'u' WHEN capacity < 20 THEN 's' ELSE 'l' END FROM courses",
     "SELECT COALESCE(CASE WHEN capacity < 20 THEN 's' WHEN capacity >= 20 THEN 'l' END, 'u') FROM courses", True,
     "COALESCE over a CASE of literals"),
    (CAMPUS, "SELECT CASE WHEN capacity < 20 THEN 's' ELSE 'l' END FROM courses",
     "SELECT CASE WHEN capacity <= 20 THEN 's' ELSE 'l' END FROM courses", False, "range bound off by one"),
    (ORG, '''SELECT CASE WHEN "IsRemote" = 'Yes' THEN 'r' ELSE 'o' END FROM "Employee"''',
     '''SELECT CASE "IsRemote" WHEN 'Yes' THEN 'r' WHEN 'No' THEN 'o' END FROM "Employee"''', True,
     "CASE over every enum label"),
    (ORG, '''SELECT CASE WHEN "Status" = 'Active' THEN 'a' ELSE 'x' END FROM "Employee"''',
     '''SELECT CASE "Status" WHEN 'Active' THEN 'a' WHEN 'OnLeave' THEN 'x' END FROM "Employee"''', False,
     "CASE missing an enum label is NULL for it"),

    # --- semi- and anti-joins -----------------------------------------------------------------------------
    (PROJECT, "SELECT p.id FROM projects p WHERE NOT EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = p.owner_id)",
     "SELECT p.id FROM projects p WHERE (p.id, p.owner_id) NOT IN (SELECT project_id, user_id FROM project_members)", True,
     "row-valued NOT IN over NOT NULL columns"),
    (CAMPUS, "SELECT s.id FROM students s WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.id = s.department_id AND d.name = s.name)",
     "SELECT s.id FROM students s WHERE (s.department_id, s.name) NOT IN (SELECT id, name FROM departments)", False,
     "row-valued NOT IN with a nullable column"),
    (CAMPUS, "SELECT c.code FROM courses c WHERE NOT EXISTS (SELECT 1 FROM course_prerequisites cp WHERE cp.course_id = c.id)",
     "SELECT c.code FROM courses c LEFT JOIN course_prerequisites cp ON cp.course_id = c.id GROUP BY c.id, c.code "
     "HAVING COUNT(cp.prerequisite_id) = 0", True, "LEFT JOIN ... HAVING COUNT(b.c) = 0"),
    (CAMPUS, "SELECT c.code FROM courses c WHERE NOT EXISTS (SELECT 1 FROM course_prerequisites cp WHERE cp.course_id = c.id)",
     "SELECT c.code FROM courses c LEFT JOIN course_prerequisites cp ON cp.course_id = c.id GROUP BY c.id, c.code "
     "HAVING COUNT(*) = 0", False, "COUNT(*) counts the NULL-extended row"),
    (CAMPUS, "SELECT s.name FROM students s WHERE s.id IN (SELECT student_id FROM enrollments) "
             "AND s.id IN (SELECT student_id FROM enrollments WHERE grade IS NULL)",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments a JOIN enrollments b ON b.student_id = a.student_id "
     "WHERE a.student_id = s.id AND b.grade IS NULL)", True, "EXISTS over two independent tables"),
    (CAMPUS, "SELECT s.name FROM students s WHERE s.id IN (SELECT student_id FROM enrollments) "
             "AND s.id IN (SELECT student_id FROM enrollments WHERE grade IS NULL)",
     "SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments a JOIN enrollments b ON b.student_id = a.student_id "
     "AND b.course_id <> a.course_id WHERE a.student_id = s.id AND b.grade IS NULL)", False,
     "the two tables are linked"),
    (CAMPUS, "SELECT d.name FROM departments d WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)",
     "SELECT d.name FROM departments d LEFT JOIN (SELECT DISTINCT department_id FROM students) t ON t.department_id = d.id "
     "WHERE t.department_id IS NULL", True, "anti-join against a DISTINCT derived table"),

    # --- derived tables, unpivots, scalar subqueries ------------------------------------------------------------
    (CAMPUS, "SELECT department_id, SUM(credits) FROM courses GROUP BY department_id",
     "SELECT x.d, SUM(x.c) FROM (SELECT department_id AS d, credits AS c FROM courses) x GROUP BY x.d", True,
     "pass-through derived table"),
    (CAMPUS, "SELECT department_id, SUM(credits) FROM courses GROUP BY department_id",
     "SELECT x.d, SUM(x.c) FROM (SELECT DISTINCT department_id AS d, credits AS c FROM courses) x GROUP BY x.d", False,
     "a DISTINCT derived table drops repeated credits"),
    (CAMPUS, "SELECT s.id, e.term FROM students s LEFT JOIN enrollments e ON e.student_id = s.id AND e.grade > 3",
     "SELECT s.id, t.term FROM students s LEFT JOIN (SELECT student_id, term FROM enrollments WHERE grade > 3) t ON t.student_id = s.id",
     True, "filtered derived table on the NULL side of a LEFT JOIN"),
    (CAMPUS, "SELECT s.id, e.term FROM students s LEFT JOIN enrollments e ON e.student_id = s.id WHERE e.grade > 3",
     "SELECT s.id, t.term FROM students s LEFT JOIN (SELECT student_id, term FROM enrollments WHERE grade > 3) t ON t.student_id = s.id",
     False, "the filter in WHERE drops unmatched students"),
    (CAMPUS, "SELECT c.code, COUNT(e.student_id) FROM courses c LEFT JOIN enrollments e ON e.course_id = c.id GROUP BY c.id, c.code",
     "SELECT c.code, COALESCE(t.n, 0) FROM courses c LEFT JOIN (SELECT course_id, COUNT(*) AS n FROM enrollments GROUP BY course_id) t "
     "ON t.course_id = c.id", True, "LEFT JOIN of a grouped derived table"),
    (CAMPUS, "SELECT c.code, COUNT(e.student_id) FROM courses c LEFT JOIN enrollments e ON e.course_id = c.id GROUP BY c.id, c.code",
     "SELECT c.code, t.n FROM courses c LEFT JOIN (SELECT course_id, COUNT(*) AS n FROM enrollments GROUP BY course_id) t "
     "ON t.course_id = c.id", False, "NULL, not 0, without a match"),
    (CAMPUS, "SELECT COUNT(DISTINCT department_id) FROM students",
     "SELECT COUNT(t.department_id) FROM (SELECT DISTINCT department_id FROM students) t", True, "COUNT over a DISTINCT derived table"),
    (CAMPUS, "SELECT COUNT(DISTINCT department_id) FROM students",
     "SELECT COUNT(*) FROM (SELECT DISTINCT department_id FROM students) t", False, "COUNT(*) counts the NULL department"),
    (CAMPUS, "SELECT name FROM students UNION SELECT name FROM instructors",
     "SELECT DISTINCT x.name FROM (SELECT name FROM students UNION ALL SELECT name FROM instructors) x", True,
     "DISTINCT over UNION ALL"),
    (CAMPUS, "SELECT name FROM students UNION SELECT name FROM instructors",
     "SELECT x.name FROM (SELECT name FROM students UNION ALL SELECT name FROM instructors) x", False, "no DISTINCT"),
    (CAMPUS, "SELECT course_id FROM course_prerequisites UNION SELECT prerequisite_id FROM course_prerequisites",
     "SELECT DISTINCT unnest(ARRAY[course_id, prerequisite_id]) FROM course_prerequisites", True, "unnest unpivot"),
    (CAMPUS, "SELECT course_id FROM course_prerequisites UNION SELECT prerequisite_id FROM course_prerequisites",
     "SELECT unnest(ARRAY[course_id, prerequisite_id]) FROM course_prerequisites", False, "unnest without DISTINCT keeps duplicates"),
    (CAMPUS, "SELECT course_id FROM course_prerequisites UNION SELECT prerequisite_id FROM course_prerequisites",
     "SELECT DISTINCT v.x FROM course_prerequisites cp CROSS JOIN LATERAL (VALUES (cp.course_id), (cp.prerequisite_id)) v(x)",
     True, "LATERAL VALUES unpivot"),
    (CAMPUS, "SELECT COALESCE(d.name, 'U'), COUNT(*) FROM students s LEFT JOIN departments d ON d.id = s.department_id "
             "GROUP BY COALESCE(d.name, 'U')",
     "SELECT COALESCE((SELECT d.name FROM departments d WHERE d.id = s.department_id), 'U'), COUNT(*) FROM students s GROUP BY 1",
     True, "scalar subquery inside an expression"),

    # --- LATERAL -------------------------------------------------------------------------------------------
    (CAMPUS, "SELECT c.name, d.name FROM courses c JOIN departments d ON d.id = c.department_id",
     "SELECT c.name, x.name FROM courses c CROSS JOIN LATERAL (SELECT name FROM departments WHERE id = c.department_id) x", True,
     "LATERAL lookup"),
    (CAMPUS, "SELECT s.name, d.name FROM students s LEFT JOIN departments d ON d.id = s.department_id",
     "SELECT s.name, x.name FROM students s LEFT JOIN LATERAL (SELECT name FROM departments WHERE id = s.department_id) x ON TRUE",
     True, "LEFT JOIN LATERAL lookup"),
    (CAMPUS, "SELECT s.name, d.name FROM students s LEFT JOIN departments d ON d.id = s.department_id",
     "SELECT s.name, x.name FROM students s CROSS JOIN LATERAL (SELECT name FROM departments WHERE id = s.department_id) x",
     False, "CROSS JOIN LATERAL drops undeclared students"),
    (CAMPUS, "SELECT d.id, (SELECT COUNT(*) FROM courses c WHERE c.department_id = d.id) FROM departments d",
     "SELECT d.id, x.n FROM departments d CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM courses c WHERE c.department_id = d.id) x",
     True, "LATERAL aggregate: one row per outer row"),
    (CAMPUS, "SELECT DISTINCT ON (i.department_id) i.department_id, i.name FROM instructors i ORDER BY i.department_id, i.hire_date DESC, i.id",
     "SELECT d.id, x.name FROM departments d CROSS JOIN LATERAL (SELECT name FROM instructors i WHERE i.department_id = d.id "
     "ORDER BY hire_date DESC, id LIMIT 1) x ORDER BY d.id", True, "LATERAL top-1 is DISTINCT ON"),
    (CAMPUS, "SELECT DISTINCT ON (i.department_id) i.department_id, i.name FROM instructors i ORDER BY i.department_id, i.hire_date DESC, i.id",
     "SELECT d.id, x.name FROM departments d CROSS JOIN LATERAL (SELECT name FROM instructors i WHERE i.department_id = d.id "
     "ORDER BY hire_date, id LIMIT 1) x ORDER BY d.id", False, "earliest hire instead of latest"),
    (CAMPUS, "SELECT s.name, t.term FROM students s JOIN (SELECT student_id, term, ROW_NUMBER() OVER (PARTITION BY student_id "
             "ORDER BY term DESC, course_id) AS rn FROM enrollments) t ON t.student_id = s.id WHERE t.rn <= 2",
     "SELECT s.name, x.term FROM students s CROSS JOIN LATERAL (SELECT term FROM enrollments e WHERE e.student_id = s.id "
     "ORDER BY term DESC, course_id LIMIT 2) x", True, "LATERAL top-n is a ROW_NUMBER filter"),
    (CAMPUS, "SELECT s.name, t.term FROM students s JOIN (SELECT student_id, term, ROW_NUMBER() OVER (PARTITION BY student_id "
             "ORDER BY term DESC, course_id) AS rn FROM enrollments) t ON t.student_id = s.id WHERE t.rn <= 2",
     "SELECT s.name, x.term FROM students s CROSS JOIN LATERAL (SELECT term FROM enrollments e WHERE e.student_id = s.id "
     "ORDER BY term DESC, course_id LIMIT 3) x", False, "top 3, not top 2"),
    (ORG, '''SELECT "EmployeeId", "Salary" - LAG("Salary") OVER (PARTITION BY "EmployeeId" ORDER BY "EffectiveDate") FROM "SalaryHistory"''',
     '''SELECT s."EmployeeId", s."Salary" - p."Salary" FROM "SalaryHistory" s LEFT JOIN LATERAL (SELECT x."Salary" FROM "SalaryHistory" x '''
     '''WHERE x."EmployeeId" = s."EmployeeId" AND x."EffectiveDate" < s."EffectiveDate" ORDER BY x."EffectiveDate" DESC LIMIT 1) p ON TRUE''',
     True, "LATERAL previous row by a unique key is LAG"),
    (ORG, '''SELECT "EmployeeId", "Salary" - LAG("Salary") OVER (PARTITION BY "EmployeeId" ORDER BY "EffectiveDate") FROM "SalaryHistory"''',
     '''SELECT s."EmployeeId", s."Salary" - p."Salary" FROM "SalaryHistory" s LEFT JOIN LATERAL (SELECT x."Salary" FROM "SalaryHistory" x '''
     '''WHERE x."EmployeeId" = s."EmployeeId" AND x."EffectiveDate" > s."EffectiveDate" ORDER BY x."EffectiveDate" LIMIT 1) p ON TRUE''',
     False, "the next row is LEAD, not LAG"),
    (ORG, '''SELECT "EmployeeId", "Salary" - LAG("Salary") OVER (PARTITION BY "EmployeeId" ORDER BY "EffectiveDate") FROM "SalaryHistory" '''
          '''WHERE "Salary" > 1000''',
     '''SELECT s."EmployeeId", s."Salary" - p."Salary" FROM "SalaryHistory" s LEFT JOIN LATERAL (SELECT x."Salary" FROM "SalaryHistory" x '''
     '''WHERE x."EmployeeId" = s."EmployeeId" AND x."EffectiveDate" < s."EffectiveDate" ORDER BY x."EffectiveDate" DESC LIMIT 1) p ON TRUE '''
     '''WHERE s."Salary" > 1000''', False, "LAG sees only the filtered rows; the lookup sees all of them"),
    (CAMPUS, "SELECT name FROM students UNION SELECT code FROM courses",
     "SELECT DISTINCT x.name FROM (SELECT name FROM students UNION ALL SELECT code FROM courses) x", True,
     "DISTINCT over UNION ALL whose branches name their column differently"),
    (CAMPUS, "SELECT name FROM students UNION SELECT code FROM courses",
     "SELECT DISTINCT x.name FROM (SELECT name FROM students UNION ALL SELECT name FROM courses) x", False,
     "the second branch reads another column"),
    (CAMPUS, "SELECT s.name, SUM(c.credits) FROM students s JOIN enrollments e ON e.student_id = s.id JOIN courses c ON c.id = e.course_id "
             "GROUP BY s.id, s.name",
     "SELECT s.name, (SELECT SUM(c.credits) FROM courses c JOIN enrollments e ON c.id = e.course_id WHERE e.student_id = s.id) FROM students s "
     "WHERE EXISTS (SELECT 1 FROM enrollments e2 JOIN courses c2 ON c2.id = e2.course_id WHERE e2.student_id = s.id)", True,
     "correlated SUM guarded by EXISTS over the same rows, tables listed in another order"),
    (CAMPUS, "SELECT s.name, SUM(c.credits) FROM students s JOIN enrollments e ON e.student_id = s.id JOIN courses c ON c.id = e.course_id "
             "GROUP BY s.id, s.name",
     "SELECT s.name, (SELECT SUM(c.credits) FROM courses c JOIN enrollments e ON c.id = e.course_id WHERE e.student_id = s.id) FROM students s",
     False, "without the EXISTS, students with no enrollment are kept (with NULL)"),
]


class TestEquivalence(unittest.TestCase):
    def test_cases(self):
        for schema, gt, pred, expected, what in CASES:
            with self.subTest(what):
                v = compare(gt, pred, schema) if pred is not None else None
                got = v.match if v else False
                self.assertEqual(got, expected, f"{what}: reason={v.reason if v else 'null'}\n GT: {gt}\n PR: {pred}")
                if v and v.match:
                    self.assertEqual(v.score, 1.0)

    def test_deterministic(self):
        for schema, gt, pred, _, _ in CASES:
            if pred is None:
                continue
            a, b = compare(gt, pred, schema), compare(gt, pred, schema)
            self.assertEqual((a.match, a.score, a.reason), (b.match, b.score, b.reason))


if __name__ == "__main__":
    unittest.main()
