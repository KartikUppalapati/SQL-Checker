"""The validity gate against PostgreSQL itself: every query below was run through EXPLAIN on
PostgreSQL 16.2 with the dataset schema loaded. The harness must reject exactly the ones PostgreSQL
rejects (comment: PostgreSQL's error), and must accept every query PostgreSQL accepts, so the gate is
never stricter than the real thing.

Run:  python -m unittest discover -s tests
"""
import logging
import unittest
from pathlib import Path

from sqleval.equivalence import invalid_reason

logging.getLogger("sqlglot").setLevel(logging.ERROR)
SCHEMAS = Path(__file__).resolve().parent.parent / "dataset" / "schemas"

CAMPUS = (SCHEMAS / "campus.sql").read_text()
PROJECT = (SCHEMAS / "project_tracker.sql").read_text()
RIDE = (SCHEMAS / "rideshare.sql").read_text()

# (schema, query) that PostgreSQL accepts
VALID = [
    (CAMPUS, 'SELECT name AS n FROM students ORDER BY n'),
    (CAMPUS, 'SELECT department_id AS d, COUNT(*) FROM students GROUP BY d'),
    (CAMPUS, 'SELECT name AS n FROM students ORDER BY n DESC NULLS LAST'),
    (CAMPUS, 'SELECT name AS n FROM students ORDER BY (n)'),
    (CAMPUS, "SELECT id AS name FROM students WHERE name = 'x'"),
    (CAMPUS, 'SELECT department_id, COUNT(*) AS c FROM students GROUP BY department_id HAVING COUNT(*) > 1'),
    (CAMPUS, 'SELECT s.name FROM students s WHERE EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)'),
    (CAMPUS, 'SELECT (SELECT x.c FROM (SELECT COUNT(*) c FROM enrollments e WHERE e.student_id = s.id) x) FROM students s'),
    (CAMPUS, 'SELECT s.name, g FROM students s, generate_series(1, s.enrollment_year % 3) g'),
    (CAMPUS, 'SELECT s.name FROM students s JOIN departments d ON d.id = s.department_id ORDER BY name'),
    (CAMPUS, 'SELECT s.name, d.name FROM students s JOIN departments d ON d.id = s.department_id ORDER BY 1'),
    (CAMPUS, 'SELECT hire_date - hire_date, hire_date + 1, hire_date - 7 FROM instructors'),
    (CAMPUS, "SELECT '1' + 2"),
    (CAMPUS, "SELECT name FROM students WHERE 'true'"),
    (CAMPUS, 'SELECT CASE WHEN is_active = 1 THEN 1 ELSE NULL END FROM students'),
    (CAMPUS, "SELECT CASE WHEN is_active = 1 THEN 'a' ELSE name END FROM students"),
    (CAMPUS, 'SELECT COALESCE(grade, 0) FROM enrollments'),
    (CAMPUS, "SELECT COALESCE(hire_date, '2024-01-01') FROM instructors"),
    (CAMPUS, 'SELECT credits FROM courses UNION SELECT grade FROM enrollments'),
    (CAMPUS, 'SELECT name FROM students UNION SELECT code FROM courses'),
    (CAMPUS, "SELECT hire_date FROM instructors UNION SELECT CAST('2024-01-01 10:00' AS TIMESTAMP)"),
    (CAMPUS, 'SELECT NULL UNION SELECT 1'),
    (CAMPUS, 'SELECT name FROM students WHERE id IN (1, 2.5)'),
    (CAMPUS, 'SELECT name FROM students WHERE (id, department_id) IN (SELECT student_id, course_id FROM enrollments)'),
    (CAMPUS, 'SELECT name FROM students ORDER BY id LIMIT ALL'),
    (CAMPUS, 'SELECT name FROM students LIMIT (SELECT COUNT(*) FROM departments)'),
    (CAMPUS, 'SELECT DISTINCT ON (department_id) department_id, name FROM students ORDER BY department_id, name'),
    (CAMPUS, 'SELECT DISTINCT ON (department_id, enrollment_year) name FROM students ORDER BY enrollment_year, department_id, name'),
    (CAMPUS, 'SELECT DISTINCT ON (department_id) name FROM students'),
    (CAMPUS, 'SELECT DISTINCT ON (1) department_id, name FROM students ORDER BY 1, 2'),
    (CAMPUS, 'SELECT DISTINCT ON (d) department_id AS d, name FROM students ORDER BY d, name'),
    (CAMPUS, 'SELECT department_id, SUM(COUNT(*)) OVER () FROM students GROUP BY department_id'),
    (CAMPUS, 'SELECT department_id, RANK() OVER (ORDER BY COUNT(*)) FROM students GROUP BY department_id'),
    (CAMPUS, 'SELECT percent_rank(2020) WITHIN GROUP (ORDER BY enrollment_year) FROM students'),
    (CAMPUS, 'SELECT (SELECT 1 FROM departments d WHERE COUNT(s.id) > 0 LIMIT 1) FROM students s'),
    (CAMPUS, "SELECT string_agg(name, ',') FROM students"),
    (CAMPUS, "SELECT string_agg(id::text, ',' ORDER BY id) FROM students"),
    (CAMPUS, 'SELECT bool_and(credits > 1) FROM courses'),
    (CAMPUS, "SELECT name FROM students WHERE name LIKE 'a%' AND email ILIKE '%@x'"),
    (CAMPUS, 'SELECT name FROM students UNION SELECT name FROM departments ORDER BY name DESC'),
    (CAMPUS, 'SELECT name FROM students UNION SELECT name FROM departments ORDER BY 1'),
    (CAMPUS, 'SELECT s.name FROM students s JOIN LATERAL (SELECT COUNT(*) c FROM enrollments e WHERE e.student_id = s.id) x ON true'),
    (CAMPUS, 'SELECT students.name FROM students JOIN students s ON s.id = students.id'),
    (CAMPUS, 'SELECT x.a FROM (VALUES (1), (2)) AS x(a)'),
    (CAMPUS, 'SELECT y.a FROM (SELECT id, name FROM students) AS y(a, b)'),
    (CAMPUS, 'SELECT department_id, COUNT(*) FROM students GROUP BY 1 ORDER BY 2'),
    (CAMPUS, 'SELECT d.id, (SELECT COUNT(*) FROM students s WHERE s.department_id = d.id) FROM departments d GROUP BY d.id'),
    (CAMPUS, 'SELECT d.id, (SELECT COUNT(*) FROM students s WHERE s.name = d.name) FROM departments d GROUP BY d.id'),
    (CAMPUS, 'SELECT DISTINCT name FROM students ORDER BY name'),
    (CAMPUS, "SELECT name FROM students WHERE enrollment_year = '+5' OR enrollment_year = ' 7 '"),
    (CAMPUS, "SELECT name FROM enrollments e JOIN students s ON s.id = e.student_id WHERE grade = '.5' OR grade = '1e0'"),
    (CAMPUS, 'SELECT name FROM students WHERE id BETWEEN SYMMETRIC 10 AND 1'),
    (CAMPUS, 'SELECT name FROM students WHERE id NOT BETWEEN SYMMETRIC 10 AND 1'),
    (CAMPUS, 'SELECT -credits, credits * 2 / 3 % 2 FROM courses'),
    (CAMPUS, 'SELECT name FROM students WHERE NOT (is_active = 1) AND (email IS NULL OR id > 3)'),
    (CAMPUS, 'SELECT name FROM students s WHERE s.id = (SELECT MAX(student_id) FROM enrollments)'),
    (CAMPUS, 'SELECT name FROM students WHERE id = ANY (ARRAY[1, 2]) AND id <> ALL (SELECT student_id FROM enrollments)'),
    (CAMPUS, 'SELECT name, COUNT(*) OVER (PARTITION BY department_id) FROM students ORDER BY COUNT(*) OVER (PARTITION BY department_id)'),
    (CAMPUS, 'SELECT c.name FROM courses c WHERE c.credits > (SELECT AVG(credits) FROM courses c2 WHERE c2.department_id = c.department_id)'),
    (CAMPUS, 'WITH t AS (SELECT department_id, COUNT(*) AS n FROM students GROUP BY department_id) SELECT n + 1 FROM t WHERE n > 1'),
    (CAMPUS, 'WITH t AS (SELECT name FROM students) SELECT t.name FROM t'),
    (CAMPUS, 'SELECT student_id FROM enrollments JOIN course_instructors USING (course_id)'),
    (CAMPUS, 'SELECT name FROM students WHERE email = name'),
    (CAMPUS, 'SELECT name FROM students ORDER BY id + 1'),
    (CAMPUS, 'SELECT department_id AS id FROM students GROUP BY id'),
    (CAMPUS, 'SELECT name FROM students WHERE id IN (SELECT student_id FROM enrollments UNION SELECT id FROM instructors)'),
    (CAMPUS, 'SELECT name FROM students LIMIT 2.5'),
    (CAMPUS, 'SELECT name FROM students LIMIT -1'),
    (PROJECT, "SELECT name FROM tasks WHERE status = 'done'"),
    (PROJECT, "SELECT name FROM tasks WHERE status::text LIKE 'd%'"),
    (PROJECT, "SELECT name FROM tasks WHERE status < 'done'"),
    (PROJECT, "SELECT name FROM tasks WHERE created_at > '2024-01-01'"),
    (PROJECT, "SELECT name FROM tasks WHERE created_at > '2024-01-01 10:30:00'"),
    (PROJECT, "SELECT name FROM tasks WHERE completed_at - created_at > INTERVAL '1 day'"),
    (PROJECT, 'SELECT AVG(completed_at - created_at) FROM tasks'),
    (PROJECT, 'SELECT SUM(completed_at - created_at) FROM tasks'),
    (PROJECT, 'SELECT MAX(created_at) FROM tasks'),
    (PROJECT, 'SELECT created_at::date FROM tasks UNION SELECT due_date FROM projects'),
    (PROJECT, "SELECT name FROM tasks WHERE status IN ('todo', 'done')"),
    (PROJECT, "SELECT CASE WHEN status = 'done' THEN 'x' ELSE status::text END FROM tasks"),
    (PROJECT, 'SELECT MIN(status) FROM tasks'),
    (RIDE, 'SELECT completed_at - started_at FROM trips'),
    (RIDE, 'SELECT AVG(completed_at - started_at), SUM(completed_at - started_at) FROM trips'),
    (RIDE, 'SELECT SUM(distance_km) / COUNT(*) FROM trips'),
    (RIDE, "SELECT requested_at + INTERVAL '1 day' FROM trips"),
    (RIDE, "SELECT requested_at::date - DATE '2024-01-01' FROM trips"),
    (RIDE, "SELECT id FROM trips WHERE requested_at - INTERVAL '1 hour' > now()"),
    (RIDE, 'SELECT ROUND(fare, 1), ROUND(distance_km), ROUND(distance_km::numeric, 2), trunc(fare, 1), trunc(distance_km) FROM trips'),
    (RIDE, 'SELECT name FROM drivers WHERE rating * 2 > 9'),
    (RIDE, 'SELECT t.id FROM trips t JOIN riders r ON r.id = t.rider_id WHERE r.is_verified AND t.is_shared'),
    (RIDE, 'SELECT name FROM riders WHERE NOT is_verified'),
    (RIDE, 'SELECT CASE WHEN is_verified THEN 1 END FROM riders'),
    (RIDE, 'SELECT bool_and(is_verified), bool_or(is_verified), every(is_verified) FROM riders'),
    (RIDE, 'SELECT MAX(rating), MIN(rating), MIN(requested_at) FROM drivers, trips'),
    (RIDE, 'SELECT COALESCE(marketing_opt_in, false) FROM riders'),
    (RIDE, 'SELECT COUNT(*) FROM riders WHERE marketing_opt_in IS UNKNOWN'),
    (RIDE, 'SELECT COUNT(*) FROM riders WHERE marketing_opt_in IS NOT UNKNOWN'),
    (RIDE, 'SELECT is_verified::int, is_verified::text FROM riders'),
    (RIDE, "SELECT name FROM riders WHERE is_verified = 't' OR is_verified IN (true)"),
    (RIDE, 'SELECT EXTRACT(EPOCH FROM completed_at - started_at) / 60 FROM trips'),
    (RIDE, 'SELECT (completed_at - started_at) * 2, (completed_at - started_at) / 2 FROM trips'),
    (RIDE, "SELECT id FROM trips WHERE date_trunc('day', requested_at) = '2024-01-01'"),
    (RIDE, "SELECT requested_at AT TIME ZONE 'UTC', timezone('UTC', requested_at), date(requested_at) FROM trips"),
    (RIDE, "SELECT id FROM trips WHERE age(completed_at, started_at) > INTERVAL '1 hour'"),
    (RIDE, "SELECT id FROM riders WHERE signed_up_at > '2024-01-01 10:00 PM'"),
    (RIDE, "SELECT id FROM riders WHERE signed_up_at > '2024-03-01T10:00:00Z'"),
    (RIDE, "SELECT id FROM riders WHERE signed_up_at > '2024-03-01 10:00:00 America/New_York'"),
    (RIDE, "SELECT id FROM trips WHERE requested_at > 'today' OR requested_at > 'epoch'"),
    (RIDE, "SELECT id FROM trips WHERE requested_at BETWEEN SYMMETRIC '2024-02-01' AND '2024-01-01'"),
    (RIDE, 'SELECT is_verified FROM riders UNION SELECT is_active FROM drivers'),
    (RIDE, 'SELECT id FROM trips WHERE surge_multiplier > 1.5 AND distance_km > 2'),
    (RIDE, 'SELECT city_id, AVG(rating) FROM drivers GROUP BY city_id HAVING AVG(rating) > 4'),
    (RIDE, "SELECT id FROM trips WHERE completed_at - started_at > '30 minutes'"),
    (RIDE, 'SELECT COUNT(*) FILTER (WHERE is_shared), SUM(is_shared::int) FROM trips'),
    (RIDE, "SELECT id FROM trips WHERE (completed_at - started_at) BETWEEN INTERVAL '1 minute' AND INTERVAL '1 hour'"),
    (RIDE, 'SELECT name FROM drivers WHERE rating IS NOT NULL ORDER BY rating DESC NULLS LAST'),
]

# (schema, query) that PostgreSQL rejects before running it
INVALID = [
    (CAMPUS, 'SELECT MIN(hire_date), MAX(name), SUM(salary), AVG(credits) FROM instructors, courses'),  # column reference "name" is ambiguous
    (CAMPUS, 'SELECT s.id FROM students s JOIN enrollments USING (id)'),  # column "id" specified in USING clause does not exist in right table
    (CAMPUS, "SELECT name AS n FROM students WHERE n = 'x'"),  # column "n" does not exist
    (CAMPUS, 'SELECT department_id, COUNT(*) AS c FROM students GROUP BY department_id HAVING c > 1'),  # column "c" does not exist
    (CAMPUS, 'SELECT department_id, COUNT(*) AS c FROM students GROUP BY department_id ORDER BY c + 1'),  # column "c" does not exist
    (CAMPUS, 'SELECT department_id AS d, COUNT(*) FROM students GROUP BY d + 1'),  # column "d" does not exist
    (CAMPUS, 'SELECT COUNT(*) AS c, c * 2 FROM students'),  # column "c" does not exist
    (CAMPUS, "SELECT name FROM students WHERE enrollment_year = '2020.0'"),  # invalid input syntax for type integer: "2020.0"
    (CAMPUS, 'SELECT name FROM students ORDER BY 2'),  # ORDER BY position 2 is not in select list
    (CAMPUS, 'SELECT name FROM students GROUP BY 0'),  # GROUP BY position 0 is not in select list
    (CAMPUS, 'SELECT name FROM students UNION SELECT name, id FROM departments'),  # each UNION query must have the same number of columns
    (CAMPUS, 'SELECT name FROM students UNION SELECT id FROM departments'),  # UNION types character varying and integer cannot be matched
    (CAMPUS, 'SELECT COALESCE(email, 0) FROM students'),  # COALESCE types character varying and integer cannot be matched
    (CAMPUS, 'SELECT CASE WHEN is_active = 1 THEN name ELSE 0 END FROM students'),  # CASE types integer and character varying cannot be matched
    (CAMPUS, "SELECT COALESCE(grade, 'n/a') FROM enrollments"),  # invalid input syntax for type numeric: "n/a"
    (CAMPUS, 'WITH x AS (SELECT SUM(name) FROM students) SELECT name FROM students'),  # function sum(character varying) does not exist
    (CAMPUS, 'SELECT name FROM students WHERE EXISTS (SELECT SUM(e.term) FROM enrollments e WHERE e.student_id = students.id)'),  # function sum(character varying) does not exist
    (CAMPUS, 'SELECT DISTINCT ON (department_id) department_id, name FROM students ORDER BY name, department_id'),  # SELECT DISTINCT ON expressions must match initial ORDER BY expressions
    (CAMPUS, 'SELECT s.name, x.c FROM students s JOIN (SELECT COUNT(*) c FROM enrollments e WHERE e.student_id = s.id) x ON true'),  # invalid reference to FROM-clause entry for table "s"
    (CAMPUS, "SELECT name FROM students LIMIT 'a'"),  # invalid input syntax for type bigint: "a"
    (CAMPUS, 'SELECT name FROM students LIMIT id'),  # argument of LIMIT must not contain variables
    (CAMPUS, 'SELECT name FROM students WHERE id IN (SELECT student_id, course_id FROM enrollments)'),  # subquery has too many columns
    (CAMPUS, 'SELECT name, (SELECT id, name FROM departments LIMIT 1) FROM students'),  # subquery must return only one column
    (CAMPUS, 'SELECT name FROM students s WHERE students.id > 3'),  # invalid reference to FROM-clause entry for table "students"
    (CAMPUS, 'SELECT name FROM students s JOIN departments s ON true'),  # table name "s" specified more than once
    (CAMPUS, 'SELECT name FROM students s JOIN departments d ON d.id = s.department_id'),  # column reference "name" is ambiguous
    (CAMPUS, "SELECT s.id FROM students s JOIN departments d ON d.id = s.department_id WHERE name = 'x'"),  # column reference "name" is ambiguous
    (CAMPUS, 'SELECT name FROM students WHERE ROW_NUMBER() OVER (ORDER BY id) = 1'),  # window functions are not allowed in WHERE
    (CAMPUS, 'SELECT department_id FROM students GROUP BY department_id HAVING ROW_NUMBER() OVER () = 1'),  # window functions are not allowed in HAVING
    (CAMPUS, 'SELECT COUNT(*) FROM students GROUP BY COUNT(*)'),  # aggregate functions are not allowed in GROUP BY
    (CAMPUS, 'SELECT MAX(COUNT(*)) FROM students GROUP BY department_id'),  # aggregate function calls cannot be nested
    (CAMPUS, 'SELECT s.name FROM students s JOIN enrollments e ON COUNT(*) > 1'),  # aggregate functions are not allowed in JOIN conditions
    (CAMPUS, 'SELECT name FROM students WHERE COUNT(*) > 1'),  # aggregate functions are not allowed in WHERE
    (CAMPUS, 'SELECT ROW_NUMBER() FROM students'),  # window function row_number requires an OVER clause
    (CAMPUS, 'SELECT rank() FROM students'),  # window function rank requires an OVER clause
    (CAMPUS, 'SELECT SUM(ROW_NUMBER() OVER ()) FROM students'),  # aggregate function calls cannot contain window function calls
    (CAMPUS, 'SELECT SUM(name) FROM students'),  # function sum(character varying) does not exist
    (CAMPUS, 'SELECT AVG(hire_date) FROM instructors'),  # function avg(date) does not exist
    (CAMPUS, "SELECT string_agg(id, ',') FROM students"),  # function string_agg(integer, unknown) does not exist
    (CAMPUS, 'SELECT name + 1 FROM students'),  # operator does not exist: character varying + integer
    (CAMPUS, 'SELECT hire_date + hire_date FROM instructors'),  # operator does not exist: date + date
    (CAMPUS, 'SELECT hire_date * 2 FROM instructors'),  # operator does not exist: date * integer
    (CAMPUS, "SELECT name FROM students WHERE id LIKE '1%'"),  # operator does not exist: integer ~~ unknown
    (CAMPUS, 'SELECT name FROM students WHERE id'),  # argument of WHERE must be type boolean, not type integer
    (CAMPUS, 'SELECT name FROM students WHERE name'),  # argument of WHERE must be type boolean, not type character varying
    (CAMPUS, 'SELECT CASE WHEN name THEN 1 END FROM students'),  # argument of CASE/WHEN must be type boolean, not type character varying
    (CAMPUS, 'SELECT name FROM students WHERE NOT id'),  # argument of NOT must be type boolean, not type integer
    (CAMPUS, 'SELECT name FROM students WHERE is_active AND id > 1'),  # argument of AND must be type boolean, not type smallint
    (CAMPUS, 'SELECT name FROM students WHERE is_active = TRUE'),  # operator does not exist: smallint = boolean
    (CAMPUS, 'SELECT name FROM students WHERE name = 5'),  # operator does not exist: character varying = integer
    (CAMPUS, 'SELECT name FROM students WHERE name = enrollment_year'),  # operator does not exist: character varying = integer
    (CAMPUS, 'SELECT name FROM instructors WHERE hire_date > 2020'),  # operator does not exist: date > integer
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date = '2024-13-01'"),  # date/time field value out of range: "2024-13-01"
    (CAMPUS, "SELECT name FROM instructors WHERE hire_date = '2024-02-30'"),  # date/time field value out of range: "2024-02-30"
    (CAMPUS, 'SELECT d.name, (SELECT COUNT(*) FROM students s WHERE s.department_id = d.id) FROM departments d GROUP BY d.name'),  # subquery uses ungrouped column "d.id" from outer query
    (CAMPUS, 'SELECT name FROM students WHERE 1'),  # argument of WHERE must be type boolean, not type integer
    (CAMPUS, 'SELECT MAX(is_active = 1) FROM students'),  # function max(boolean) does not exist
    (CAMPUS, 'SELECT bool_and(credits) FROM courses'),  # function bool_and(integer) does not exist
    (CAMPUS, 'SELECT name FROM students UNION SELECT name FROM departments ORDER BY id'),  # column "id" does not exist
    (CAMPUS, 'SELECT name FROM students UNION SELECT name FROM departments ORDER BY students.name'),  # missing FROM-clause entry for table "students"
    (CAMPUS, 'SELECT name FROM students UNION SELECT name FROM departments ORDER BY lower(name)'),  # invalid UNION/INTERSECT/EXCEPT ORDER BY clause
    (CAMPUS, "SELECT name FROM students WHERE id IN ('a', 'b')"),  # invalid input syntax for type integer: "a"
    (CAMPUS, "SELECT name FROM students WHERE id BETWEEN 'a' AND 'z'"),  # invalid input syntax for type integer: "a"
    (CAMPUS, 'SELECT NULLIF(name, 1) FROM students'),  # operator does not exist: character varying = integer
    (CAMPUS, 'SELECT GREATEST(name, 1) FROM students'),  # GREATEST types character varying and integer cannot be matched
    (CAMPUS, 'SELECT name FROM students WHERE (SELECT name FROM departments LIMIT 1) = 1'),  # operator does not exist: character varying = integer
    (CAMPUS, "SELECT CASE id WHEN 'x' THEN 1 END FROM students"),  # invalid input syntax for type integer: "x"
    (CAMPUS, "SELECT name FROM students WHERE CAST('abc' AS INTEGER) = id"),  # invalid input syntax for type integer: "abc"
    (CAMPUS, 'SELECT department_id AS d FROM students GROUP BY department_id HAVING d > 1'),  # column "d" does not exist
    (PROJECT, "SELECT name FROM tasks WHERE status LIKE 'd%'"),  # operator does not exist: task_status ~~ unknown
    (PROJECT, 'SELECT name FROM tasks WHERE status = name'),  # operator does not exist: task_status = character varying
    (PROJECT, 'SELECT name FROM tasks WHERE status = 1'),  # operator does not exist: task_status = integer
    (PROJECT, 'SELECT name FROM tasks WHERE created_at > 5'),  # operator does not exist: timestamp without time zone > integer
    (PROJECT, 'SELECT SUM(created_at) FROM tasks'),  # function sum(timestamp without time zone) does not exist
    (PROJECT, 'SELECT created_at + created_at FROM tasks'),  # operator does not exist: timestamp without time zone + timestamp without time zone
    (PROJECT, "SELECT name FROM tasks WHERE created_at = '2024-02-31 10:00'"),  # date/time field value out of range: "2024-02-31 10:00"
    (PROJECT, "SELECT CASE WHEN status = 'done' THEN 'x' ELSE status END FROM tasks"),  # invalid input value for enum task_status: "x"
    (PROJECT, "SELECT name FROM tasks t JOIN projects p ON p.id = t.project_id WHERE name = 'a'"),  # column reference "name" is ambiguous
    (PROJECT, 'SELECT COALESCE(completed_at, 0) FROM tasks'),  # COALESCE types timestamp without time zone and integer cannot be matched
    (PROJECT, 'SELECT status FROM tasks UNION SELECT role FROM project_members'),  # UNION could not convert type member_role to task_status
    (RIDE, 'SELECT ROUND(distance_km, 2) FROM trips'),  # function round(double precision, integer) does not exist
    (RIDE, 'SELECT trunc(distance_km, 1) FROM trips'),  # function trunc(double precision, integer) does not exist
    (RIDE, 'SELECT MAX(is_verified) FROM riders'),  # function max(boolean) does not exist
    (RIDE, 'SELECT SUM(is_shared) FROM trips'),  # function sum(boolean) does not exist
    (RIDE, 'SELECT name FROM riders WHERE is_verified = 1'),  # operator does not exist: boolean = integer
    (RIDE, 'SELECT is_verified + 1 FROM riders'),  # operator does not exist: boolean + integer
    (RIDE, 'SELECT requested_at + requested_at FROM trips'),  # operator does not exist: timestamp with time zone + timestamp with time zone
    (RIDE, 'SELECT requested_at * 2 FROM trips'),  # operator does not exist: timestamp with time zone * integer
    (RIDE, 'SELECT id FROM trips WHERE completed_at - started_at > 30'),  # operator does not exist: interval > integer
    (RIDE, "SELECT id FROM trips WHERE distance_km LIKE '1%'"),  # operator does not exist: double precision ~~ unknown
    (RIDE, 'SELECT id FROM trips WHERE requested_at > 5'),  # operator does not exist: timestamp with time zone > integer
    (RIDE, 'SELECT id FROM trips WHERE distance_km'),  # argument of WHERE must be type boolean, not type double precision
    (RIDE, "SELECT name FROM riders WHERE is_verified > 'maybe'"),  # invalid input syntax for type boolean: "maybe"
    (RIDE, "SELECT id FROM trips WHERE requested_at = '2024-13-01'"),  # date/time field value out of range: "2024-13-01"
    (RIDE, 'SELECT AVG(requested_at) FROM trips'),  # function avg(timestamp with time zone) does not exist
    (RIDE, "SELECT string_agg(rating, ',') FROM drivers"),  # function string_agg(real, unknown) does not exist
    (RIDE, "SELECT id FROM trips WHERE requested_at > '2024-03-01x'"),  # invalid input syntax for type timestamp with time zone: "2024-03-01x"
    (RIDE, 'SELECT is_verified FROM riders UNION SELECT rating FROM drivers'),  # UNION types boolean and real cannot be matched
    (RIDE, 'SELECT COALESCE(marketing_opt_in, 0) FROM riders'),  # COALESCE types boolean and integer cannot be matched
]


class TestGate(unittest.TestCase):
    def test_accepts_what_postgresql_accepts(self):
        for schema, sql in VALID:
            with self.subTest(sql):
                self.assertIsNone(invalid_reason(sql, schema), sql)

    def test_rejects_what_postgresql_rejects(self):
        for schema, sql in INVALID:
            with self.subTest(sql):
                self.assertIsNotNone(invalid_reason(sql, schema), sql)


if __name__ == "__main__":
    unittest.main()
