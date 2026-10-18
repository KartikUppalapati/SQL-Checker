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
  schema.py             #   DDL -> catalog (columns, NOT NULL, PK/UNIQUE, FKs, enum labels)
  canonical.py          #   equivalence-preserving canonicalization
  equivalence.py        #   compare(): gate -> fast path -> canonical comparison -> partial score
  cli.py                #   batch scoring, parallelism, summary report
  eval_dev.py           #   dev tool: list false positives / negatives on a labeled file
generator/              # programs used to generate the dataset (requirements in generator/requirements.txt)
dataset/
  schemas/<id>.sql      # one DDL file per schema
  queries.jsonl         # NL questions + target SQL
  pairs.jsonl           # labeled (ground_truth, predicted, equivalent) pairs
tests/
session/                # exported LLM/agent session history
```

## Usage

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./score --schemas dataset/schemas --pairs dataset/pairs.jsonl --output results.jsonl
python -m unittest discover -s tests                       # boundary tests for each rewrite rule
python -m sqleval.eval_dev --show fn                       # inspect misclassified labeled pairs
```

## Dataset design

All schemas and queries are PostgreSQL. The dataset has 8 schemas and 181 natural-language
questions, each with a target SQL query (22–23 questions per schema).

### How schemas were varied

Each schema is a deliberate combination of four variation axes. Each axis value appears in at
least two schemas, so no single convention is tied to a single domain:

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
- **Binary state.** Two-valued flags are stored three different ways: `SMALLINT` with
  `CHECK (x IN (0, 1))`, `VARCHAR` with `CHECK (x IN ('on', 'off'))`, and PostgreSQL `ENUM`
  types.

### Queries

`dataset/queries.jsonl` records have `id` (`<schema>_<nnn>`), `schema_id`, `question`, `sql` and
`tags`. Tags come from three sources:
- Derived from the SQL's syntax tree: `join`, `multi_join`, `aggregation`, `having`, `subquery`,
  `cte`, `recursive_cte`, `window`, `set_op`, `order_by`, `limit`, `exists`, `distinct`,
  `null_handling`.
- Hand-assigned: `self_join`, `m2m`, `anti_join`, `integer_division`, `generic_names`,
  `nullable`, `flag_int`/`flag_onoff`/`flag_enum`, `distinct_on`, `top_k`.
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

No database is available at generation time, so the targets are checked statically but have
not been executed.

### Labeled pairs

`dataset/pairs.jsonl` holds 2,046 pairs: 969 equivalent and 1,077 not. Each pair uses the
required fields (`id`, `schema_id`, `ground_truth`, `predicted`, `equivalent`). For analysis,
each pair also records `query_id`, `source`, `category` and a `note` explaining the label; the
harness ignores these. Every query in `queries.jsonl` contributes pairs from three sources:

| source | equivalent | not equivalent | what it is |
|---|---|---|---|
| `hand` | 248 | 387 | About 3–4 hand-written predictions per query, each with a one-line reason for its label |
| `auto` | 721 | 509 | Mutations of the target generated from its syntax tree (`generator/mutations.py`) |
| `malformed` | 0 | 181 | One malformed prediction per query |

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

**Automatic pairs** are chosen per query with an RNG seeded by the query id, so the build is
deterministic.
- *Equivalent rewrites:*
  - Cosmetic: case changes, reformatting, a comment or trailing semicolon, `!=`.
  - Structural: alias renaming, reordered conjuncts and join-equality sides, inner joins
    rewritten as implicit comma joins, `COUNT(1)`, and `ORDER BY 1` added when the target has
    no ordering.
- *Breaking mutations:*
  - Flip a comparison against a literal, change a literal, drop a literal filter.
  - Swap or drop output columns.
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
- Every prediction labeled equivalent parses and resolves against the schema.
- Hand-written negatives are valid SQL unless the note marks them `INVALID:`, and those must
  actually fail.
- Cosmetic rewrites leave the PostgreSQL parse tree unchanged.
- Breaking mutations change the parse tree.
- No pair is duplicated.

A harness that always answers "not equivalent" scores 52.6% on this set.

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
   markdown-fenced, multi-statement and DML predictions are rejected here. Rejected predictions
   get `match: false, score: 0`.
2. **Name resolution against the schema.** sqlglot `qualify` resolves every table and column under
   PostgreSQL identifier rules: unquoted names fold to lowercase, quoted names are case-sensitive.
   The prediction is also rejected for:
   - unknown tables or columns, or ambiguous columns;
   - an enum literal that is not a label of its type (a runtime error in PostgreSQL);
   - selecting a column that is neither grouped nor covered by a grouped primary key (UNIQUE
     columns don't count, matching PostgreSQL);
   - `SELECT DISTINCT … ORDER BY` an expression that is not in the select list.
3. **Fast path.** If the two parse trees are identical once source positions are ignored, the
   queries match.
4. **Canonicalization** (`sqleval/canonical.py`). Each rewrite below states the condition that
   makes it sound:
   - *Names and structure:* parentheses, `FETCH FIRST`/`LIMIT`, positional and alias references in
     `GROUP BY`/`ORDER BY`, `*` expansion, `USING` → `ON`. CTEs become derived tables (when read
     more than once, only if deterministic: no `random()`, `nextval`, ...). Aliases are made
     globally unique. Derived-table columns nothing reads are dropped (not under `DISTINCT`), and
     the rest are renamed by position.
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
     - `~* 'lit'`, `lower(x) LIKE` and `ILIKE` are unified.
   - *Type-aware literals:*
     - An untyped string compared with a DATE or TIMESTAMP column takes the column's type.
     - Bounds over discrete types are tightened: `x <= 5` → `x < 6` for integers, by one day
       for DATE and one microsecond for TIMESTAMP.
     - `EXTRACT(YEAR FROM d) = y` becomes a date range.
     - Enum comparisons are checked against the type's labels, and `<> 'No'` on a two-label enum
       becomes `= 'Yes'`.
   - *Per SELECT, innermost first:*
     - `IN` → `EXISTS` (also inside `OR`). `NOT IN` → `NOT EXISTS` only when both sides are
       provably non-NULL. `x IN (A INTERSECT B)` → `x IN A AND x IN B`.
     - `EXISTS (… WHERE c AND (a OR b))` → `EXISTS (… a) OR EXISTS (… b)` when each part is
       correlated.
     - `LEFT JOIN … WHERE b.notnull_col IS NULL` → `NOT EXISTS`.
     - `LEFT JOIN` on a non-NULL foreign key → inner join.
     - A scalar subquery on a key → `LEFT JOIN`; one joining a chain of tables, each fixed by a
       key equality on the previous one and the value read from the last, → chained `LEFT JOIN`s.
     - `NULLS FIRST`/`LAST` is reset to the default for a sort key that is never NULL.
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
     - `GROUP BY` columns covering a key are reduced to the primary key.
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

**Results on `dataset/pairs.jsonl`:** accuracy 99.8%, precision 100% (0 false positives), recall
99.6% (4 false negatives, all hand-written semantic equivalences).

**Soundness checks:**
- `tests/test_equivalence.py` places a "precondition holds" pair next to a "precondition fails"
  look-alike for every rule. Writing these tests found and fixed two real false positives:
  aggregate-ownership bugs where an aggregate inside a subquery was attributed to the outer query.
- A stress run applying every breaking mutation to every target query (611 queries) produced no
  false matches. Every equivalence rewrite (1,636) was recognized.

**Performance and determinism:**
- 10,563 pairs take 8.8s on 2 workers (about 1,200 pairs/s) with a peak of 61 MB RSS.
- Ground-truth analysis is cached and shared across pairs.
- The code has no randomness, clocks, network access or execution. Output is byte-identical with
  any number of workers.

## Known limitations

- **One-sided by design.** Equivalences that need reasoning beyond the implemented rewrites are
  reported as non-matches. The 4 remaining false negatives:
  - recursive CTEs seeded differently (2: a scalar vs. a joined seed; seeding with the root and
    filtering it out);
  - distributing a division (`Q*P*(1 - C/100)` vs. `Q*P*C/100`): NUMERIC division rounds to a
    scale that depends on the dividend's magnitude, so the two can differ for very large values;
  - an average of per-post counts vs. a ratio of table counts: needs the counts to be summed
    through a foreign key, and the naive `COUNT/COUNT` form divides by zero where `AVG` is NULL.
- **CHECK constraints are ignored.** The task defines validity by types, keys, NOT NULL and
  foreign keys, so `flag <> 0` is not treated as equal to `flag = 1`. Enum types are used.
- **Numeric display scale is not modelled.** Results are compared by value semantics. Rewrites
  that could change the numeric result (re-association, distributing division) are not performed.
- **Validity is approximated.** Syntax is checked exactly by PostgreSQL's parser. Name
  resolution, enum labels and grouping rules are checked statically. Type errors (comparing an
  integer to a non-numeric string) and runtime errors (division by zero, a scalar subquery
  returning several rows) are not detected.
- **Ties under `ORDER BY … LIMIT`.** Two queries with the same `ORDER BY` and `LIMIT` are
  treated as equal even when ties make both nondeterministic. The same goes for
  `ROW_NUMBER() = 1` and `DISTINCT ON` with the same ordering. A prediction that adds a
  tie-breaker is reported as different.
- **sqlglot coverage.** If sqlglot can't parse a construct PostgreSQL accepts (e.g.
  `BETWEEN SYMMETRIC`), the pair falls back to parse-tree identity and is reported as
  `unsupported`, not invalid.
- **Unknown `schema_id`.** Without a schema the harness still parses and normalizes, but it skips
  every constraint-dependent rewrite and the column-existence checks.
