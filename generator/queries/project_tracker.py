SCHEMA_ID = "project_tracker"

QUERIES = [
    ("How many open (not done) tasks does each project have? Show project name and count.",
     """SELECT p.name, COUNT(*) AS open_tasks
FROM projects p
JOIN tasks t ON t.project_id = p.id
WHERE t.status <> 'done'
GROUP BY p.id, p.name""",
     ["flag_enum", "generic_names"]),

    ("List unassigned tasks in projects that are not archived.",
     """SELECT t.id, t.name
FROM tasks t
JOIN projects p ON p.id = t.project_id
WHERE t.assignee_id IS NULL AND p.archived = 'no'""",
     ["nullable", "flag_enum", "generic_names"]),

    ("Which users are members of more than two projects?",
     """SELECT u.name
FROM users u
JOIN project_members pm ON pm.user_id = u.id
GROUP BY u.id, u.name
HAVING COUNT(*) > 2""",
     ["m2m", "having"]),

    ("Which tasks depend on at least one task that is not yet done? Return the dependent task ids.",
     """SELECT DISTINCT d.task_id
FROM task_dependencies d
JOIN tasks blocker ON blocker.id = d.depends_on_task_id
WHERE blocker.status <> 'done'""",
     ["self_m2m", "flag_enum"]),

    ("For each subtask, show its name and its parent task's name.",
     """SELECT child.name, parent.name AS parent_name
FROM tasks child
JOIN tasks parent ON parent.id = child.parent_task_id""",
     ["self_join", "generic_names"]),

    ("Return the ids of all tasks nested anywhere beneath task 100 (children, grandchildren, and so on).",
     """WITH RECURSIVE tree AS (
    SELECT id FROM tasks WHERE parent_task_id = 100
    UNION ALL
    SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id
)
SELECT id FROM tree""",
     ["self_join"]),

    ("How many hours did each user log in 2024? Include users who logged nothing, showing 0.",
     """SELECT u.name, COALESCE(SUM(te.hours), 0) AS hours
FROM users u
LEFT JOIN time_entries te
  ON te.user_id = u.id AND te.logged_on >= DATE '2024-01-01' AND te.logged_on < DATE '2025-01-01'
GROUP BY u.id, u.name""",
     ["outer_join", "nullable", "dates"]),

    ("Which tasks have more hours logged than their estimate? Tasks without an estimate are excluded.",
     """SELECT t.id, t.estimate_hours, SUM(te.hours) AS logged
FROM tasks t
JOIN time_entries te ON te.task_id = t.id
WHERE t.estimate_hours IS NOT NULL
GROUP BY t.id, t.estimate_hours
HAVING SUM(te.hours) > t.estimate_hours""",
     ["having", "nullable"]),

    ("For each project, show the project name and its owner's name.",
     """SELECT projects.name, users.name
FROM projects
JOIN users ON users.id = projects.owner_id""",
     ["generic_names"]),

    ("What is the average time to complete a task, in days, for each project? Only completed tasks count.",
     """SELECT project_id, AVG(EXTRACT(EPOCH FROM completed_at - created_at) / 86400) AS avg_days
FROM tasks
WHERE status = 'done' AND completed_at IS NOT NULL
GROUP BY project_id""",
     ["dates", "flag_enum"]),

    ("Which labels have never been applied to any task?",
     """SELECT l.name FROM labels l
WHERE NOT EXISTS (SELECT 1 FROM task_labels tl WHERE tl.label_id = l.id)""",
     ["anti_join", "m2m"]),

    ("Which admin users do not belong to any team?",
     "SELECT name, email FROM users WHERE is_admin = 'yes' AND team_id IS NULL",
     ["flag_enum", "nullable"]),

    ("For each team, how many projects and how many users does it have? Show team name with both counts.",
     """SELECT tm.name,
       (SELECT COUNT(*) FROM projects p WHERE p.team_id = tm.id) AS project_count,
       (SELECT COUNT(*) FROM users u WHERE u.team_id = tm.id) AS user_count
FROM teams tm""",
     ["correlated_subquery", "generic_names"]),

    ("Who are the top 5 users by number of completed tasks assigned to them?",
     """SELECT u.name, COUNT(*) AS done_tasks
FROM tasks t
JOIN users u ON u.id = t.assignee_id
WHERE t.status = 'done'
GROUP BY u.id, u.name
ORDER BY done_tasks DESC
LIMIT 5""",
     ["flag_enum", "top_k"]),

    ("Find tasks whose name mentions 'bug' in any capitalization.",
     "SELECT id, name FROM tasks WHERE name ILIKE '%bug%'",
     ["string_match"]),

    ("Which projects are past their due date (before 2024-10-01) but still have tasks that are not done?",
     """SELECT DISTINCT p.name
FROM projects p
JOIN tasks t ON t.project_id = p.id
WHERE p.due_date < DATE '2024-10-01' AND t.status <> 'done'""",
     ["nullable", "flag_enum", "dates"]),

    ("How many tasks are in each status? Show status and count.",
     "SELECT status, COUNT(*) FROM tasks GROUP BY status",
     ["flag_enum"]),

    ("List users who are the owner of a project but are not recorded as a member of that project.",
     """SELECT u.name, p.name AS project
FROM projects p
JOIN users u ON u.id = p.owner_id
WHERE NOT EXISTS (SELECT 1 FROM project_members pm
                  WHERE pm.project_id = p.id AND pm.user_id = p.owner_id)""",
     ["anti_join", "m2m", "generic_names"]),

    ("Which labels are used on high-priority (priority 1) tasks, and on how many such tasks each?",
     """SELECT l.name, COUNT(*) AS tasks
FROM task_labels tl
JOIN labels l ON l.id = tl.label_id
JOIN tasks t ON t.id = tl.task_id
WHERE t.priority = 1
GROUP BY l.id, l.name""",
     ["m2m"]),

    ("For each project, list maintainers' names.",
     """SELECT p.name AS project, u.name AS maintainer
FROM project_members pm
JOIN projects p ON p.id = pm.project_id
JOIN users u ON u.id = pm.user_id
WHERE pm.role = 'maintainer'""",
     ["m2m", "flag_enum", "generic_names"]),
]
