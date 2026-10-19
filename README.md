# SQL-Checker: Deterministic SQL Evaluator

Synthetic evaluation suite for a natural-language-to-SQL CLI, plus a scoring harness that decides
whether a predicted PostgreSQL query is equivalent to the ground truth **without executing either
query and without any LLM calls**.

## Layout

```
score                   # scoring harness CLI (entry point -> sqleval/cli.py)
requirements.txt        # harness dependencies: sqlglot, pglast
sqleval/                # harness implementation
  validate.py           #   syntax gate (PostgreSQL's own parser via pglast)
  semantic.py           #   errors PostgreSQL raises before running a query (types, placement, names)
  schema.py             #   DDL -> catalog (columns, NOT NULL, PK/UNIQUE, FKs, enum labels)
  canonical.py          #   equivalence-preserving canonicalization
  equivalence.py        #   compare(): gate -> fast path -> canonical comparison -> partial score
  cli.py                #   batch scoring, parallelism, summary report
  eval_dev.py           #   dev tool: list false positives / negatives on a labeled file
generator/              # programs used to generate the dataset (requirements in generator/requirements.txt)
  check_postgres.py     #   optional: checks every label against a real PostgreSQL server
dataset/
  schemas/<id>.sql      # one DDL file per schema
  queries.jsonl         # NL questions + target SQL
  pairs.jsonl           # labeled (ground_truth, predicted, equivalent) pairs
tests/
session/                # exported LLM/agent session history
```

## What next

I would focus on expanding coverage area to cases that require reasoning beyond what can be generalized without running the sql. Something like TypeSafe's Jev could be used for this. I'd look at previous cases where we got a prediction wrong and build out more test cases to cover those areas. Then I'd expand to more sql languages like mysql or sqlite.


## Usage

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./score --schemas dataset/schemas --pairs dataset/pairs.jsonl --output results.jsonl
python -m unittest discover -s tests                       # rewrite boundary tests + the PostgreSQL-checked gate tests
python -m sqleval.eval_dev --show fn                       # inspect misclassified labeled pairs
```

## Dataset design

All schemas and queries are PostgreSQL. The dataset has 9 schemas and 237 natural-language
questions, each with a target SQL query (22–28 questions per schema).

### How schemas were varied

Each schema is a deliberate combination of four variation axes. Each axis value appears in at
least two schemas, so no single convention is tied to a single domain. The one exception is native
`BOOLEAN` flags, used only by `rideshare`, the schema added for the column types the others lack:

| schema | shape | naming | quoting | binary flags |
|---|---|---|---|---|
| `campus` | many-to-many (students↔courses, courses↔instructors) + self-ref M:N (prerequisites) | snake_case | unquoted | `SMALLINT` 0/1 |
| `org_chart` | self-referential hierarchies (Employee→manager, Department→parent) + self-ref M:N (mentorship) | PascalCase | **quoted** | `ENUM` (`'Yes'/'No'`) |
| `social_graph` | self-ref M:N (follows) + self-ref reply threads + M:N (hashtags) | camelCase | **quoted** | `VARCHAR` `'on'/'off'` |
| `sales_star` | **star**: one fact, flat single-hop dimensions | snake_case (`fact_`/`dim_`, `*_key`) | unquoted | `SMALLINT` 0/1 |
| `sales_snowflake` | **snowflake**: Product→Subcategory→Category, Store→City→Country→Region | PascalCase | unquoted (folds to lowercase) | `ENUM` (`'active'/'inactive'`) |
| `library` | M:N (books↔authors, books↔genres) + self-ref (sequels, genre tree) | camelCase | unquoted (folds to lowercase) | `VARCHAR` `'on'/'off'` |
| `web_analytics` | **star** with two fact tables sharing conformed dimensions | camelCase (`fact`/`dim`) | **quoted** | `SMALLINT` 0/1 |
| `project_tracker` | M:N (users↔projects, tasks↔labels) + self-ref (subtasks) + self-ref M:N (dependencies) | snake_case | unquoted | `ENUM` (`yes_no`, `task_status`) |
| `rideshare` | **star** (trips → riders, drivers, vehicles, cities) + self-ref (rider referrals) | snake_case | unquoted | `BOOLEAN`, some nullable |

- **Shape.** Many-to-many junction tables, often with a composite primary key. Self-referential
  foreign keys, which call for self-joins and recursive CTEs. Self-referential many-to-many
  tables. Star schemas, where a fact table reaches each dimension in one hop. Snowflake schemas,
  where a query needs 3–5 joins to reach a dimension attribute.
- **Naming and quoting.** Each convention is used both quoted and unquoted, because PostgreSQL
  treats the two very differently:
  - In quoted schemas (`"Employee"."ManagerId"`, `"postLikes"."userId"`), names are
    case-sensitive and every reference has to be quoted.
  - In unquoted PascalCase or camelCase schemas (`DimProduct`, `bookAuthors`), names fold to
    lowercase, so `DimProduct`, `dimproduct` and `DIMPRODUCT` all refer to the same table.
- **Generic column names.** Columns like `id`, `name`, `"Name"`, `created_at`, `team_id` and
  `"dateKey"` are repeated across tables. Queries therefore have to qualify columns
  (`courses.name` vs `departments.name`), and a query that picks the wrong table's `name` is
  still valid SQL but gives a different answer.
- **Nullable columns.** Each schema has columns that are nullable on purpose: optional
  attributes, a self-reference that is NULL at the top of a hierarchy, a NULL meaning "not
  returned yet" or "not graded yet", optional foreign keys and nullable measures. Queries
  exercise `IS NULL`, `COALESCE`, `COUNT(col)` vs `COUNT(*)`, outer joins, and NULL groups in
  `GROUP BY`.
- **Binary state.** Two-valued flags are stored four different ways: `SMALLINT` with
  `CHECK (x IN (0, 1))`, `VARCHAR` with `CHECK (x IN ('on', 'off'))`, PostgreSQL `ENUM` types, and
  native `BOOLEAN`. Some `BOOLEAN` flags are nullable, so `x IS NOT TRUE`, `NOT x` and
  `COALESCE(x, false)` differ.
- **Column types.** Besides integers, `NUMERIC`, text, `DATE` and `TIMESTAMP`, `rideshare` has
  `TIMESTAMPTZ` instants in cities with their own time zones (so `AT TIME ZONE`, the session time
  zone and offsets matter), intervals, and `DOUBLE PRECISION` / `REAL` measures (whose sums and
  rounding are not exact).

### Queries

`dataset/queries.jsonl` records have `id` (`<schema>_<nnn>`), `schema_id`, `question`, `sql` and
`tags`. Tags come from three sources:
- Derived from the SQL's syntax tree: `join`, `multi_join`, `aggregation`, `having`, `subquery`,
  `cte`, `recursive_cte`, `window`, `set_op`, `order_by`, `limit`, `exists`, `distinct`,
  `null_handling`.
- Hand-assigned: `self_join`, `m2m`, `anti_join`, `outer_join`, `full_join`, `integer_division`,
  `generic_names`, `nullable`, `three_valued`, `flag_int`/`flag_onoff`/`flag_enum`/`flag_bool`,
  `distinct_on`, `top_k`, `lateral`, `window_frame`, `value_window` (`first_value`, `last_value`,
  `nth_value`), `distribution_window` (`ntile`, `cume_dist`, `percent_rank`), `ordered_agg`
  (`string_agg`/`array_agg … ORDER BY`), `ordered_set_agg` (`percentile_cont`, `mode`),
  `bool_agg`, `grouping_sets`, `grouping_func`, `gap_fill` (`generate_series`), `values_list`,
  `string_func`, `concat_null`, `regex`, `greatest_least`, `range`, `dates`, `date_format`,
  `timestamptz`, `time_zone`, `interval`, `float`.
- Inherited from the schema: `shape:*`, `naming:*`, `quoting:*`, `flags:*`.

The questions and SQL were written with an LLM agent (see `session/`) as Python data in
`generator/queries/<schema>.py`. `generator/build_queries.py` validates them and writes the
JSONL file:

```bash
pip install -r generator/requirements.txt   # pglast: PostgreSQL's real parser (generator-only)
python -m generator.build_queries
```

The build fails, and `queries.jsonl` is not written, if any of these checks fail:
1. Every schema file parses with `pglast` (libpg_query, the parser from PostgreSQL itself).
2. Every target is exactly one `SELECT` statement and parses with `pglast`.
3. Every table and column a target references resolves against its schema under PostgreSQL
   identifier folding (sqlglot `qualify`). This catches unquoted references to quoted
   camelCase columns, ambiguous generic columns, and typos.

These checks are static. `generator/check_postgres.py` (below) also runs every target and
prediction on a real PostgreSQL server.

### Labeled pairs

`dataset/pairs.jsonl` holds 3,193 pairs: 1,508 equivalent and 1,685 not. Each pair uses the
required fields (`id`, `schema_id`, `ground_truth`, `predicted`, `equivalent`). For analysis,
each pair also records `query_id`, `source`, `category` and a `note` explaining the label; the
harness ignores these. Every query in `queries.jsonl` contributes pairs from three sources:

| source | equivalent | not equivalent | what it is |
|---|---|---|---|
| `hand` | 563 | 800 | About 6 hand-written predictions per query, each with a one-line reason for its label |
| `auto` | 945 | 648 | Mutations of the target generated from its syntax tree (`generator/mutations.py`) |
| `malformed` | 0 | 237 | One malformed prediction per query |

**Hand-written pairs** (`generator/pairs/<schema>.py`) target the cases a string or AST diff
gets wrong.
- *Equivalent but different:*
  - Rewriting `NOT EXISTS` as a `LEFT JOIN … IS NULL` or as `NOT IN` over a NOT NULL column.
  - `EXCEPT`/`GROUP BY` on a UNIQUE NOT NULL column.
  - A `LEFT JOIN` along a NOT NULL foreign key, which is the same as an `INNER JOIN`.
  - Scalar subqueries over foreign keys.
  - `<> 'No'` on a two-value NOT NULL enum.
  - `ROW_NUMBER` vs `DISTINCT ON`.
  - A redundant `IS NOT NULL` before a comparison or a scalar `AVG`.
  - `COUNT(*) - COUNT(col)`, `ROWS 6 PRECEDING`, `FETCH FIRST`.
  - `UNION` vs `UNION ALL` when the rows are provably distinct, `DISTINCT` over `UNION ALL`,
    `ROLLUP`/`GROUPING SETS` vs a `UNION ALL` total row, and set operations vs `OR`-ed semi-joins.
  - `LIMIT 1` on an aggregate that returns one row, `OFFSET 0`, `EXCEPT ALL` over unique rows.
  - `NOT IN`/`<> ALL` with NULLs filtered out, and a `NULL` inside an `IN` list.
  - PostgreSQL syntax a model may emit: `LATERAL` lookups and top-N, `RIGHT`/`CROSS JOIN`,
    `= ANY (ARRAY[...])`, row-value comparisons, named `WINDOW`s, `LAG(x, 1)`, explicit default
    `NULLS FIRST/LAST`, `GROUPING SETS`/`CUBE`, `unnest`/`LATERAL VALUES` unpivots, date `+ INTERVAL`.
  - Facts from types and keys: enum declaration order (`status < 'done'`), integer bounds on
    counts, `AVG` as `SUM / COUNT` over a NOT NULL column, `RANK` vs `ROW_NUMBER` when the
    ordering is unique, `WITH TIES` with a unique last sort key, `NULLIF` where the target is
    already NULL.
  - Booleans and three-valued logic: `x = TRUE`, `'yes'`, `IS TRUE` on a NOT NULL flag or in
    `WHERE`, `IS NULL OR NOT x` as `IS NOT TRUE`, `COALESCE(x, false)`, `every`, `NOT bool_or(NOT x)`.
  - Time: the same instant at another UTC offset, `AT TIME ZONE 'UTC'` comparisons, `ts::date = d`
    as a session-local range, `timezone()`/`date()`, `a > b + INTERVAL '30 minutes'`,
    `INTERVAL '0.5 hours'`, `EXTRACT(EPOCH FROM …)`.
  - Functions: `CONCAT` of never-NULL arguments as `||`, `trim`/`lower` order, `btrim`,
    `GREATEST` as a `CASE`, `GROUPING(a, b)` as a bit mask, `nth_value(x, 1)`, `last_value` over the
    whole partition, `BETWEEN SYMMETRIC`, `NATURAL JOIN`/`USING`, `VALUES` row order, string
    literals for numbers (`LIMIT '5'`), `CAST(int AS NUMERIC)`.
- *Near-misses:*
  - `NOT IN` or `<> ALL` over a nullable column (the `ManagerId` trap), a `NULL` inside a `NOT IN`
    list, and `COALESCE(col, 0)` used to dodge the NULL.
  - `UNION ALL` where duplicates exist, `UNION` where the target keeps them, and a recursive CTE
    switched to `UNION ALL` over a graph with several paths.
  - `LIMIT` without `ORDER BY`, and off-by-one `LIMIT`s and comparisons (`>` vs `>=`).
  - Quoted identifiers whose case does not match the DDL (`"Name"` vs `name`).
  - `COUNT(*)` vs `COUNT(col)` after an outer join.
  - Integer division (`100 * x / n`, `/ 1000`).
  - A date filter placed in `WHERE` instead of `ON` of a `LEFT JOIN`.
  - Double `LEFT JOIN` fan-out.
  - `DISTINCT`/`INTERSECT` merging rows that share a name, and grouping by a non-unique `name`.
  - Case-sensitive enum or string literals, and `ILIKE` vs `=`.
  - `NULLS FIRST` under `DESC … LIMIT`.
  - `RANK` vs `ROW_NUMBER`.
  - A missing tie-break before `LIMIT`.
  - The target has an `ORDER BY` but the prediction has none.
  - Generic `name`/`country` columns read from the wrong table.
  - Columns with no constraint linking them, such as `quarter` vs `month IN (10, 11, 12)`.
  - `FETCH … WITH TIES`, `ROLLUP`/`CUBE` total rows, `FULL JOIN`, `LEFT JOIN LATERAL`.
  - Window frames: default cumulative frame, `ROWS` vs `RANGE … INTERVAL`, missing `PARTITION BY`.
  - Empty-input and division-by-zero differences (`NULLIF`, `SUM(1)`, `COUNT(*) FILTER`).
  - Values a CHECK constraint rules out (`flag <> 0` vs `flag = 1`): CHECK constraints are not
    part of the equivalence definition, so these are labeled not equivalent.
  - Functions from other dialects (`IFNULL`, `NVL`, `ISNULL(a, b)`, `DATEDIFF`), labeled invalid.
  - Queries that parse but that PostgreSQL rejects, labeled invalid: an output alias in `WHERE`,
    `HAVING` or the same select list (a MySQL habit); an ungrouped column; an aggregate in
    `WHERE` or `GROUP BY` (also via `GROUP BY 2`); nested aggregates; a window function in
    `WHERE`/`HAVING` or without `OVER`; a non-boolean condition (`WHERE is_active` on a
    `SMALLINT`); type mismatches (`varchar = integer`, `date >= 2020`, `COALESCE(email, 0)`,
    `interval > 30`); `SUM`/`AVG` of text or booleans, `MIN`/`MAX` of booleans, `string_agg` of
    an integer, `round(double precision, 1)`; literals a type cannot read (`'2020.0'` for an
    integer, a misspelt enum label); `LIKE` on an enum; `UNION` column count or type mismatches;
    a subquery with too many columns; `DISTINCT ON` not leading the `ORDER BY`; an `ORDER BY`
    position past the select list; a derived table that needs `LATERAL`; a table referred to by
    name after it was aliased; ambiguous columns.
  - Traps from the new functions: `last_value` with the default frame, `DENSE_RANK` vs `RANK`,
    `ntile` vs `ceil(4.0 * row_number / n)`, `percent_rank` vs `cume_dist`, `CONCAT` vs `||` with
    NULLs, `string_agg` without `ORDER BY`, `array_agg` keeping NULLs, `mode()` tie order,
    `percentile_disc` vs `_cont`, `generate_series` gap-filling with `COUNT(*)`, `FULL JOIN` vs
    `LEFT JOIN`, `OFFSET`, `GREATEST` written as a `CASE` that lets NULL through, `floor` vs
    integer division on negatives, `[[:alpha:]]` vs `[A-Za-z]`, `SUM` over an empty table vs
    `COUNT`, the session time zone vs a fixed offset, an exact `NUMERIC` sum of a float column.

**Automatic pairs** are chosen per query with an RNG seeded by the query id, so the build is
deterministic.
- *Equivalent rewrites:*
  - Cosmetic: case changes, reformatting, a comment or trailing semicolon, `!=`.
  - Structural: alias renaming, reordered conjuncts and join-equality sides, inner joins
    rewritten as implicit comma joins, `COUNT(1)`, and `ORDER BY 1` added when the target has
    no ordering.
- *Breaking mutations:*
  - Flip a comparison against a literal, change a literal (another label of an enum, the next
    day of a date), drop a literal filter.
  - Swap or drop output columns (positions in `GROUP BY`/`ORDER BY` follow a swap).
  - Drop `DISTINCT`, swap `SUM`↔`AVG` or `MIN`↔`MAX`, change `LEFT` to `INNER` join.
  - Flip `IS [NOT] NULL`, turn `100.0` into `100` (integer division), flip or drop
    `ORDER BY`, change `LIMIT`.
  - Remove quotes from a quoted camelCase name, or add quotes to an unquoted PascalCase one.
    Both produce an invalid query.

Each query gets at most one cosmetic positive and at most one column-shape or quoting negative,
so the automatic pairs are mostly structural and semantic changes.

**Malformed predictions** rotate through `null`, `""`, a fenced markdown block, two statements,
prose, truncated SQL, MySQL backticks, a `DELETE`, and a misspelled `SELECT`. Under the spec's
rule that a prediction must be exactly one valid PostgreSQL query, all of these are labeled
not equivalent.

```bash
python -m generator.build_pairs     # after build_queries
```

The build refuses to write the file if any of these fail:
- Every prediction labeled equivalent parses, resolves against the schema and passes the
  harness's validity gate.
- Hand-written negatives pass the gate unless the note marks them `INVALID:`, and those must
  fail it.
- Cosmetic rewrites leave the PostgreSQL parse tree unchanged.
- Breaking mutations change the parse tree.
- No pair is duplicated.

A harness that always answers "not equivalent" scores 52.8% on this set.

### Checked against PostgreSQL

The harness never runs SQL, but the dataset's labels were checked on PostgreSQL 16.2 by
`generator/check_postgres.py` (an embedded server from the `pgserver` package):

```bash
pip install -r generator/requirements-postgres.txt
python -m generator.check_postgres --seeds 40
```

- **Validity.** Every target and prediction is planned with `EXPLAIN`. PostgreSQL rejects exactly
  the predictions labeled invalid (0 disagreements in 3,193 pairs).
- **Results.** Each schema is filled with random data 40 times. The data respects types, keys,
  NOT NULL and foreign keys, with CHECK constraints dropped as in the equivalence definition, and
  each fill uses a different session time zone. Every pair labeled equivalent returns the same
  multiset of rows as its target on all 40 databases. Most pairs labeled not equivalent (1,078 of
  1,234 runnable ones) differ on at least one. The rest need data that random rows rarely produce,
  such as a tie, a `#` in a post body or a negative duration.

This audit found and fixed six hand-written labels: queries PostgreSQL rejects that were labeled
as valid non-equivalents. It also found two automatic mutations that produced invalid SQL:
`change_literal` misspelt enum labels, and `swap_columns` left `GROUP BY 1` pointing at an
aggregate. `tests/test_gate.py` keeps 215 queries whose verdict comes from PostgreSQL.

## How scoring works

The harness never executes SQL and never calls a model. It proves equivalence by rewriting both
queries into a canonical form, using only rewrites that preserve the result on **every** database
satisfying the schema. A pair matches when the two canonical forms are identical. When a rewrite's
precondition can't be proven from the schema, the rewrite is skipped. So the harness is
deliberately one-sided: a missed equivalence becomes a false negative, but it never claims a
match it can't justify.

For each pair (`sqleval/equivalence.py`):

1. **Validity gate.** The prediction is parsed with PostgreSQL's own grammar (libpg_query via
   `pglast`). It must be exactly one statement and must be a query. Null, empty, prose,
   markdown-fenced, multi-statement and DML predictions are rejected here, and so are calls to
   functions from other dialects that PostgreSQL does not have (`IFNULL`, `NVL`, `DATEDIFF`, ...;
   sqlglot would otherwise map some of them onto `COALESCE`). Rejected predictions get
   `match: false, score: 0`.
2. **Name resolution against the schema.** sqlglot `qualify` resolves every table and column under
   PostgreSQL identifier rules: unquoted names fold to lowercase, quoted names are case-sensitive.
   The prediction is also rejected for the errors PostgreSQL raises before it runs a query
   (`sqleval/semantic.py`, `_check_grouping`). Each check is lenient where it is unsure: it rejects
   only what PostgreSQL certainly rejects, and `tests/test_gate.py` holds it to PostgreSQL's own
   verdicts.
   - *Names:* unknown tables or columns, ambiguous columns, a table referred to by its name after
     it was aliased, duplicate aliases, a derived table that reads a sibling without `LATERAL`, a
     `USING` column missing on one side, and output aliases where PostgreSQL does not see them
     (`WHERE`, `HAVING`, `ON`, the same select list, inside an `ORDER BY`/`GROUP BY` expression).
     These run before sqlglot's `qualify`, which would silently expand an alias in `WHERE`.
   - *Grouping and placement:* a column neither grouped nor covered by a grouped primary key
     (UNIQUE keys don't count), also when read by a subquery; aggregates in `WHERE`, `ON` or
     `GROUP BY`; nested aggregates; window functions in `WHERE`, `HAVING`, `GROUP BY`, inside an
     aggregate or another window, or ranking/value functions without `OVER`.
   - *Shape:* `UNION`/`INTERSECT`/`EXCEPT` branches with different column counts, an `ORDER BY`
     on a set operation that is not an output column, `ORDER BY`/`GROUP BY` positions past the
     select list, `IN`/scalar subqueries with the wrong number of columns, `DISTINCT ON` that does
     not lead the `ORDER BY`, `SELECT DISTINCT … ORDER BY` something not selected, a column in
     `LIMIT`.
   - *Types* (inferred from the catalog: numeric, string, date/time, interval, boolean, each enum):
     operands no operator accepts (`varchar = integer`, `date > 2020`, `interval > 30`,
     `boolean = 1`); a non-boolean `WHERE`/`ON`/`HAVING`/`CASE WHEN`/`AND`/`NOT` operand;
     arithmetic on text, booleans or enums, `date + date`; `LIKE` on a number, date or enum;
     `CASE`/`COALESCE`/`UNION`/`IN` branches whose types cannot be matched; `SUM`/`AVG` of text,
     booleans, enums or dates; `MIN`/`MAX` of booleans; `bool_and` of a non-boolean;
     `string_agg` of a number; `round`/`trunc(double precision, n)`; and a string literal its
     type cannot read (`'2020.0'` for an integer, `'2024-13-01'`, `'maybe'` for a boolean, a
     label that is not in the enum).
   Before any of this, the PostgreSQL syntax that sqlglot misreads is handled. `BETWEEN SYMMETRIC`
   would otherwise be read as `BETWEEN <a column named symmetric>`. `IS [NOT] UNKNOWN` doesn't
   parse at all. `NATURAL JOIN` would become a join with no condition, matching a cross join.
   And `INTERVAL '0.5 hours'` would become `'5 hours'`, and `'-1 day'` would lose its sign, since
   sqlglot splits interval strings with an unanchored regex. The PostgreSQL aggregates that
   sqlglot parses as plain functions (`mode`, `every`, ...) are marked as aggregates.
3. **Fast path.** If the two parse trees are identical once source positions are ignored, the
   queries match.
4. **Canonicalization** (`sqleval/canonical.py`). Each rewrite below states the condition that
   makes it sound:
   - *Names and structure:* parentheses, `FETCH FIRST`/`LIMIT` (`WITH TIES` is kept distinct),
     positional and alias references in `GROUP BY`/`ORDER BY`, `*` expansion, `NATURAL JOIN` →
     `USING (<shared columns>)` → `ON`. CTEs become derived tables (when read
     more than once, only if deterministic: no `random()`, `nextval`, ...). Aliases are made
     globally unique. Derived-table columns nothing reads are dropped (not under `DISTINCT`), and
     the rest are sorted and renamed by position (only their names are read). `OVER w` is replaced
     by the `WINDOW w` definition (an unresolved name is not canonicalized).
   - *LATERAL, derived tables and unpivots:*
     - `JOIN LATERAL (q) x`: a plain `q` becomes a join with q's tables and WHERE; a `LEFT JOIN
       LATERAL` over one table whose outputs are its columns becomes `LEFT JOIN T ON …`; an
       ungrouped aggregate `q` (exactly one row) becomes scalar subqueries; `q … ORDER BY o LIMIT 1`
       from a single keyed outer table becomes `DISTINCT ON (<its key>) … ORDER BY <key>, o`;
       `q … LIMIT n` over one table correlated by one equality `T.f = <outer column>` becomes a
       join to `ROW_NUMBER() OVER (PARTITION BY T.f ORDER BY o) <= n`; and the previous (next) row by a
       NOT NULL key `(k.., d)` of the outer table itself becomes `LAG` (`LEAD`), only when the outer
       query reads every row of that table.
     - A derived table that only filters and projects is merged into its outer query (as the FROM
       item or an inner join; on the NULL side of a `LEFT JOIN` when it reads one table and outputs
       its columns, its WHERE moving into ON).
     - `o LEFT JOIN (SELECT k, AGG … GROUP BY k) t ON t.k = o.c`, read only through its aggregates
       (counts as `COALESCE(t.n, 0)`), → `LEFT JOIN b` grouped by the outer keys; also under one
       outer aggregate. `COUNT(t.x)` over `(SELECT DISTINCT g.., x …) t GROUP BY t.g..` →
       `COUNT(DISTINCT x)`.
     - `SELECT DISTINCT … FROM (A UNION ALL B) x …` → the `UNION` of the query over each branch.
     - `SELECT [DISTINCT] unnest(ARRAY[a, b])` and `CROSS JOIN LATERAL (VALUES (a), (b)) v(c)` →
       `UNION [ALL]` of one branch per element.
     - `AGG(x) OVER ()` in an ungrouped SELECT → the scalar subquery over the same FROM / WHERE;
       `ROW_NUMBER() OVER (ORDER BY O) <= n` (no partition) in a derived table → `ORDER BY O LIMIT n`
       when the outer order is O itself or does not count.
   - *Row limits and set operations:*
     - `OFFSET 0` is dropped, and `LIMIT n` (n ≥ 1) on an ungrouped aggregate (exactly one row).
     - `SELECT [DISTINCT] <all columns> FROM (<set operation>) t` is the set operation; with
       `DISTINCT` its `UNION ALL` chain becomes `UNION`.
     - `L EXCEPT R`, with L's columns never NULL → `SELECT [DISTINCT] L … WHERE NOT EXISTS (R …
       AND R = L)`; `DISTINCT` is omitted, and `EXCEPT ALL` is accepted, when L's rows are
       provably distinct.
     - A deduplicated set of values that each name one row of `T` by its key (`T.k` itself, or a
       NOT NULL foreign key to it) → `SELECT t.k FROM T t WHERE …` with a semi-join per source:
       a `UNION` of such branches (or a provably duplicate-free `UNION ALL`), `SELECT DISTINCT fk`,
       and `COUNT(DISTINCT fk)` (→ `COUNT(*)`; NULLs need not be excluded there).
     - Plain branches of a deduplicating `UNION`/`INTERSECT`/`EXCEPT` are marked `DISTINCT`.
     - A grouped derived table joined to keyed tables, or alone, is merged into its outer query:
       one `GROUP BY` (plus the other tables' primary keys), outer conditions on its aggregates in
       `HAVING`. Window functions in the outer query are kept (they run after grouping).
     - First row per group: `… WHERE rn = 1` over `ROW_NUMBER() OVER (PARTITION BY P ORDER BY O)`
       → `DISTINCT ON (P) … ORDER BY P, O`; and `h.c = (SELECT MAX(s.c) … WHERE s.f = X)`, with
       a NOT NULL unique key inside `(f, c)` and `c` NOT NULL → `DISTINCT ON (<other tables'
       keys>) … ORDER BY …, h.c DESC`. Since `DISTINCT ON`'s ORDER BY also orders the output,
       neither applies to an unordered outermost query when row order is compared.
     - `ORDER BY` without `LIMIT`/`OFFSET` that cannot affect the result is removed before these
       rewrites, as well as at the end.
   - *Expressions:*
     - Comparisons are oriented consistently, `NOT` is pushed into comparisons, `BETWEEN` is
       expanded, and single-value `IN` becomes `=`. `= ANY (subquery)` is `IN`, `<> ALL
       (subquery)` is `NOT IN`.
     - `NULL` items of an `IN` list are dropped in WHERE / HAVING / ON conditions reached only
       through `AND`/`OR` (where NULL and FALSE both reject the row), and `NULL` arguments of
       `COALESCE` always.
     - `date_trunc('year', a) = date_trunc('year', b)` becomes `EXTRACT(YEAR …)` equality.
     - A window frame with only a start ends at `CURRENT ROW`.
     - `IS [NOT] DISTINCT FROM NULL` becomes `IS [NOT] NULL`; `CASE WHEN x IS NULL …` becomes
       `COALESCE`.
     - Exact SUM arithmetic, for integer/NUMERIC terms that are never NULL (never floats):
       products are multiplied out inside `SUM` and `SUM(a ± b)` becomes `SUM(a) ± SUM(b)`.
       `SUM(CASE WHEN p THEN x END)` is `SUM(x) FILTER (WHERE p)`. Where an empty input cannot
       show (a plain `GROUP BY`, or next to a SUM/AVG/MIN/MAX that is NULL then anyway):
       `SUM(CASE WHEN p THEN x ELSE 0 END)` → `COALESCE(SUM(x) FILTER (WHERE p), 0)` (`COUNT(*)
       FILTER` for x = 1) and `SUM(COALESCE(x, 0))` → `COALESCE(SUM(x), 0)`.
       `AVG(<integer column>::numeric)` is `AVG(<integer column>)`.
     - `AVG(t.s)` over `(SELECT SUM(x) AS s … GROUP BY k) t` → `SUM(x) / COUNT(DISTINCT k)`
       (k and x never NULL; an integer x is cast to numeric like AVG's result). An ungrouped
       aggregate SELECT whose output mixes in a scalar subquery is written with each aggregate as
       its own scalar subquery.
     - `COUNT(1)` becomes `COUNT(*)`. After the per-SELECT rewrites, `COUNT(c)` is written
       `COUNT(*) FILTER (WHERE c IS NOT NULL)` and `COUNT(*) - COUNT(c)` is `COUNT(*) FILTER
       (WHERE c IS NULL)`; in an ungrouped SELECT whose aggregates share one filter, the filter
       moves to `WHERE`.
     - `~* 'lit'`, `lower(x) LIKE` and `ILIKE` are unified; `strpos(s, 'p') > 0` and
       `position('p' IN s) > 0` are `s LIKE '%p%'` (p without wildcards); `char_length` is `length`.
     - `= ANY (ARRAY[…])` / `<> ALL (ARRAY[…])` are `IN` / `NOT IN` lists; `(a, b) = (x, y)` is
       `a = x AND b = y` (`<>`: the `OR`); `IS [NOT] DISTINCT FROM` between never-NULL operands is
       `=` / `<>`; `COALESCE(x, c) = c` is `x IS NULL OR x = c`; a simple `CASE x WHEN v` is a
       searched `CASE WHEN x = v`.
     - `(SELECT COUNT(*) …) = 0` is `NOT EXISTS`, `> 0` is `EXISTS`; `(SELECT SUM(x) …) IS NOT NULL`
       with x never NULL is `EXISTS`. `a - b` compared with 0 is `a` compared with `b` for NUMERIC
       (or counts: no overflow). `LAG`/`LEAD` drop a default offset of 1; ranking functions drop
       their frame. Numeric literals in comparisons are respelled (`1e4` = `10000`).
     - `x LIKE '%'` is `x IS NOT NULL` in WHERE / HAVING / ON conditions.
     - Booleans: `x = TRUE` is x and `x = FALSE` is `NOT x` (the same NULL); `IS [NOT] DISTINCT FROM
       TRUE` is `IS [NOT] TRUE`; `COALESCE(x, FALSE)` is `x IS TRUE`; `x IS NULL OR NOT x` is
       `x IS NOT TRUE` (and the `IS NOT NULL AND` duals); `IS TRUE`/`IS FALSE` on a never-NULL x is
       x / `NOT x`; in WHERE / HAVING / ON and `CASE WHEN` (through AND/OR) `x IS TRUE` is x;
       `CASE WHEN NOT c THEN a ELSE b END` swaps when c is never NULL; `NOT (a OR b)` is
       `NOT a AND NOT b`; `every` is `bool_and`; `NOT bool_or(NOT x)` is `bool_and(x)`; a never-NULL
       boolean cast to `int` is `CASE WHEN x THEN 1 ELSE 0 END`.
     - Strings and functions: `CONCAT` of never-NULL arguments is `||`; `btrim(x)` is `trim(x)`, and
       trimming spaces commutes with `lower`/`upper`; `GROUPING(a, b, …)` is the bit mask
       `2^(n-1) * GROUPING(a) + …`; `GREATEST`/`LEAST` arguments are a set, `COALESCE(x, y)` next
       to y inside them is x, and `CASE WHEN x > c THEN x ELSE c END` (c never NULL) is
       `GREATEST(x, c)`; the rows of a `VALUES` list in FROM are a bag; an unquoted function name
       folds to lowercase.
     - Value windows: `nth_value(x, 1)` is `first_value(x)`; `first_value` over any frame from
       `UNBOUNDED PRECEDING` to the current row or later is the default frame; `last_value` over a
       frame ending at `UNBOUNDED FOLLOWING` is `first_value` in the reversed order when that order
       includes a key of the window's only table.
     - `SUM(x) / COUNT(x)` (`::numeric` first for integers; `COUNT(*)` when x is never NULL;
       `NULLIF(count, 0)` allowed) is `AVG(x)`. `X / NULLIF(C, 0)` is `X / C` when C counts the input's
       rows and X is strict in a SUM / AVG / MIN / MAX of it (or the SELECT has a plain `GROUP BY`).
       `SUM(CASE WHEN <null test> THEN 1 ELSE -1 END)` is a difference of two filtered counts where
       an empty input cannot show.
     - An aggregate window with `ORDER BY` and no frame uses `RANGE … CURRENT ROW`, which is `ROWS`
       when its partition and order keys cover the SELECT's `GROUP BY` keys.
     - `CASE`: `COALESCE(CASE …, d)` with literal results folds d into the ELSE; a `CASE` over all
       labels of a NOT NULL enum turns its last test into ELSE; a `CASE` whose tests compare one
       column with numeric literals is rebuilt from the step function it computes (branch order and
       spelling no longer matter).
   - *Type-aware literals:*
     - An untyped string compared with a DATE or TIMESTAMP column takes the column's type; next to a
       numeric column it is that number (`= '5'`, `IN ('1')`, `COALESCE(x, '0')`, `LIMIT '5'`); next
       to a boolean column it is that boolean (`'t'`, `'yes'`, `'off'`). `CAST(<integer> AS NUMERIC)`
       compared with a number is the integer. `BETWEEN SYMMETRIC` with literal bounds is `BETWEEN`
       the smaller and the larger; otherwise it is PostgreSQL's `OR` of both orders.
     - `TIMESTAMPTZ` literals become instants: a literal with an offset (`+02`, `Z`) is the UTC
       instant; one without, a `TIMESTAMP` or a `DATE` is the session-local time (a date at
       midnight), so `'2024-03-01'`, `TIMESTAMPTZ '2024-03-01'` and `DATE '2024-03-01'` agree and
       `'2024-03-01 00:00+00'` does not. `ts::date` compared with a date is the local-midnight
       range. `ts AT TIME ZONE 'UTC'` compared with a timestamp literal is ts compared with that UTC
       instant. `timezone(z, ts)` is `ts AT TIME ZONE z`, `date(x)` is `x::date`.
     - Intervals: compared with a timestamp difference, an interval literal of seconds…weeks is its
       length (`'0.5 hours'` = `'30 minutes'`; comparison counts a day as 24 hours); `a op b + c`
       (c a time-only interval, no days) is `a - b op c`; `EXTRACT(EPOCH FROM a - b) op n` is
       `a - b op INTERVAL 'n seconds'`. Day and month intervals are kept as written: added to a
       `TIMESTAMPTZ` they follow the calendar across daylight-saving changes.
     - Bounds over discrete types are tightened: `x <= 5` → `x < 6` for integers, by one day
       for DATE and one microsecond for TIMESTAMP.
     - `EXTRACT(YEAR FROM d) = y` becomes a date range.
     - Enum comparisons are checked against the type's labels, and `<> 'No'` on a two-label enum
       becomes `= 'Yes'`. `<` / `<=` against a label use the declaration order (an `IN` list),
       `enum::text LIKE / ILIKE / = 'pattern'` is the set of labels it matches, and `enum::text`
       `<>` or `IN` labels is the enum test.
     - Integer-valued aggregates (`COUNT`, `SUM`/`MIN`/`MAX` of integers) get discrete bounds
       (`COUNT(*) >= 4` is `COUNT(*) > 3`).
     - Date arithmetic on literals is folded (`DATE '2024-01-31' + INTERVAL '1 month'` →
       `TIMESTAMP '2024-02-29'`, months first, clamped); a DATE column compared with a timestamp
       literal compares with the corresponding day.
   - *Per SELECT, innermost first:*
     - `IN` → `EXISTS` (also inside `OR`). `NOT IN` → `NOT EXISTS` only when both sides are
       provably non-NULL. `x IN (A INTERSECT B)` → `x IN A AND x IN B`.
     - `EXISTS (… WHERE c AND (a OR b))` → `EXISTS (… a) OR EXISTS (… b)` when each part is
       correlated.
     - `LEFT JOIN … WHERE b.notnull_col IS NULL` → `NOT EXISTS` (also for a derived table `b` whose
       column is equated in ON); `LEFT JOIN b … GROUP BY <outer keys> HAVING COUNT(b.notnull_col) = 0`
       → `NOT EXISTS`. `EXISTS (SELECT … FROM (<plain or DISTINCT derived>) d …)` reads d's tables
       directly; `EXISTS (… FROM T b WHERE b.key = a.key AND p)` for an outer row a of T is p.
     - Row-valued `IN` / `NOT IN` → `EXISTS` / `NOT EXISTS` (NOT only when every column is
       non-NULL). An `EXISTS` over tables no condition links (after routing equalities through
       outer columns) is split into one `EXISTS` per group of tables.
     - A correlated aggregate in the select list guarded by an `EXISTS` over the same rows → an inner
       join + `GROUP BY` the outer primary keys. Scalar key lookups are also replaced inside larger
       expressions, `GROUP BY` and `ORDER BY`.
     - `LEFT JOIN` on a non-NULL foreign key → inner join.
     - A scalar subquery on a key → `LEFT JOIN`; one joining a chain of tables, each fixed by a
       key equality on the previous one and the value read from the last, → chained `LEFT JOIN`s.
     - `NULLS FIRST`/`LAST` is reset to the default for a sort key that is never NULL (including
       `COUNT`, and `SUM`/`AVG`/`MIN`/`MAX` of a never-NULL value in a plain `GROUP BY`).
     - `col = (SELECT k FROM t WHERE key = literal)` → join.
     - A single correlated aggregate subquery → `LEFT JOIN` + `GROUP BY` the outer primary keys;
       several correlated `COUNT(*)` subqueries → one `LEFT JOIN` each, counted as
       `COUNT(DISTINCT x.pk)` (the joins can multiply rows).
     - One row per outer row (`GROUP BY` covers its primary key) with children joined only for a
       `HAVING` test: `x IN (v1..vn) … COUNT(DISTINCT x) = n` → one `EXISTS` per value, and
       `COUNT(*) > 1` over a child keyed by (join column, a) → `EXISTS` of two children that
       differ in a.
     - `LEFT JOIN` + `HAVING COUNT(b.col) > k` → inner join.
     - `EXISTS` → join when the outer query is `DISTINCT` or the subquery's keys are all fixed.
     - `[NOT] EXISTS` (matching parent row via FK) → `fk IS [NOT] NULL`.
     - A join to a parent that only reads the parent's key → the foreign key column.
     - `EXCEPT` on a unique key → `NOT EXISTS`, plus self-join-on-key removal.
     - Redundant `IS NOT NULL` filters are dropped.
     - `COUNT(non-null col)` → `COUNT(*)`.
     - `GROUP BY` columns covering a key are reduced to the primary key. `SELECT e.. GROUP BY e..`
       (the same set, no aggregate) is `SELECT DISTINCT e..`; a `GROUP BY` with no aggregate that
       covers a key of every source is dropped. `x = x` on a never-NULL x is dropped.
     - `a RIGHT JOIN b ON c [more joins]` → `b LEFT JOIN a ON c [more joins]`.
     - `ROLLUP`, `CUBE`, `GROUPING SETS` and mixed lists are expanded to one explicit list of
       grouping sets (a single set becomes a plain `GROUP BY`). Repeated sets are kept unless
       `GROUP BY DISTINCT`. The key-reduction and `DISTINCT` rules are skipped under grouping sets.
     - `G UNION ALL T` → `G` with `GROUPING SETS ((keys), ())`, when `G` aggregates with a plain
       `GROUP BY` and `T` canonicalizes like `G` without `GROUP BY` and with its keys replaced by
       `NULL`, i.e. `T` is exactly the grand-total row. The union's `ORDER BY`/`LIMIT` carry over.
     - `A UNION B` → `A UNION ALL B` when each branch's rows are provably distinct (the `DISTINCT`
       rule above) and no row can be in both: some output position is `NULL` in one branch and
       never NULL in the other, or holds different literals, or both branches scan one table,
       output one of its NOT NULL keys and filter a column to different literals.
     - In a recursive CTE that walks a single-parent hierarchy (`FROM T x JOIN cte ON x.P = cte.k`,
       with `x.K` a NOT NULL key of `T` output as `k` in both terms), `UNION` → `UNION ALL` when the
       seed is closed: every seed row's parent is NULL, one literal, or one row picked by a unique
       key. Each row is then reached along exactly one path. On cyclic data the `UNION ALL` form
       never terminates; like the dataset's labels, the harness treats that as no result to compare.
     - `DISTINCT` is dropped when the output is provably unique, including when the output
       columns and equalities fix a key of every source table.
     - Columns made equal by inner-join conditions are replaced by one representative.
   - *Row order:* if the ground truth's outermost query has no `ORDER BY`, an outer `ORDER BY`
     without `LIMIT` is removed from both queries. Otherwise the `ORDER BY` lists must match.
     `ORDER BY` inside subqueries without `LIMIT` is always removed.
   - *Serialization:* a structural string in which order doesn't matter for inner-join relations,
     WHERE conjuncts, column-equality classes, `GROUP BY` keys, `AND`/`OR`, `=`/`<>`/`+`/`*`
     operands and `UNION`/`INTERSECT` branches. Column names and aliases are ignored. Table aliases
     get canonical names: for self-joins every assignment is tried (capped at 720) and the
     smallest string is kept.
5. **Partial credit.** A valid prediction that doesn't match scores `0.95 × Jaccard` similarity
   of the canonical tokens (tables, columns, operators, literals). Invalid predictions score 0,
   and matches score 1.0.

**Results on `dataset/pairs.jsonl`:** accuracy 99.1%, precision 100% (0 false positives), recall
98.0% (30 false negatives, all hand-written semantic equivalences). Automatic and malformed
pairs are all scored correctly; hand-written pairs 97.8%.

**Soundness checks:**
- `tests/test_equivalence.py` places a "precondition holds" pair next to a "precondition fails"
  look-alike for every rule. Writing these tests found and fixed two real false positives:
  aggregate-ownership bugs where an aggregate inside a subquery was attributed to the outer query.
- A stress run applying every breaking mutation to every target query (767 mutations) matched only
  one, which is in fact equivalent (an output alias unquoted). Every hand and automatic prediction
  compared against every other target of its schema (75,257 comparisons) matched only predictions
  that really do equal that other target (3, `org_chart_001`/`002`).
- Covering the missing SQL features exposed four false positives in code that predated them,
  all fixed, each with a regression test:
  - sqlglot read `NATURAL JOIN` as a cross join.
  - `INTERVAL '0.5 hours'` parsed as five hours, and `'-1 day'` as one day.
  - `qualify` expanded an output alias used in `WHERE`/`HAVING`, so the invalid
    `HAVING c > 1` matched `HAVING COUNT(*) > 1`.
  - Type errors inside an `EXISTS` select list were thrown away with that list.
- The labels themselves were checked on PostgreSQL (above).
- Inlining named windows closed a false positive: the serializer ignored the `WINDOW` clause, so
  `RANK() OVER w` matched whatever `w` was defined as.

**Performance and determinism:**
- The 3,193 dataset pairs take 2.2s on 10 workers (about 1,450 pairs/s; about 285 pairs/s on one).
- Ground-truth analysis is cached and shared across pairs.
- The code has no randomness, clocks, network access or execution. Output is byte-identical with
  any number of workers.

## Known limitations

- **One-sided by design.** Equivalences that need reasoning beyond the implemented rewrites are
  reported as non-matches. Of the 30 remaining false negatives, 11 predate the new features:
  - recursive CTEs seeded differently (3) and `WITH TIES` with a unique last sort key (1);
  - distributing a division (`Q*P*(1 - C/100)` vs. `Q*P*C/100`): NUMERIC division rounds to a
    scale that depends on the dividend's magnitude, so the two can differ for very large values;
  - `AVG(x) > k` vs. `SUM(x) > k * COUNT(*)`: AVG's NUMERIC division rounds, so on enormous inputs
    the two can disagree at the bound;
  - a LATERAL top-n with grouping inside it vs. a `ROW_NUMBER`/`RANK` over a join that already
    includes the outer table (2); a correlated sum over a date range vs. a `RANGE INTERVAL` window;
    `LEAST`/`GREATEST` pairing vs. an ordered self-join; and an average of per-post counts vs. a
    ratio of table counts (needs the counts summed through a foreign key).

  The other 19 are one-off idioms for the newer features:
  - **Strings and regex:** `array_to_string(array_agg(…))`, `format('%s …')`, the two
    `CASE` branches of a `COALESCE` written out, `SIMILAR TO` and `~*` vs. `~`.
  - **Rewritten logic:** a correlated top-1 lookup vs. `first_value`, a half-open month range
    vs. `date_trunc`, `to_char(date_trunc(…))`, `(x - x % 1000) / 1000`, and `GROUPING(x)` written
    as `(x IS NULL)::int`.
  - **Aggregates and joins:** conditional aggregation vs. a `FULL JOIN` of two aggregates, a
    `DISTINCT ON` mode vs. `mode()`, a `VALUES` join vs. a `CASE` lookup, `MIN`/`MAX(x::int)` and
    filtered counts as `bool_and`/`bool_or`, a correlated previous-row lookup vs. `LAG`, and an
    `IN (… MAX …)` latest row vs. `DISTINCT ON`.
- **CHECK constraints are ignored.** The task defines validity by types, keys, NOT NULL and
  foreign keys, so `flag <> 0` is not treated as equal to `flag = 1`. Enum types are used.
- **Numeric display scale is not modelled.** Results are compared by value semantics. Rewrites
  that could change the numeric result (re-association, distributing division) are not performed.
- **Validity is approximated.** Syntax is checked exactly by PostgreSQL's parser. Names,
  grouping, placement, shapes and the type errors listed above are checked statically and leniently.
  Not detected: calls to functions that do not exist or with the wrong arguments (beyond the listed
  ones), type errors involving expressions whose type is not inferred (function results, array
  elements), and runtime errors (division by zero, a scalar subquery returning several rows,
  `LIMIT -1`, an unknown time-zone name).
- **Session settings.** Equivalence must hold under any session time zone (the PostgreSQL check runs
  each database under a different one), so the session-local and fixed-offset readings of a
  timestamp are different. Other settings (`DateStyle`, locale and collation) are assumed to be the
  defaults.
- **Floating point.** Rewrites that would change float arithmetic (re-association,
  `SUM(x::numeric)`, `AVG` vs. `SUM/COUNT` of a `REAL`) are not performed. Like PostgreSQL itself,
  the harness does not fix the order in which a float `SUM` adds its rows.
- **Ties under `ORDER BY … LIMIT`.** Two queries with the same `ORDER BY` and `LIMIT` are
  treated as equal even when ties make both nondeterministic. The same goes for
  `ROW_NUMBER() = 1` / `<= n`, `DISTINCT ON` and `LATERAL … LIMIT n` with the same ordering. A
  prediction that adds a tie-breaker is reported as different.
- **sqlglot coverage.** If sqlglot can't parse a construct PostgreSQL accepts (`GROUPS` window
  frames, a frame `EXCLUDE` clause, `BETWEEN SYMMETRIC` mixed with a plain `BETWEEN` in one
  query), the pair falls back to parse-tree identity and is reported as `unsupported`, not invalid.
- **Unknown `schema_id`.** Without a schema the harness still parses and normalizes, but it skips
  every constraint-dependent rewrite and the column-existence checks.
