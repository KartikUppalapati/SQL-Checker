SCHEMA_ID = "campus"

# (question, target_sql, extra_tags)
QUERIES = [
    ("List the names of all currently active students.",
     "SELECT name FROM students WHERE is_active = 1",
     ["flag_int"]),

    ("Show each course's name alongside the name of the department that offers it.",
     """SELECT courses.name, departments.name
FROM courses
JOIN departments ON departments.id = courses.department_id""",
     ["generic_names"]),

    ("How many students are enrolled in each course? Include courses with no enrollments, and show the course code and the count.",
     """SELECT c.code, COUNT(e.student_id) AS enrolled
FROM courses c
LEFT JOIN enrollments e ON e.course_id = c.id
GROUP BY c.id, c.code""",
     ["outer_join"]),

    ("Which students have no email address on file?",
     "SELECT id, name FROM students WHERE email IS NULL",
     ["nullable"]),

    ("List the names of students who have never enrolled in any course.",
     """SELECT s.name
FROM students s
WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id)""",
     ["anti_join"]),

    ("What is the average grade in each department's courses, counting only graded enrollments? Order from highest to lowest average.",
     """SELECT d.name, AVG(e.grade) AS avg_grade
FROM enrollments e
JOIN courses c ON c.id = e.course_id
JOIN departments d ON d.id = c.department_id
WHERE e.grade IS NOT NULL
GROUP BY d.id, d.name
ORDER BY avg_grade DESC""",
     ["generic_names"]),

    ("Who are the top 3 instructors by number of courses they lead? Break ties by name alphabetically.",
     """SELECT i.name, COUNT(*) AS lead_courses
FROM instructors i
JOIN course_instructors ci ON ci.instructor_id = i.id
WHERE ci.role = 'lead'
GROUP BY i.id, i.name
ORDER BY lead_courses DESC, i.name ASC
LIMIT 3""",
     ["m2m", "top_k"]),

    ("For each course that has prerequisites, list the course name and the name of each prerequisite course.",
     """SELECT c.name AS course, p.name AS prerequisite
FROM course_prerequisites cp
JOIN courses c ON c.id = cp.course_id
JOIN courses p ON p.id = cp.prerequisite_id""",
     ["self_join", "m2m"]),

    ("Which courses have no prerequisites at all? Give their codes.",
     """SELECT code FROM courses
WHERE id NOT IN (SELECT course_id FROM course_prerequisites)""",
     ["anti_join"]),

    ("How many total credits is each student taking in term 2024FA? Show student name and total credits.",
     """SELECT s.name, SUM(c.credits) AS total_credits
FROM students s
JOIN enrollments e ON e.student_id = s.id
JOIN courses c ON c.id = e.course_id
WHERE e.term = '2024FA'
GROUP BY s.id, s.name""",
     ["m2m"]),

    ("What percentage of students are international?",
     "SELECT 100.0 * SUM(is_international) / COUNT(*) AS pct_international FROM students",
     ["flag_int", "integer_division"]),

    ("What is the whole-number ratio of students to instructors (rounded down)?",
     "SELECT (SELECT COUNT(*) FROM students) / (SELECT COUNT(*) FROM instructors) AS ratio",
     ["integer_division"]),

    ("Which students took both CS101 and MATH201 (in any term)? Give their names.",
     """SELECT s.name FROM students s
WHERE EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id
              WHERE e.student_id = s.id AND c.code = 'CS101')
  AND EXISTS (SELECT 1 FROM enrollments e JOIN courses c ON c.id = e.course_id
              WHERE e.student_id = s.id AND c.code = 'MATH201')""",
     ["m2m"]),

    ("Which departments spend more on instructor salaries than their budget? Show department name, total salary and budget.",
     """SELECT d.name, SUM(i.salary) AS total_salary, d.budget
FROM departments d
JOIN instructors i ON i.department_id = d.id
GROUP BY d.id, d.name, d.budget
HAVING SUM(i.salary) > d.budget""",
     ["having", "nullable"]),

    ("List tenured instructors hired on or after January 1, 2020, newest hire first.",
     """SELECT name, hire_date FROM instructors
WHERE is_tenured = 1 AND hire_date >= DATE '2020-01-01'
ORDER BY hire_date DESC""",
     ["flag_int", "dates"]),

    ("Which courses were over capacity in term 2024FA (more enrolled students than their capacity)? Courses with unlimited capacity never count.",
     """SELECT c.code, c.capacity, COUNT(*) AS enrolled
FROM courses c
JOIN enrollments e ON e.course_id = c.id
WHERE e.term = '2024FA'
GROUP BY c.id, c.code, c.capacity
HAVING COUNT(*) > c.capacity""",
     ["having", "nullable"]),

    ("Find every course that is directly or indirectly a prerequisite of the course with code CS301. Return the codes.",
     """WITH RECURSIVE prereqs AS (
    SELECT cp.prerequisite_id AS id
    FROM course_prerequisites cp
    JOIN courses c ON c.id = cp.course_id
    WHERE c.code = 'CS301'
    UNION
    SELECT cp.prerequisite_id
    FROM course_prerequisites cp
    JOIN prereqs p ON cp.course_id = p.id
)
SELECT courses.code FROM courses JOIN prereqs ON prereqs.id = courses.id""",
     ["self_join"]),

    ("For each department, rank its students by their average graded grade (highest first) and return department name, student name and rank. Students without a department are excluded.",
     """SELECT d.name AS department, s.name AS student,
       RANK() OVER (PARTITION BY d.id ORDER BY AVG(e.grade) DESC) AS grade_rank
FROM students s
JOIN departments d ON d.id = s.department_id
JOIN enrollments e ON e.student_id = s.id
WHERE e.grade IS NOT NULL
GROUP BY d.id, d.name, s.id, s.name""",
     ["generic_names"]),

    ("How many students does each department have? Students with no declared department should appear as a group labelled 'Undeclared'.",
     """SELECT COALESCE(d.name, 'Undeclared') AS department, COUNT(*) AS students
FROM students s
LEFT JOIN departments d ON d.id = s.department_id
GROUP BY COALESCE(d.name, 'Undeclared')""",
     ["outer_join", "nullable"]),

    ("List the names of online courses that are co-taught (have more than one instructor).",
     """SELECT c.name FROM courses c
JOIN course_instructors ci ON ci.course_id = c.id
WHERE c.is_online = 1
GROUP BY c.id, c.name
HAVING COUNT(ci.instructor_id) > 1""",
     ["flag_int", "m2m", "having"]),

    ("Which departments have no students who declared them as their major?",
     """SELECT d.name
FROM departments d
WHERE NOT EXISTS (SELECT 1 FROM students s WHERE s.department_id = d.id)""",
     ["anti_join", "nullable"]),

    ("List every distinct name of a person on campus, students and instructors alike.",
     """SELECT name FROM students
UNION
SELECT name FROM instructors""",
     ["generic_names"]),

    ("List the codes of all online courses together with the codes of 4-credit in-person courses.",
     """SELECT code FROM courses WHERE is_online = 1
UNION
SELECT code FROM courses WHERE is_online = 0 AND credits = 4""",
     ["flag_int"]),

    ("For each student with at least one enrollment, show their name, the number of courses passed (graded 2.0 or higher) and the number failed (graded below 2.0). Ungraded enrollments count as neither.",
     """SELECT s.name,
       SUM(CASE WHEN e.grade >= 2.0 THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN e.grade < 2.0 THEN 1 ELSE 0 END) AS failed
FROM students s
JOIN enrollments e ON e.student_id = s.id
GROUP BY s.id, s.name""",
     ['case', 'nullable']),

    ("For each department that has instructors, show the best-paid instructor: department id, instructor name and salary. Instructors with an unknown salary rank last, and ties go to the lowest instructor id.",
     """SELECT DISTINCT ON (i.department_id) i.department_id, i.name, i.salary
FROM instructors i
ORDER BY i.department_id, i.salary DESC NULLS LAST, i.id""",
     ['distinct_on', 'nullable']),
]
