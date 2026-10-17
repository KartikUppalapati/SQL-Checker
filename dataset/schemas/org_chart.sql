-- org_chart: company HR directory
-- Shape:    self-referential hierarchies (Employee.ManagerId -> Employee, Department.ParentDepartmentId
--           -> Department) plus a self-referential many-to-many (Mentorship)
-- Naming:   PascalCase, double-quoted (case-sensitive; must be quoted in queries), singular tables
-- Flags:    PostgreSQL ENUM types ("RemoteFlag" 'Yes'/'No'; "EmploymentStatus")
-- Generic:  "Id" / "Name" / "Salary" appear in several tables

CREATE TYPE "EmploymentStatus" AS ENUM ('Active', 'OnLeave', 'Terminated');
CREATE TYPE "RemoteFlag" AS ENUM ('Yes', 'No');

CREATE TABLE "Location" (
    "Id"       INTEGER PRIMARY KEY,
    "Name"     TEXT NOT NULL,
    "City"     TEXT NOT NULL,
    "Country"  TEXT NOT NULL
);

CREATE TABLE "Department" (
    "Id"                  INTEGER PRIMARY KEY,
    "Name"                TEXT NOT NULL,
    "ParentDepartmentId"  INTEGER REFERENCES "Department" ("Id"),   -- null for top-level
    "LocationId"          INTEGER REFERENCES "Location" ("Id"),     -- nullable
    "CostCenter"          TEXT                                      -- nullable
);

CREATE TABLE "Employee" (
    "Id"               INTEGER PRIMARY KEY,
    "Name"             TEXT NOT NULL,
    "Title"            TEXT NOT NULL,
    "ManagerId"        INTEGER REFERENCES "Employee" ("Id"),        -- null for the CEO
    "DepartmentId"     INTEGER NOT NULL REFERENCES "Department" ("Id"),
    "HireDate"         DATE NOT NULL,
    "TerminationDate"  DATE,                                        -- nullable
    "Salary"           NUMERIC(12, 2) NOT NULL,
    "BonusPct"         NUMERIC(5, 2),                               -- nullable
    "Status"           "EmploymentStatus" NOT NULL,
    "IsRemote"         "RemoteFlag" NOT NULL DEFAULT 'No'
);

CREATE TABLE "Mentorship" (
    "MentorId"   INTEGER NOT NULL REFERENCES "Employee" ("Id"),
    "MenteeId"   INTEGER NOT NULL REFERENCES "Employee" ("Id"),
    "StartDate"  DATE NOT NULL,
    "EndDate"    DATE,                                              -- null = ongoing
    PRIMARY KEY ("MentorId", "MenteeId")
);

CREATE TABLE "SalaryHistory" (
    "Id"             INTEGER PRIMARY KEY,
    "EmployeeId"     INTEGER NOT NULL REFERENCES "Employee" ("Id"),
    "EffectiveDate"  DATE NOT NULL,
    "Salary"         NUMERIC(12, 2) NOT NULL,
    UNIQUE ("EmployeeId", "EffectiveDate")
);
