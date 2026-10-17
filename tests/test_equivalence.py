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
