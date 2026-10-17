-- campus: university registrar
-- Shape:    many-to-many (students <-> courses via enrollments, courses <-> instructors via
--           course_instructors) plus a self-referential many-to-many (course_prerequisites)
-- Naming:   snake_case, unquoted, plural table names
-- Flags:    SMALLINT 0/1 with CHECK constraints
-- Generic:  id / name / department_id appear in several tables

CREATE TABLE departments (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE,
    building    VARCHAR(50),                      -- nullable
    budget      NUMERIC(12, 2)                    -- nullable
);

CREATE TABLE students (
    id                INTEGER PRIMARY KEY,
    name              VARCHAR(100) NOT NULL,
    email             VARCHAR(255) UNIQUE,                       -- nullable
    department_id     INTEGER REFERENCES departments (id),       -- nullable: undeclared major
    enrollment_year   INTEGER NOT NULL,
    graduation_year   INTEGER,                                   -- null until graduated
    is_active         SMALLINT NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_international  SMALLINT NOT NULL DEFAULT 0 CHECK (is_international IN (0, 1))
);

CREATE TABLE instructors (
    id             INTEGER PRIMARY KEY,
    name           VARCHAR(100) NOT NULL,
    department_id  INTEGER NOT NULL REFERENCES departments (id),
    hire_date      DATE NOT NULL,
    salary         NUMERIC(10, 2),                               -- nullable
    is_tenured     SMALLINT NOT NULL DEFAULT 0 CHECK (is_tenured IN (0, 1))
);

CREATE TABLE courses (
    id             INTEGER PRIMARY KEY,
    code           VARCHAR(20) NOT NULL UNIQUE,
    name           VARCHAR(200) NOT NULL,
    department_id  INTEGER NOT NULL REFERENCES departments (id),
    credits        INTEGER NOT NULL,
    capacity       INTEGER,                                      -- null = unlimited
    is_online      SMALLINT NOT NULL DEFAULT 0 CHECK (is_online IN (0, 1))
);

CREATE TABLE enrollments (
    student_id  INTEGER NOT NULL REFERENCES students (id),
    course_id   INTEGER NOT NULL REFERENCES courses (id),
    term        VARCHAR(6) NOT NULL,                             -- e.g. '2024FA'
    grade       NUMERIC(2, 1),                                   -- 0.0-4.0, null while in progress
    is_audit    SMALLINT NOT NULL DEFAULT 0 CHECK (is_audit IN (0, 1)),
    PRIMARY KEY (student_id, course_id, term)
);

CREATE TABLE course_instructors (
    course_id      INTEGER NOT NULL REFERENCES courses (id),
    instructor_id  INTEGER NOT NULL REFERENCES instructors (id),
    role           VARCHAR(10) NOT NULL CHECK (role IN ('lead', 'assistant')),
    PRIMARY KEY (course_id, instructor_id)
);

CREATE TABLE course_prerequisites (
    course_id        INTEGER NOT NULL REFERENCES courses (id),
    prerequisite_id  INTEGER NOT NULL REFERENCES courses (id),
    PRIMARY KEY (course_id, prerequisite_id)
);
