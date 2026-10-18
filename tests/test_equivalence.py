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

    # --- result shape ------------------------------------------------------------------------------
    (CAMPUS, "SELECT id, name FROM students", "SELECT s.id AS x, s.name AS y FROM students s", True, "aliases ignored"),
    (CAMPUS, "SELECT id, name FROM students", "SELECT name, id FROM students", False, "column order matters"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students ORDER BY name", True, "unordered target"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students", False, "ordered target"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students ORDER BY 1", True, "ordinal"),
    (CAMPUS, "SELECT name FROM students ORDER BY name", "SELECT name FROM students ORDER BY name DESC", False, "direction"),
    (CAMPUS, "SELECT name FROM students", "SELECT name FROM students ORDER BY name LIMIT 5", False, "limit"),
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
