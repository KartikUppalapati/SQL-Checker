-- project_tracker: issue / project management tool
-- Shape:    many-to-many (users <-> projects via project_members, tasks <-> labels via task_labels),
--           self-references (tasks.parent_task_id -> tasks) and a self-referential many-to-many
--           (task_dependencies)
-- Naming:   snake_case, unquoted
-- Flags:    PostgreSQL ENUM types (yes_no 'yes'/'no', task_status, member_role)
-- Generic:  id / name / created_at / team_id appear in several tables

CREATE TYPE yes_no AS ENUM ('yes', 'no');
CREATE TYPE task_status AS ENUM ('todo', 'in_progress', 'blocked', 'done');
CREATE TYPE member_role AS ENUM ('owner', 'maintainer', 'contributor', 'viewer');

CREATE TABLE teams (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL
);

CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    email       VARCHAR(255) NOT NULL UNIQUE,
    team_id     INTEGER REFERENCES teams (id),                    -- nullable
    is_admin    yes_no NOT NULL DEFAULT 'no',
    created_at  TIMESTAMP NOT NULL
);

CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES teams (id),
    owner_id    INTEGER NOT NULL REFERENCES users (id),
    archived    yes_no NOT NULL DEFAULT 'no',
    due_date    DATE,                                             -- nullable
    created_at  TIMESTAMP NOT NULL
);

CREATE TABLE project_members (
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    user_id     INTEGER NOT NULL REFERENCES users (id),
    role        member_role NOT NULL,
    joined_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects (id),
    parent_task_id  INTEGER REFERENCES tasks (id),                -- null = top-level task
    name            VARCHAR(300) NOT NULL,
    status          task_status NOT NULL DEFAULT 'todo',
    priority        SMALLINT NOT NULL,                            -- 1 (highest) .. 4
    assignee_id     INTEGER REFERENCES users (id),                -- null = unassigned
    estimate_hours  INTEGER,                                      -- nullable
    created_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP                                     -- null until done
);

CREATE TABLE task_dependencies (
    task_id             INTEGER NOT NULL REFERENCES tasks (id),
    depends_on_task_id  INTEGER NOT NULL REFERENCES tasks (id),
    PRIMARY KEY (task_id, depends_on_task_id)
);

CREATE TABLE labels (
    id     INTEGER PRIMARY KEY,
    name   VARCHAR(50) NOT NULL UNIQUE,
    color  CHAR(7)                                                -- nullable, '#rrggbb'
);

CREATE TABLE task_labels (
    task_id   INTEGER NOT NULL REFERENCES tasks (id),
    label_id  INTEGER NOT NULL REFERENCES labels (id),
    PRIMARY KEY (task_id, label_id)
);

CREATE TABLE time_entries (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks (id),
    user_id    INTEGER NOT NULL REFERENCES users (id),
    hours      NUMERIC(5, 2) NOT NULL,
    logged_on  DATE NOT NULL,
    note       TEXT                                               -- nullable
);
